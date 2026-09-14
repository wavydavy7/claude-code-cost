# claude-code-cost

Estimate what your Claude Code usage costs, from the transcripts Claude Code already
writes to disk. No Anthropic admin key, no org permissions — this reads **your own**
local data only.

Two pieces:

- **`cc_cost.py`** — an on-demand breakdown by model, project, day, or session.
- **`cc_cost_daily.py`** — a scheduled job that posts a trailing-24h + month-to-date
  summary to Slack.

## Why this exists

Anthropic's Admin API exposes org-wide Claude Code usage, but it needs an
organization-level admin key with the Usage Reporting scope — which most engineers
don't have, and which reports on the whole org rather than on you. Meanwhile every
Claude Code session writes a complete per-turn `usage` block to
`~/.claude/projects/**/*.jsonl`. That's enough to price your own usage exactly.

## Quick start

```bash
git clone <this repo> ~/claude-code-cost
cd ~/claude-code-cost
python3 cc_cost.py
```

No dependencies — standard library only, Python 3.9+.

```
Claude Code usage — 14 transcript file(s), 1240 assistant turns

  Estimated cost:  $284.50   (public list rates, not a bill)
  Uncached input:       3.5K
  Cache reads:        336.0M  (billed at 0.1x input)
  Cache writes:         9.4M
  Output:             890.0K  (incl. 360.0K thinking)
  Cache hit rate:      97.3%  (reads / all prompt tokens)

By model
                                              cost   share   turns       in      out   cached
  claude-opus-5                            284.50$  100.0%    1240     3.5K   890.0K   336.0M

By project
                                              cost   share   turns       in      out   cached
  ~/some-service                           201.70$   70.9%     620     2.4K   460.0K   259.0M
  ~/another-repo                             24.20$    8.5%     133      250   112.0K    21.0M
```

### Useful invocations

```bash
python3 cc_cost.py --by day                      # daily trend
python3 cc_cost.py --by project --top 5          # where it goes
python3 cc_cost.py --by session                  # which sessions were expensive
python3 cc_cost.py --last-hours 24               # rolling window
python3 cc_cost.py --since 2026-08-01            # month to date
python3 cc_cost.py --sidechains                  # include subagent turns
python3 cc_cost.py --json | jq .                 # machine-readable
```

Every user reads only their own `~/.claude/projects`; there is no shared state, no server,
and no account to sign into for the cost report itself. The only credential anywhere in
this tool is your personal Slack token, used solely to deliver the message.

## Daily Slack report

```bash
./install.sh          # daily at 17:00 local
./install.sh 9        # daily at 09:00 local
```

