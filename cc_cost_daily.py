#!/usr/bin/env python3
"""cc_cost_daily — post a daily Claude Code cost summary to Slack.

Reports the trailing 24 hours plus month-to-date. Both figures are computed
directly from local transcripts on every run, so the job is idempotent: a
missed run leaves no gap in the monthly total, and a double run double-counts
nothing. The monthly total "resets" simply because the month-to-date window
starts at the 1st.

State and credentials live OUTSIDE this checkout (default ~/.claude/cc-cost/)
so a Slack token can never land in git. Override with CC_COST_HOME.

Slack credentials (first match wins):
  1. $CC_COST_HOME/slack.json  — {"webhook_url": "..."}
                              or {"bot_token": "xoxb-...", "channel": "..."}
  2. env SLACK_WEBHOOK_URL
  3. env SLACK_BOT_TOKEN + SLACK_CHANNEL

Usage:
    python3 cc_cost_daily.py --dry-run     # print the message, send nothing
    python3 cc_cost_daily.py               # send to Slack
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cc_cost as cc

# Keep mutable state and secrets out of the checkout.
HOME = Path(os.environ.get("CC_COST_HOME", os.path.expanduser("~/.claude/cc-cost")))
STATE = HOME / "state.json"
SLACK_CONF = HOME / "slack.json"
ROOT = Path(os.environ.get("CC_COST_PROJECTS", os.path.expanduser("~/.claude/projects")))
WINDOW_HOURS = float(os.environ.get("CC_COST_WINDOW_HOURS", "24"))


# --- Slack -------------------------------------------------------------------

def slack_config() -> dict:
    if SLACK_CONF.is_file():
        try:
            conf = json.loads(SLACK_CONF.read_text())
            if conf.get("webhook_url") or conf.get("bot_token"):
                return conf
        except json.JSONDecodeError as e:
            raise SystemExit(f"{SLACK_CONF} is not valid JSON: {e}")
    if os.environ.get("SLACK_WEBHOOK_URL"):
        return {"webhook_url": os.environ["SLACK_WEBHOOK_URL"]}
    if os.environ.get("SLACK_BOT_TOKEN"):
        return {"bot_token": os.environ["SLACK_BOT_TOKEN"],
                "channel": os.environ.get("SLACK_CHANNEL", "")}
    return {}


def check_perms() -> str | None:
    """Warn if the credentials file is readable by anyone but the owner."""
    if not SLACK_CONF.is_file():
        return None
    mode = SLACK_CONF.stat().st_mode & 0o077
    if mode:
        return (f"{SLACK_CONF} is group/world-readable (mode "
                f"{SLACK_CONF.stat().st_mode & 0o777:o}). Run: chmod 600 {SLACK_CONF}")
    return None


def post(url: str, payload: dict, headers: dict) -> tuple[int, str]:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json; charset=utf-8", **headers},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        return 0, f"network error: {e.reason}"


def send(text: str, conf: dict) -> None:
    if conf.get("webhook_url"):
        status, body = post(conf["webhook_url"], {"text": text}, {})
        if status != 200:
            raise SystemExit(f"Slack webhook failed ({status}): {body[:300]}")
        return

    if conf.get("bot_token"):
        channel = conf.get("channel")
        if not channel:
            raise SystemExit("bot_token is set but 'channel' is missing "
                             "(your own user ID, like U0123ABC, self-DMs you)")
        status, body = post(
            "https://slack.com/api/chat.postMessage",
            {"channel": channel, "text": text},
            {"Authorization": f"Bearer {conf['bot_token']}"},
        )
        # chat.postMessage returns HTTP 200 even on logical failure.
        try:
            parsed = json.loads(body)
            ok, err = parsed.get("ok", False), parsed.get("error", "")
        except json.JSONDecodeError:
            ok, err = False, body[:200]
        if status != 200 or not ok:
            raise SystemExit(f"Slack chat.postMessage failed: {err or body[:300]}")
        return

    raise SystemExit(
        f"No Slack credentials found. Create {SLACK_CONF} with either\n"
        '  {"webhook_url": "https://hooks.slack.com/services/..."}\n'
        '  {"bot_token": "xoxb-...", "channel": "U0123ABC"}\n'
        "then chmod 600 it. Or run with --dry-run to preview."
    )


# --- Report ------------------------------------------------------------------

def money(x: float) -> str:
    return f"${x:,.2f}"


def who(conf: dict) -> str:
    """Identify the sender when several people post into one shared channel.

    A bot DM needs no attribution — you know it's you. A webhook posts to a shared
    channel, so without this every teammate's report looks identical.
    """
    if conf.get("label"):
        return f"{conf['label']} · "
    if conf.get("webhook_url"):
        return f"{os.environ.get('USER') or os.environ.get('LOGNAME') or 'unknown'} · "
    return ""


def build(now: datetime, conf: dict | None = None) -> tuple[str, dict]:
    conf = conf or {}
    day_start = now - timedelta(hours=WINDOW_HOURS)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    day, day_by, day_unpriced, _ = cc.collect(ROOT, day_start, now, False)
    month, _, _, _ = cc.collect(ROOT, month_start, now, False)

    top = sorted(day_by["project"].items(), key=lambda kv: kv[1].cost, reverse=True)[:3]
    lines = [
        f"*Claude Code — {who(conf)}{now:%a %b %-d}*",
        f"• Last {WINDOW_HOURS:g}h: *{money(day.cost)}*  ({day.turns} turns, "
        f"{cc.fmt(day.out)} out / {cc.fmt(day.cache_read)} cached)",
        f"• {now:%B} month-to-date: *{money(month.cost)}*  ({month.turns} turns)",
    ]
    if top and day.cost > 0:
        lines.append("• Top projects:")
        for name, b in top:
            short = name.replace(os.path.expanduser("~"), "~")
            if len(short) > 44:
                short = "…" + short[-43:]
            lines.append(f"    ‣ `{short}` — {money(b.cost)}")
    if day.cost == 0:
        lines.append("• _No recorded usage in the window._")
    if day_unpriced:
        lines.append(f"• :warning: Unpriced model(s) excluded: {', '.join(sorted(day_unpriced))}")
    lines.append("_Estimate at public list rates — not a billing statement._")

    record = {
        "ran_at": now.isoformat(timespec="seconds"),
        "window_hours": WINDOW_HOURS,
        "window_usd": round(day.cost, 4),
        "window_turns": day.turns,
        "month": f"{now:%Y-%m}",
        "month_to_date_usd": round(month.cost, 4),
        "month_turns": month.turns,
    }
    return "\n".join(lines), record


def save(record: dict) -> None:
    """Append the run to state, archiving prior months. State is a log, not a source of truth."""
    HOME.mkdir(parents=True, exist_ok=True)
    try:
        state = json.loads(STATE.read_text()) if STATE.is_file() else {}
    except json.JSONDecodeError:
        state = {}
    if state.get("month") and state["month"] != record["month"]:
        state.setdefault("history", {})[state["month"]] = {
            "final_usd": state.get("month_to_date_usd"),
            "turns": state.get("month_turns"),
        }
        state["runs"] = []
    state["month"] = record["month"]
    state["month_to_date_usd"] = record["month_to_date_usd"]
    state["month_turns"] = record["month_turns"]
    runs = [r for r in state.get("runs", []) if r["ran_at"][:10] != record["ran_at"][:10]]
    state["runs"] = (runs + [record])[-40:]
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE)  # atomic — a killed run never leaves truncated state


def verify() -> int:
    """Check credentials end to end and send one test message. Run this after setup."""
    conf = slack_config()
    if not conf:
        print(f"✗ No credentials found.\n"
              f"  Create {SLACK_CONF} — see the README's 'Slack credentials' section.")
        return 1

    source = "config file" if SLACK_CONF.is_file() else "environment"
    warn = check_perms()
    if warn:
        print(f"⚠ {warn}")

    if conf.get("bot_token"):
        print(f"→ Found bot_token in {source}; checking with auth.test …")
        status, body = post("https://slack.com/api/auth.test", {},
                            {"Authorization": f"Bearer {conf['bot_token']}"})
        try:
            r = json.loads(body)
        except json.JSONDecodeError:
            print(f"✗ Unreadable response from Slack: {body[:200]}")
            return 1
        if not r.get("ok"):
            hint = {
                "invalid_auth": "the token is wrong, revoked, or not a bot token",
                "account_inactive": "the app was uninstalled from the workspace",
                "token_revoked": "the token was revoked — reinstall the app",
            }.get(r.get("error", ""), "see https://api.slack.com/methods/auth.test")
            print(f"✗ Token rejected: {r.get('error')} — {hint}")
            return 1
        print(f"✓ Token valid — workspace '{r.get('team')}', bot '{r.get('user')}'")
        if not conf.get("channel"):
            print("✗ 'channel' is missing. Use your own member ID (Slack profile → "
                  "⋮ → Copy member ID) to have it DM you.")
            return 1
        print(f"→ Sending a test message to {conf['channel']} …")
    else:
        print(f"→ Found webhook_url in {source}; sending a test message …")

    try:
        send(f":white_check_mark: `claude-code-cost` is wired up — "
             f"this is a test message from {os.uname().nodename}.", conf)
    except SystemExit as e:
        print(f"✗ {e}")
        if conf.get("bot_token"):
            print("  If this says 'channel_not_found', the value in 'channel' isn't a "
                  "member/channel ID.\n"
                  "  If it says 'not_in_channel', invite the bot to that channel first.")
        return 1

    print("✓ Test message sent. You're set — the scheduled run will work.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Post daily Claude Code cost to Slack.")
    p.add_argument("--dry-run", action="store_true", help="print the message, send nothing")
    p.add_argument("--verify", action="store_true",
                   help="check credentials and send one test message")
    p.add_argument("--no-save", action="store_true", help="skip writing the state file")
    a = p.parse_args()

    if a.verify:
        return verify()

    if not ROOT.is_dir():
        print(f"No transcripts at {ROOT}", file=sys.stderr)
        return 1

    now = datetime.now().astimezone()
    conf = slack_config()
    text, record = build(now, conf)

    if a.dry_run:
        print(text)
        print("\n--- state record ---")
        print(json.dumps(record, indent=2))
        return 0

    warn = check_perms()
    if warn:
        print(f"warning: {warn}", file=sys.stderr)

    send(text, conf)
    if not a.no_save:
        save(record)
    print(f"[{now:%Y-%m-%d %H:%M}] sent — {WINDOW_HOURS:g}h {money(record['window_usd'])}, "
          f"MTD {money(record['month_to_date_usd'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
