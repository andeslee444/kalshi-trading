#!/usr/bin/env python3
"""Probe the Real login response shape with secrets redacted."""

from __future__ import annotations

import asyncio
import json
import os

import httpx

from domain.oracle.real_sports_client import (
    RealSportsConfig,
    _DEVICE_NAME,
    _REAL_VERSION,
    _ensure_real_device_uuid,
    _generate_request_token,
)


async def _run() -> dict[str, object]:
    cfg = RealSportsConfig(
        email=os.environ["REAL_LOGIN_EMAIL"],
        password=os.environ["REAL_LOGIN_PASSWORD"],
    )
    async with httpx.AsyncClient(base_url=cfg.base_url, timeout=cfg.timeout_seconds) as client:
        resp = await client.post(
            "/login",
            json={"login": cfg.email, "password": cfg.password, "tfaAuthCode": ""},
            headers={
                "content-type": "application/json",
                "real-device-type": "desktop_web",
                "real-device-name": _DEVICE_NAME,
                "real-device-uuid": _ensure_real_device_uuid(cfg),
                "real-version": _REAL_VERSION,
                "real-request-token": _generate_request_token(),
                "referer": "https://www.realapp.com/",
            },
        )
        data = resp.json()

    user = data.get("user", {}) if isinstance(data, dict) else {}
    return {
        "status": resp.status_code,
        "keys": sorted(data.keys()) if isinstance(data, dict) else [type(data).__name__],
        "user_keys": sorted(user.keys()) if isinstance(user, dict) else [],
        "has_token": bool(data.get("token")) if isinstance(data, dict) else False,
        "token_len": len(data.get("token") or "") if isinstance(data, dict) else 0,
        "has_device_id": bool(data.get("deviceId")) if isinstance(data, dict) else False,
        "device_id_len": len(data.get("deviceId") or "") if isinstance(data, dict) else 0,
        "user_id_candidates": {
            "user.id": user.get("id") if isinstance(user, dict) else None,
            "user.userId": user.get("userId") if isinstance(user, dict) else None,
            "top.userId": data.get("userId") if isinstance(data, dict) else None,
        },
    }


def main() -> int:
    email = os.environ.get("REAL_LOGIN_EMAIL")
    password = os.environ.get("REAL_LOGIN_PASSWORD")
    if not email or not password:
        raise SystemExit("REAL_LOGIN_EMAIL and REAL_LOGIN_PASSWORD are required")
    print(json.dumps(asyncio.run(_run()), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
