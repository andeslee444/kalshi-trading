#!/usr/bin/env python3
"""Capture Real browser websocket/request behavior after login."""

from __future__ import annotations

import json
import os
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import WebSocket
from playwright.sync_api import sync_playwright


OUTPUT_PATH = Path("/tmp/real-playwright-ws-probe.json")


def _trim(value: object, limit: int = 500) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + "...<trimmed>"


def main() -> int:
    email = os.environ.get("REAL_LOGIN_EMAIL")
    password = os.environ.get("REAL_LOGIN_PASSWORD")
    if not email or not password:
        raise SystemExit("REAL_LOGIN_EMAIL and REAL_LOGIN_PASSWORD are required")

    requests: list[dict[str, object]] = []
    websockets: list[dict[str, object]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        def handle_request(request) -> None:
            url = request.url
            if "real" not in url.lower():
                return
            requests.append(
                {
                    "method": request.method,
                    "url": url,
                    "resource_type": request.resource_type,
                    "headers": {
                        k: v
                        for k, v in request.headers.items()
                        if k.lower().startswith("real-") or k.lower() in {"origin", "referer"}
                    },
                }
            )

        def handle_websocket(ws: WebSocket) -> None:
            entry: dict[str, object] = {"url": ws.url, "frames_sent": [], "frames_received": []}
            websockets.append(entry)

            def on_sent(payload: str) -> None:
                frames = entry["frames_sent"]
                assert isinstance(frames, list)
                if len(frames) < 20:
                    frames.append(_trim(payload))

            def on_received(payload: str) -> None:
                frames = entry["frames_received"]
                assert isinstance(frames, list)
                if len(frames) < 20:
                    frames.append(_trim(payload))

            ws.on("framesent", on_sent)
            ws.on("framereceived", on_received)

        page.on("request", handle_request)
        page.on("websocket", handle_websocket)

        page.goto("https://www.realapp.com/login", wait_until="networkidle", timeout=60000)
        page.get_by_placeholder("Username, phone number or email").fill(email)
        page.get_by_placeholder("Password").fill(password)
        page.get_by_text("Log in", exact=True).click()
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(20000)

        result = {
            "url": page.url,
            "requests": requests,
            "websockets": websockets,
        }
        OUTPUT_PATH.write_text(json.dumps(result, indent=2))
        browser.close()

    summary = {
        "url": result["url"],
        "request_count": len(requests),
        "websocket_count": len(websockets),
        "websocket_urls": [ws["url"] for ws in websockets],
        "result_path": str(OUTPUT_PATH),
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
