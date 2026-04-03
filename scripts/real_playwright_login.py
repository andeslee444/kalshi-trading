#!/usr/bin/env python3
"""Headless Playwright login helper for Real Sports."""

from __future__ import annotations

import json
import os
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


OUTPUT_PATH = Path("/tmp/real-playwright-login-result.json")


def _collect_storage(kind: str) -> str:
    if kind not in {"localStorage", "sessionStorage"}:
        raise ValueError(f"unsupported storage kind: {kind}")
    return f"""
        () => {{
          const out = {{}};
          const storage = window.{kind};
          for (let i = 0; i < storage.length; i++) {{
            const key = storage.key(i);
            out[key] = storage.getItem(key);
          }}
          return out;
        }}
    """


def main() -> int:
    email = os.environ.get("REAL_LOGIN_EMAIL")
    password = os.environ.get("REAL_LOGIN_PASSWORD")
    if not email or not password:
        raise SystemExit("REAL_LOGIN_EMAIL and REAL_LOGIN_PASSWORD are required")

    result: dict[str, object] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.goto("https://www.realapp.com/login", wait_until="networkidle", timeout=60000)
        page.get_by_placeholder("Username, phone number or email").fill(email)
        page.get_by_placeholder("Password").fill(password)
        page.get_by_text("Log in", exact=True).click()
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(5000)
        local_storage = page.evaluate(_collect_storage("localStorage"))
        session_storage = page.evaluate(_collect_storage("sessionStorage"))
        cookies = context.cookies()
        result = {
            "url": page.url,
            "title": page.title(),
            "body_text": page.locator("body").inner_text()[:4000],
            "local_storage": local_storage,
            "session_storage": session_storage,
            "cookies": cookies,
            "local_storage_keys": sorted(local_storage.keys()),
            "session_storage_keys": sorted(session_storage.keys()),
        }
        OUTPUT_PATH.write_text(json.dumps(result, indent=2))
        browser.close()

    print(
        json.dumps(
            {
                "url": result["url"],
                "title": result["title"],
                "local_storage_keys": result["local_storage_keys"],
                "session_storage_keys": result["session_storage_keys"],
                "cookie_names": sorted(cookie.get("name") for cookie in result["cookies"]),
                "body_text": str(result["body_text"])[:1500],
                "result_path": str(OUTPUT_PATH),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
