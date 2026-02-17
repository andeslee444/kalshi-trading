#!/usr/bin/env python3
"""Shared Kalshi API authentication and utilities.

All bots should use this module instead of duplicating auth logic.

Usage:
    from kalshi_auth import KalshiClient, load_trades, save_trade, setup_unbuffered

    client = KalshiClient()  # reads from env vars
    data = client.get("/portfolio/balance")
    client.post("/portfolio/orders", body={...})
"""

import json, time, base64, os, sys, signal, logging
import requests
from pathlib import Path
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

# === Constants ===
PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_KEY_PATH = PROJECT_DIR / "config" / "keys" / "kalshi-demo.pem"

DEMO_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"
PROD_BASE_URL = "https://trading-api.kalshi.com/trade-api/v2"

MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 1.0  # seconds

_log = logging.getLogger("kalshi_auth")


def setup_unbuffered():
    """Enable unbuffered stdout for real-time logging."""
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(line_buffering=True)
    os.environ['PYTHONUNBUFFERED'] = '1'


def setup_logging(name, log_file=None):
    """Configure a logger with consistent format for a bot.

    Returns a logging.Logger with stdout handler (and optional file handler).
    Call once at bot startup: ``log = setup_logging("weather-bot")``
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # Already configured
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    if log_file:
        fh = logging.FileHandler(log_file)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger


def setup_signal_handlers():
    """Install graceful shutdown handlers for SIGTERM and SIGINT."""
    def _handler(signum, frame):
        _log.info("Received signal %s, shutting down gracefully...", signum)
        sys.exit(0)
    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


class KalshiClient:
    """Kalshi API client with RSA-PSS authentication and retry logic."""

    def __init__(self, api_key=None, key_path=None, mode=None):
        """Initialize client.

        Args:
            api_key: Kalshi API key. Falls back to KALSHI_API_KEY env var.
            key_path: Path to RSA private key PEM file. Falls back to
                      KALSHI_KEY_FILE env var, then default demo key path.
            mode: "demo" or "production". Falls back to KALSHI_MODE env var,
                  then defaults to "demo".
        """
        self.api_key = api_key or os.environ.get("KALSHI_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "Kalshi API key required. Set KALSHI_API_KEY env var or pass api_key="
            )

        key_file = key_path or os.environ.get("KALSHI_KEY_FILE", str(DEFAULT_KEY_PATH))
        key_path_obj = Path(key_file)
        if not key_path_obj.is_absolute():
            key_path_obj = PROJECT_DIR / key_file

        try:
            with open(key_path_obj, "rb") as f:
                self.private_key = serialization.load_pem_private_key(
                    f.read(), password=None, backend=default_backend()
                )
        except FileNotFoundError:
            raise FileNotFoundError(
                f"RSA private key not found at {key_path_obj}. "
                f"Set KALSHI_KEY_FILE env var to the correct path."
            )
        except Exception as e:
            raise ValueError(f"Failed to load RSA private key from {key_path_obj}: {e}")

        self.mode = mode or os.environ.get("KALSHI_MODE", "demo")
        if self.mode == "production":
            self.base_url = PROD_BASE_URL
        else:
            self.base_url = DEMO_BASE_URL

    def _sign(self, method: str, path: str) -> dict:
        """Generate authentication headers for a request."""
        ts = str(int(time.time() * 1000))
        path_clean = path.split("?")[0]
        msg = f"{ts}{method}{path_clean}"
        sig = self.private_key.sign(
            msg.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode("utf-8"),
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, body=None, timeout=15):
        """Make an authenticated API request with retry on transient errors."""
        url = self.base_url + path
        full_path = "/trade-api/v2" + path
        headers = self._sign(method, full_path)

        last_err = None
        for attempt in range(MAX_RETRIES):
            try:
                if method == "GET":
                    r = requests.get(url, headers=headers, timeout=timeout)
                elif method == "POST":
                    r = requests.post(url, headers=headers, json=body, timeout=timeout)
                elif method == "DELETE":
                    r = requests.delete(url, headers=headers, timeout=timeout)
                else:
                    r = requests.request(method, url, headers=headers, json=body, timeout=timeout)

                # Don't retry client errors (4xx) except 429
                if r.status_code == 429:
                    wait = RETRY_BACKOFF_BASE * (2 ** attempt)
                    _log.warning("Rate limited (429), retrying in %.1fs...", wait)
                    time.sleep(wait)
                    headers = self._sign(method, full_path)  # re-sign with fresh timestamp
                    continue

                r.raise_for_status()
                return r.json()

            except requests.exceptions.ConnectionError as e:
                last_err = e
                wait = RETRY_BACKOFF_BASE * (2 ** attempt)
                _log.warning("Connection error, retrying in %.1fs... (%s)", wait, e)
                time.sleep(wait)
                headers = self._sign(method, full_path)
            except requests.exceptions.Timeout as e:
                last_err = e
                wait = RETRY_BACKOFF_BASE * (2 ** attempt)
                _log.warning("Timeout, retrying in %.1fs...", wait)
                time.sleep(wait)
                headers = self._sign(method, full_path)
            except requests.exceptions.HTTPError:
                raise  # Don't retry other HTTP errors
            except Exception:
                raise

        raise last_err or Exception("Max retries exceeded")

    def get(self, path: str, **kwargs):
        """Make an authenticated GET request."""
        return self._request("GET", path, **kwargs)

    def post(self, path: str, body=None, **kwargs):
        """Make an authenticated POST request."""
        return self._request("POST", path, body=body, **kwargs)

    def delete(self, path: str, **kwargs):
        """Make an authenticated DELETE request."""
        return self._request("DELETE", path, **kwargs)

    def get_all_markets(self, prefix=None, status="open", max_pages=50):
        """Paginate through all open markets, optionally filtering by ticker prefix."""
        all_markets = []
        cursor = None
        for _ in range(max_pages):
            path = f"/markets?status={status}&limit=1000"
            if cursor:
                path += f"&cursor={cursor}"
            try:
                data = self.get(path)
            except Exception as e:
                _log.error("Market page error: %s", e)
                break
            batch = data.get("markets", [])
            if prefix:
                for m in batch:
                    if m.get("ticker", "").startswith(prefix):
                        all_markets.append(m)
            else:
                all_markets.extend(batch)
            cursor = data.get("cursor")
            if not cursor or not batch:
                break
        return all_markets

    def get_balance(self):
        """Get portfolio balance. Returns (balance_cents, available_cents)."""
        data = self.get("/portfolio/balance")
        return data.get("balance", 0), data.get("available_balance", data.get("balance", 0))


# === Trade file utilities ===

def load_trades(trades_path: Path) -> list:
    """Load trades from a JSON file. Returns [] on missing/corrupt file."""
    if trades_path.exists():
        try:
            return json.loads(trades_path.read_text())
        except (json.JSONDecodeError, ValueError) as e:
            _log.warning("Corrupt trades file %s: %s", trades_path, e)
            return []
    return []


def save_trade(trades_path: Path, trade: dict):
    """Append a trade to a JSON trades file."""
    trades = load_trades(trades_path)
    trades.append(trade)
    trades_path.parent.mkdir(parents=True, exist_ok=True)
    trades_path.write_text(json.dumps(trades, indent=2))
