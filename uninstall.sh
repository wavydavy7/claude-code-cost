#!/usr/bin/env bash
# Remove the scheduled job. Leaves your config and state in place;
# pass --purge to delete those too (this removes your Slack credentials).
set -euo pipefail

LABEL="com.claude-code-cost.daily"
CONF_HOME="${CC_COST_HOME:-$HOME/.claude/cc-cost}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ "$(uname -s)" == "Darwin" ]]; then
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Removed $LABEL"
else
    echo "Not macOS — remove the cron line for cc_cost_daily.py manually."
fi

if [[ "${1:-}" == "--purge" ]]; then
    rm -rf "$CONF_HOME"
    echo "Purged $CONF_HOME (config, state, log)"
else
    echo "Kept $CONF_HOME — pass --purge to delete config/state/log too"
fi
