#!/usr/bin/env python3
"""GitHub webhook listener — fast inline deploy on push events.

Runs on port 3458, exposed via Cloudflare tunnel at deploy.andeslee.com.
On push to main: git pull, send a WhatsApp notification, then reload bots in background.

Usage:
    python3 scripts/github-webhook.py                  # default (warn if no secret)
    python3 scripts/github-webhook.py --require-secret  # exit if no secret set
"""

import argparse
import datetime
import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = 3458
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELOAD_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reload-bots.sh")
LOG = "/tmp/github-webhook.log"
sys.path.insert(0, os.path.join(PROJECT_DIR, "src", "kalshi"))

# Module-level — set by main() after arg parsing / env loading
from kalshi_auth import notify_whatsapp

WEBHOOK_SECRET = ""


def log(msg):
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


def send_whatsapp(text):
    """Send a deploy notification through WhatsApp."""
    try:
        if notify_whatsapp(text):
            log("WhatsApp notification sent")
        else:
            log("WhatsApp notification skipped or failed")
    except Exception as e:
        log(f"WhatsApp notification failed: {e}")


def run_command(cmd, cwd=None, timeout=60):
    """Run a shell command, return (success, stdout+stderr)."""
    try:
        result = subprocess.run(
            cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
        output = (result.stdout + result.stderr).strip()
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, f"Command timed out after {timeout}s"
    except Exception as e:
        return False, str(e)


def deploy(repo_name, commits, changed_files):
    """Pull, notify, then do slow work (pip, sync, reload) in sequence."""
    try:
        # 1. git pull
        ok, output = run_command("git pull --ff-only", cwd=PROJECT_DIR, timeout=30)
        log(f"git pull: {'OK' if ok else 'FAILED'} — {output}")
        if not ok:
            send_whatsapp(f"⚠️ {repo_name} deploy FAILED: git pull\n{output[:200]}")
            return

        # 2. Send WhatsApp notification immediately
        commit_summary = "\n".join(f"• {c}" for c in commits[:5])
        if len(commits) > 5:
            commit_summary += f"\n  ...and {len(commits) - 5} more"
        send_whatsapp(f"🚀 {repo_name} deployed\n{commit_summary}")

        # 3. pip install (only if requirements.txt changed)
        if "requirements.txt" in changed_files:
            log("requirements.txt changed — running pip install")
            ok, output = run_command(
                f"pip install -r {PROJECT_DIR}/requirements.txt", cwd=PROJECT_DIR, timeout=120,
            )
            log(f"pip install: {'OK' if ok else 'FAILED'} — {output[:300]}")

        # 4. S3 sync
        ok, output = run_command("npm run sync:down", cwd=PROJECT_DIR, timeout=60)
        log(f"sync:down: {'OK' if ok else 'FAILED'} — {output[:200]}")
        ok, output = run_command("npm run sync:up", cwd=PROJECT_DIR, timeout=60)
        log(f"sync:up: {'OK' if ok else 'FAILED'} — {output[:200]}")

        # 5. Reload bots
        ok, output = run_command(f'bash "{RELOAD_SCRIPT}"', cwd=PROJECT_DIR, timeout=10)
        log(f"reload-bots: {'OK' if ok else 'FAILED'} — {output}")

        log("Deploy complete")
    except Exception as e:
        log(f"Deploy error: {e}")


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

                # Collect changed files from all commits
                changed_files = set()
                for c in data.get("commits", []):
                    changed_files.update(c.get("added", []))
                    changed_files.update(c.get("modified", []))
                    changed_files.update(c.get("removed", []))

                if ref in ("refs/heads/main", "refs/heads/master"):
                    log("Triggering inline deploy...")
                    threading.Thread(
                        target=deploy,
                        args=(repo, commits, changed_files),
                        daemon=True,
                    ).start()
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"OK: deploy triggered")
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
        pass


def load_env():
    """Load .env file if present (simple key=value parsing)."""
    env_path = os.path.join(PROJECT_DIR, ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if key and key not in os.environ:
                os.environ[key] = value


def main(args=None):
    global WEBHOOK_SECRET

    parser = argparse.ArgumentParser(description="GitHub webhook listener")
    parser.add_argument("--require-secret", action="store_true",
                        help="Exit with error if GITHUB_WEBHOOK_SECRET is not set")
    parsed = parser.parse_args(args)

    load_env()

    WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")

    if parsed.require_secret and not WEBHOOK_SECRET:
        print("ERROR: --require-secret set but GITHUB_WEBHOOK_SECRET env var is empty.",
              file=sys.stderr)
        print("Set GITHUB_WEBHOOK_SECRET for production security.", file=sys.stderr)
        sys.exit(1)

    if not WEBHOOK_SECRET:
        print("WARNING: GITHUB_WEBHOOK_SECRET not set — webhook signature verification disabled")

    log(f"Starting webhook listener on port {PORT}")
    server = HTTPServer(("127.0.0.1", PORT), WebhookHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Shutting down")
        server.server_close()


if __name__ == "__main__":
    main()
