#!/usr/bin/env python3
"""GitHub webhook listener — triggers auto-pull on push events.

Runs on port 3458, exposed via Cloudflare tunnel at deploy.andeslee.com.
GitHub sends POST /webhook on every push, this script runs the auto-pull script.

Usage:
    python3 scripts/github-webhook.py                  # default (warn if no secret)
    python3 scripts/github-webhook.py --require-secret  # exit if no secret set
"""

import argparse
import hashlib
import hmac
import json
import os
import subprocess
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = 3458
AUTO_PULL_SCRIPT = "/Users/andeslee/.openclaw/workspace/scripts/github-auto-pull.sh"
RELOAD_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reload-bots.sh")
LOG = "/tmp/github-webhook.log"

# Module-level — set by main() after arg parsing
WEBHOOK_SECRET = ""


def log(msg):
    import datetime
    line = f"[{datetime.datetime.now().isoformat()}] {msg}"
    with open(LOG, "a") as f:
        f.write(line + "\n")


def verify_signature(payload, signature):
    """Verify GitHub HMAC-SHA256 signature."""
    if not WEBHOOK_SECRET:
        return True
    if not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(
        WEBHOOK_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)


class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/webhook":
            self.send_response(404)
            self.end_headers()
            return

        content_length = int(self.headers.get("Content-Length", 0))
        payload = self.rfile.read(content_length)
        signature = self.headers.get("X-Hub-Signature-256", "")

        if not verify_signature(payload, signature):
            log("REJECTED: invalid signature")
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"Invalid signature")
            return

        event = self.headers.get("X-GitHub-Event", "")
        log(f"Received event: {event}")

        if event == "push":
            try:
                data = json.loads(payload)
                repo = data.get("repository", {}).get("name", "unknown")
                ref = data.get("ref", "")
                pusher = data.get("pusher", {}).get("name", "unknown")
                commits = [c.get("message", "").split("\n")[0] for c in data.get("commits", [])]
                log(f"Push to {repo} ({ref}) by {pusher}: {commits}")

                # Only trigger on main branch
                if ref in ("refs/heads/main", "refs/heads/master"):
                    log(f"Triggering auto-pull + bot reload...")
                    subprocess.Popen(
                        ["/bin/bash", "-c", f'"{AUTO_PULL_SCRIPT}" && /bin/bash "{RELOAD_SCRIPT}"'],
                        stdout=open("/tmp/github-auto-pull.log", "a"),
                        stderr=subprocess.STDOUT,
                    )
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"OK: pull triggered")
                else:
                    log(f"Ignoring push to {ref} (not main)")
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"OK: ignored non-main branch")
            except Exception as e:
                log(f"Error processing push: {e}")
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode())
        elif event == "ping":
            log("Ping received — webhook configured successfully")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"pong")
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(f"Ignored event: {event}".encode())

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"GitHub webhook listener running")

    def log_message(self, format, *args):
        pass  # Suppress default HTTP logging


def main(args=None):
    """Entry point. Parses args and starts the webhook server."""
    global WEBHOOK_SECRET

    parser = argparse.ArgumentParser(description="GitHub webhook listener")
    parser.add_argument("--require-secret", action="store_true",
                        help="Exit with error if GITHUB_WEBHOOK_SECRET is not set")
    parsed = parser.parse_args(args)

    WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")

    if parsed.require_secret and not WEBHOOK_SECRET:
        print("ERROR: --require-secret set but GITHUB_WEBHOOK_SECRET env var is empty.",
              file=sys.stderr)
        print("Set GITHUB_WEBHOOK_SECRET for production security.", file=sys.stderr)
        sys.exit(1)

    if not WEBHOOK_SECRET:
        print("WARNING: GITHUB_WEBHOOK_SECRET not set — webhook signature verification disabled")
        print("Set GITHUB_WEBHOOK_SECRET env var for production security")

    log(f"Starting webhook listener on port {PORT}")
    server = HTTPServer(("127.0.0.1", PORT), WebhookHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Shutting down")
        server.server_close()


if __name__ == "__main__":
    main()
