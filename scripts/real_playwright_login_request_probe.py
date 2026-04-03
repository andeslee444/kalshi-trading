#!/usr/bin/env python3
"""Capture the Real login request shape with sensitive values redacted."""

from __future__ import annotations

import json
import os

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


def _redact_payload(value):
    if isinstance(value, dict):
        return {k: _redact_payload(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_payload(v) for v in value]
    if isinstance(value, str):
        return "<redacted>" if value else value
    return value


def main() -> int:
    email = os.environ.get("REAL_LOGIN_EMAIL")
    password = os.environ.get("REAL_LOGIN_PASSWORD")
    if not email or not password:
        raise SystemExit("REAL_LOGIN_EMAIL and REAL_LOGIN_PASSWORD are required")

    captured: list[dict[str, object]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        def handle_request(request) -> None:
            if "/login" not in request.url:
                return
            raw = request.post_data or ""
            try:
                payload = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                payload = raw
            captured.append(
                {
                    "method": request.method,
                    "url": request.url,
                    "headers": {
                        k: v
                        for k, v in request.headers.items()
                        if k.lower().startswith("real-")
                        or k.lower() in {"origin", "referer", "content-type"}
                    },
                    "post_data": _redact_payload(payload),
                }
            )

        page.on("request", handle_request)
        page.goto("https://www.realapp.com/login", wait_until="networkidle", timeout=60000)
        page.get_by_placeholder("Username, phone number or email").fill(email)
        page.get_by_placeholder("Password").fill(password)
        page.get_by_text("Log in", exact=True).click()
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(5000)
        browser.close()

    print(json.dumps(captured, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
