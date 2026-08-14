#!/usr/bin/env bash
# Install the daily Claude Code cost report as a scheduled job.
#
#   ./install.sh          # run daily at 17:00 local
#   ./install.sh 9        # run daily at 09:00 local
#
# Idempotent: re-running replaces the existing job.
set -euo pipefail

HOUR="${1:-17}"
if ! [[ "$HOUR" =~ ^[0-9]{1,2}$ ]] || (( HOUR > 23 )); then
    echo "error: hour must be 0-23, got '$HOUR'" >&2
    exit 1
fi

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.claude-code-cost.daily"
CONF_HOME="${CC_COST_HOME:-$HOME/.claude/cc-cost}"
LOG="$CONF_HOME/daily.log"

# System python3 on purpose: stable across Homebrew upgrades. Fall back to PATH.
PY=/usr/bin/python3
[[ -x "$PY" ]] || PY="$(command -v python3 || true)"
if [[ -z "$PY" ]]; then
    echo "error: no python3 found" >&2
    exit 1
fi
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' || {
    echo "error: $PY is $("$PY" -V 2>&1); need Python 3.9+" >&2
    exit 1
}

mkdir -p "$CONF_HOME"
chmod 700 "$CONF_HOME"

# Fail fast if the code itself is broken, before wiring up a scheduler.
"$PY" "$REPO/cc_cost_daily.py" --dry-run >/dev/null

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "Not macOS — launchd unavailable. Add this to your crontab instead:"
    echo
    echo "  $HOUR 0 * * * CC_COST_HOME=$CONF_HOME $PY $REPO/cc_cost_daily.py >> $LOG 2>&1"
    echo
    echo "(note: cron does NOT fire missed jobs after sleep; launchd does)"
    exit 0
fi

PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$(dirname "$PLIST")"

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>$PY</string>
        <string>$REPO/cc_cost_daily.py</string>
    </array>

    <key>EnvironmentVariables</key>
    <dict>
        <key>CC_COST_HOME</key>
        <string>$CONF_HOME</string>
    </dict>

    <!-- If the Mac is asleep at this time, launchd fires the job on wake. -->
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>$HOUR</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>

    <key>RunAtLoad</key>
    <false/>

    <key>StandardOutPath</key>
    <string>$LOG</string>
    <key>StandardErrorPath</key>
    <string>$LOG</string>

    <key>WorkingDirectory</key>
    <string>$HOME</string>

    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
PLIST_EOF

plutil -lint "$PLIST" >/dev/null

# Replace any existing job (bootout is a no-op error if not loaded).
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "Installed $LABEL — daily at $(printf '%02d' "$HOUR"):00 local"
echo "  code:   $REPO/cc_cost_daily.py"
echo "  config: $CONF_HOME"
echo "  log:    $LOG"

if [[ ! -f "$CONF_HOME/slack.json" ]]; then
    cat <<SETUP

NEXT STEP — Slack credentials are not configured yet, so runs will exit with
an error until you create $CONF_HOME/slack.json with ONE of:

  {"bot_token": "xoxb-...", "channel": "U0123ABC"}   # DMs you; needs chat:write
  {"webhook_url": "https://hooks.slack.com/services/..."}   # fixed channel

Then: chmod 600 $CONF_HOME/slack.json
Verify: $PY $REPO/cc_cost_daily.py --verify

Full setup steps (creating the Slack app, finding your member ID) are in the
README under "Slack credentials".
SETUP
fi
