#!/usr/bin/env python3
"""
Process Watchdog — monitors all Harbor processes for:
1. Dead processes (PID file exists but process gone)
2. Crash loops (process alive but CPU > 90% for multiple checks)
3. Stale processes (no log output for extended period)

Alerts via OpenClaw WhatsApp. Kills runaway processes automatically.
Runs every 5 minutes via cron or standalone.
"""

import os
import sys
import time
import json
import signal
import subprocess
from pathlib import Path
from datetime import datetime, timedelta

# === Configuration ===
WATCHDOG_STATE = Path("/tmp/watchdog-state.json")
ALERT_COOLDOWN_MIN = 30  # Don't re-alert for same issue within this window

PROCESSES = {
    # Kalshi bots removed — managed by supervisor (scripts/supervisor.py)
    "flight-daemon": {
        "pid_file": "/tmp/flight-daemon.pid",
        "log_file": "/tmp/flight-daemon.log",
        "restart_cmd": None,  # Currently paused intentionally
        "max_cpu": 50,
    },
    "wireproxy": {
        "pid_file": "/tmp/wireproxy.pid",
        "log_file": "/tmp/wireproxy.log",
        "restart_cmd": "nohup /tmp/wireproxy -c /tmp/wireproxy.conf > /tmp/wireproxy.log 2>&1 & echo $! > /tmp/wireproxy.pid",
        "max_cpu": 20,
    },
    "harbor-web": {
        "pid_file": "/tmp/harbor-web.pid",
        "log_file": "/tmp/harbor-web.log",
        "restart_cmd": "cd /Users/andeslee/Documents/cursor-projects/Andes-Website && nohup npx tsx web/server.ts >> /tmp/harbor-web.log 2>&1 & echo $! > /tmp/harbor-web.pid",
        "max_cpu": 50,
    },
}

# === Helpers ===

def load_state():
    if WATCHDOG_STATE.exists():
        try:
            return json.loads(WATCHDOG_STATE.read_text())
        except:
            pass
    return {"alerts": {}, "cpu_history": {}}


def save_state(state):
    WATCHDOG_STATE.write_text(json.dumps(state, indent=2, default=str))


def is_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:
        return False


def get_cpu(pid):
    """Get CPU% for a PID."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "%cpu="],
            capture_output=True, text=True, timeout=5
        )
        return float(result.stdout.strip()) if result.stdout.strip() else 0
    except:
        return 0


def get_log_age(log_file):
    """Seconds since log file was last modified."""
    try:
        mtime = os.path.getmtime(log_file)
        return time.time() - mtime
    except:
        return float("inf")


def send_alert(message):
    """Alert via OpenClaw CLI."""
    print(f"[ALERT] {message}")
    try:
        subprocess.run(
            ["/opt/homebrew/bin/openclaw", "message", "send",
             "--channel", "whatsapp",
             "--to", "+14255336828",
             "--message", f"🚨 Watchdog: {message}"],
            timeout=15,
            capture_output=True
        )
    except Exception as e:
        print(f"[ALERT FAILED] {e}")
        # Fallback: write to alert log
        with open("/tmp/watchdog-alerts.log", "a") as f:
            f.write(f"[{datetime.now().isoformat()}] {message}\n")


def should_alert(state, key):
    """Check cooldown — don't spam for the same issue."""
    last = state["alerts"].get(key)
    if not last:
        return True
    try:
        last_time = datetime.fromisoformat(last)
        return datetime.now() - last_time > timedelta(minutes=ALERT_COOLDOWN_MIN)
    except:
        return True


def mark_alerted(state, key):
    state["alerts"][key] = datetime.now().isoformat()


# === Main Check ===

def run_check():
    state = load_state()
    if "cpu_history" not in state:
        state["cpu_history"] = {}

    issues = []

    for name, config in PROCESSES.items():
        pid_file = config["pid_file"]

        # 1. Check if PID file exists
        if not os.path.exists(pid_file):
            continue  # No PID file = not expected to run

        try:
            pid = int(open(pid_file).read().strip())
        except:
            continue

        # 2. Check if process is alive
        if not is_alive(pid):
            issue = f"{name} is DEAD (stale PID {pid})"
            issues.append(issue)

            if config.get("restart_cmd") and should_alert(state, f"dead:{name}"):
                send_alert(f"{issue} — attempting restart")
                mark_alerted(state, f"dead:{name}")
                try:
                    subprocess.run(config["restart_cmd"], shell=True, timeout=15)
                    time.sleep(2)
                    # Verify restart
                    try:
                        new_pid = int(open(pid_file).read().strip())
                        if is_alive(new_pid):
                            send_alert(f"✅ {name} restarted successfully (PID {new_pid})")
                        else:
                            send_alert(f"❌ {name} restart FAILED — still dead")
                    except:
                        send_alert(f"❌ {name} restart FAILED — no PID")
                except Exception as e:
                    send_alert(f"❌ {name} restart error: {e}")
            elif should_alert(state, f"dead:{name}"):
                send_alert(f"{issue} — no auto-restart configured")
                mark_alerted(state, f"dead:{name}")
            continue

        # 3. Check CPU usage (detect crash loops)
        cpu = get_cpu(pid)
        history = state["cpu_history"].get(name, [])
        history.append({"cpu": cpu, "time": datetime.now().isoformat()})
        # Keep last 6 checks (30 min at 5-min intervals)
        history = history[-6:]
        state["cpu_history"][name] = history

        max_cpu = config.get("max_cpu", 50)
        # If CPU > max for 3+ consecutive checks, it's likely a crash loop
        high_cpu_streak = sum(1 for h in history[-3:] if h["cpu"] > max_cpu)

        if high_cpu_streak >= 3:
            issue = f"{name} (PID {pid}) HIGH CPU: {cpu:.0f}% for 3+ checks — likely crash loop"
            issues.append(issue)

            if should_alert(state, f"cpu:{name}"):
                # Kill it
                try:
                    os.kill(pid, signal.SIGKILL)
                    send_alert(f"{issue} — KILLED runaway process")
                    # Clear PID file
                    os.remove(pid_file)
                except Exception as e:
                    send_alert(f"{issue} — kill failed: {e}")
                mark_alerted(state, f"cpu:{name}")

        # 4. Check log staleness (optional — process alive but doing nothing)
        log_file = config.get("log_file")
        if log_file and os.path.exists(log_file):
            age = get_log_age(log_file)
            # If no log output for 2 hours and process should be active
            if age > 7200 and name not in ("wireproxy", "harbor-web", "flight-daemon"):
                if should_alert(state, f"stale:{name}"):
                    send_alert(f"{name} has had no log output for {age/3600:.1f}h — may be stuck")
                    mark_alerted(state, f"stale:{name}")

    save_state(state)

    if not issues:
        print(f"[{datetime.now().isoformat()}] All processes healthy")
    else:
        print(f"[{datetime.now().isoformat()}] Issues found: {len(issues)}")
        for i in issues:
            print(f"  - {i}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "daemon":
        # Run as daemon, check every 5 minutes
        print(f"Watchdog daemon started (PID {os.getpid()})")
        while True:
            try:
                run_check()
            except Exception as e:
                print(f"[ERROR] Watchdog check failed: {e}")
            time.sleep(300)  # 5 minutes
    else:
        # Single check
        run_check()