On macOS this installs a `launchd` LaunchAgent — chosen over cron because it survives
reboots **and fires a missed run when the Mac wakes**, so a closed lid doesn't silently
skip a day. On Linux the installer prints the equivalent crontab line instead (with the
caveat that cron won't backfill a missed run).

The message:

```
*Claude Code — Fri Aug 14*
• Last 24h: *$140.00*  (550 turns, 425.0K out / 165.0M cached)
• August month-to-date: *$284.50*  (1240 turns)
• Top projects:
    ‣ `~/some-service` — $93.10
    ‣ `~/another-repo` — $11.70
_Estimate at public list rates — not a billing statement._
```

### Slack credentials

Each user supplies their own — there is no shared account and nothing is baked into the
repo. Pick one of the two options below, then verify:

```bash
python3 cc_cost_daily.py --verify
```

That validates the token against Slack, warns if the file is group/world-readable, and
sends one test message, so you find out now rather than at 17:00.

> **Heads up:** many workspaces require admin approval to install a Slack app. If your
> org restricts this, the webhook option is usually easier to get approved than a bot
> token — or ask an admin to install it once and share the webhook.

#### Option A — bot token (DMs you)

1. <https://api.slack.com/apps> → **Create New App** → **From scratch**, pick your workspace.
2. **OAuth & Permissions** → **Bot Token Scopes** → add `chat:write`.
   (If DMs later fail with an error about opening a conversation, add `im:write` too.)
3. **Install to Workspace** → **Allow**.
4. Copy the **Bot User OAuth Token** — it starts `xoxb-`.
5. Get your own member ID: in Slack, click your avatar → **Profile** → **⋮** →
   **Copy member ID**. It looks like `U0123ABC456`.

```bash
mkdir -p ~/.claude/cc-cost
cat > ~/.claude/cc-cost/slack.json <<'JSON'
{"bot_token": "xoxb-YOUR-TOKEN", "channel": "U0YOUR-MEMBER-ID"}
JSON
chmod 600 ~/.claude/cc-cost/slack.json
python3 cc_cost_daily.py --verify
```

#### Option B — incoming webhook (fixed channel)

Fewer steps, but a webhook posts to **one preset channel** and cannot DM you.

1. Same app → **Incoming Webhooks** → toggle **On**.
2. **Add New Webhook to Workspace** → choose the channel → **Allow**.
3. Copy the URL.

```bash
mkdir -p ~/.claude/cc-cost
echo '{"webhook_url": "https://hooks.slack.com/services/YOUR/WEBHOOK/URL"}' \
  > ~/.claude/cc-cost/slack.json
chmod 600 ~/.claude/cc-cost/slack.json
python3 cc_cost_daily.py --verify
```

#### Sharing one channel across a team

If several people point at the **same webhook**, each message is prefixed with the sender
so they're distinguishable — `$USER` by default, or set `label` explicitly:

```json
{"webhook_url": "https://hooks.slack.com/services/...", "label": "Davy (laptop)"}
```

Bot-token DMs skip the prefix, since you already know a DM is yours.

Credentials can also come from the environment (`SLACK_WEBHOOK_URL`, or
`SLACK_BOT_TOKEN` + `SLACK_CHANNEL`) if you'd rather not write a file — but note that
`launchd` and `cron` run with a minimal environment, so the file is more reliable for the
scheduled job.

Until credentials exist the job runs and exits with a message naming exactly what to
create; it does not fail silently. Errors land in `$CC_COST_HOME/daily.log`.

**Credentials and state deliberately live outside this checkout** (in `$CC_COST_HOME`,
default `~/.claude/cc-cost/`) so a Slack token can't end up in git. `.gitignore` covers
the same filenames as a second layer.

### Removing it

```bash
./uninstall.sh            # remove the job, keep config
./uninstall.sh --purge    # also delete config, state, and log
```

## How the costing works

Token counts are **exact** — they come from the API's own `usage` block on each assistant
turn. Prices are the public list rates in the `PRICES` dict at the top of `cc_cost.py`.

Three details that a naive implementation gets wrong:

**Cache tiers are priced separately.** Cache reads bill at 0.1× the base input rate
(**0.025×** on Claude Fable 5.1, whose cache reads are $0.25/MTok), 5-minute cache writes
at 1.25×, and 1-hour cache writes at **2×**. Transcripts split these out per turn:

```json
"cache_creation": {"ephemeral_1h_input_tokens": 8639, "ephemeral_5m_input_tokens": 0}
```

Lumping all cache creation together at 1.25× understates a 1h-TTL workload by roughly 40%
of its cache-write cost. For a heavy session that's real money.

**Fast mode is a different rate.** `speed: "fast"` on Opus-tier models bills at $10/$50
per MTok rather than $5/$25.

**Unknown models are excluded, never guessed.** A model with no entry in `PRICES` is
counted separately and reported in an "Unpriced models" section, so a silently-wrong
total is impossible. (`<synthetic>`, Claude Code's placeholder for local non-API messages,
lands here — correctly, since it costs nothing.)

Turns are deduplicated by UUID, and timestamps are converted to local time for day
bucketing and rolling windows.

## Monthly totals

The daily job reports month-to-date **recomputed from transcripts on every run**, rather
than accumulating into a stored counter. This means a missed run leaves no gap and a
double run double-counts nothing, and the "monthly reset" is inherent — on the 1st the
window simply starts at the 1st.

`$CC_COST_HOME/state.json` keeps a run log and archives each completed month, but it's a
record, not a source of truth. Deleting it loses history, not accuracy.

## Caveats worth knowing

- **These are list-price estimates, not a bill.** If your org has negotiated API rates, or
  you're on a subscription rather than API billing, treat the figure as "what this would
  cost at list price." It is useful for *relative* comparison — which project, which day,
  which session — regardless.
- **The price table goes stale.** It's one dict at the top of `cc_cost.py`; update it as
  models ship, and run `python3 test_cc_cost.py` afterwards. PRs welcome.
- **Local-only.** Work done on another machine, in the web app, or in an IDE extension
  elsewhere won't appear.
- **Subagent turns are excluded by default** (`isSidechain`). Workflow- and agent-heavy
  sessions can have most of their spend there — pass `--sidechains` for the true total.
  The daily Slack report currently excludes them.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `CC_COST_HOME` | `~/.claude/cc-cost` | Config, state, and log location |
| `CC_COST_PROJECTS` | `~/.claude/projects` | Transcript root |
| `CC_COST_WINDOW_HOURS` | `24` | Daily report window |

`slack.json` keys: `bot_token` + `channel`, or `webhook_url`; optional `label` to name the
sender in a shared channel.
