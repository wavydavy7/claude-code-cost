#!/usr/bin/env python3
"""cc-cost — personal Claude Code spend, computed from local transcripts.

Reads ~/.claude/projects/**/*.jsonl (the transcripts Claude Code already writes)
and prices every assistant turn. No Anthropic admin key required — this is your
own usage only, not your org's.

Costs are ESTIMATES: token counts are exact (they come from the API's own usage
block), but prices are the public list rates hardcoded in PRICES below. If your
org has negotiated rates, or a subscription rather than API billing, the dollar
figures are "what this would cost at list price", not what anyone is billed.

Usage:
    python3 cc_cost.py                          # summary
    python3 cc_cost.py --by day
    python3 cc_cost.py --by project --since 2026-08-01
    python3 cc_cost.py --last-hours 24          # rolling window
    python3 cc_cost.py --by session --top 10
    python3 cc_cost.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

# --- Pricing -----------------------------------------------------------------
# USD per million tokens (input, output) at public list rates.
# Update when models ship; unknown models are reported separately, never guessed.
PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.00, 50.00),
    "claude-mythos-5-1": (10.00, 50.00),
    "claude-fable-5": (10.00, 50.00),
    "claude-mythos-5": (10.00, 50.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-opus-4-5": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
}

# Fast mode runs the same model at premium rates (Opus 5 / Opus 4.8 only).
FAST_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (10.00, 50.00),
    "claude-opus-4-8": (10.00, 50.00),
}

# Sonnet 5 introductory pricing, active through this date inclusive.
SONNET_5_INTRO = (2.00, 10.00)
SONNET_5_INTRO_UNTIL = date(2026, 8, 31)

# Cache multipliers applied to the model's base INPUT rate.
CACHE_READ_MULT = 0.10   # serving a cached prefix
# Fable 5.1 bills cache reads at $0.25/MTok (0.025x of its $10 input rate).
# Mythos 5.1 is left at the default: whether it shares the 0.025x rate is unconfirmed.
CACHE_READ_MULT_BY_MODEL: dict[str, float] = {
    "claude-fable-5-1": 0.025,
}
CACHE_WRITE_5M_MULT = 1.25
CACHE_WRITE_1H_MULT = 2.00


def rates(model: str, speed: str, when: date | None) -> tuple[float, float] | None:
    """Resolve (input, output) $/MTok for a model, honoring fast mode and intro pricing."""
    if speed == "fast" and model in FAST_PRICES:
        return FAST_PRICES[model]
    if model == "claude-sonnet-5" and when and when <= SONNET_5_INTRO_UNTIL:
        return SONNET_5_INTRO
    return PRICES.get(model)


class Bucket:
    __slots__ = ("cost", "turns", "inp", "out", "cache_read", "cache_write", "think")

    def __init__(self) -> None:
        self.cost = 0.0
        self.turns = 0
        self.inp = 0
        self.out = 0
        self.cache_read = 0
        self.cache_write = 0
        self.think = 0

    def add(self, cost: float, u: dict, out: int, think: int) -> None:
        self.cost += cost
        self.turns += 1
        self.inp += u["inp"]
        self.out += out
        self.cache_read += u["read"]
        self.cache_write += u["w5"] + u["w1h"]
        self.think += think


def parse_usage(usage: dict) -> dict:
    """Normalize a usage block into flat token counts, splitting cache writes by TTL."""
    creation = usage.get("cache_creation") or {}
    w5 = creation.get("ephemeral_5m_input_tokens")
    w1h = creation.get("ephemeral_1h_input_tokens")
    if w5 is None and w1h is None:
        # Older transcripts predate the per-TTL split; assume the 5m default.
        w5, w1h = usage.get("cache_creation_input_tokens", 0) or 0, 0
    return {
        "inp": usage.get("input_tokens", 0) or 0,
        "read": usage.get("cache_read_input_tokens", 0) or 0,
        "w5": w5 or 0,
        "w1h": w1h or 0,
    }


def price(u: dict, out: int, rate_in: float, rate_out: float, model: str = "") -> float:
    billable_in = (
        u["inp"]
        + u["read"] * CACHE_READ_MULT_BY_MODEL.get(model, CACHE_READ_MULT)
        + u["w5"] * CACHE_WRITE_5M_MULT
        + u["w1h"] * CACHE_WRITE_1H_MULT
    )
    return (billable_in * rate_in + out * rate_out) / 1_000_000


def project_name(path: Path, cwd: str | None) -> str:
    """Prefer the real cwd; fall back to the encoded directory name."""
    if cwd:
        return cwd
    return path.parent.name.replace("-", "/", 1).replace("-", "/")


def collect(root: Path, since: datetime | None, until: datetime | None, include_sidechains: bool):
    """Walk transcripts and bucket priced turns. `since`/`until` are tz-aware local datetimes."""
    by = {k: defaultdict(Bucket) for k in ("model", "project", "day", "session")}
    total = Bucket()
    unpriced: dict[str, int] = defaultdict(int)
    seen: set[str] = set()
    files = sorted(root.rglob("*.jsonl"))

    for f in files:
        try:
            handle = f.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") != "assistant":
                    continue
                if rec.get("isSidechain") and not include_sidechains:
                    continue

                uid = rec.get("uuid")
                if uid:
                    if uid in seen:
                        continue
                    seen.add(uid)

                msg = rec.get("message") or {}
                usage = msg.get("usage")
                model = msg.get("model")
                if not usage or not model:
                    continue

                ts = rec.get("timestamp") or ""
                try:
                    # Transcripts stamp UTC; bucket and window in local time.
                    when = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
                    day = when.date()
                except (ValueError, AttributeError, TypeError):
                    when, day = None, None
                if since and (when is None or when < since):
                    continue
                if until and (when is None or when > until):
                    continue

                r = rates(model, usage.get("speed", "standard"), day)
                if r is None:
                    unpriced[model] += 1
                    continue

                u = parse_usage(usage)
                out = usage.get("output_tokens", 0) or 0
                think = (usage.get("output_tokens_details") or {}).get("thinking_tokens", 0) or 0
                cost = price(u, out, *r, model=model)

                total.add(cost, u, out, think)
                by["model"][model].add(cost, u, out, think)
                by["project"][project_name(f, rec.get("cwd"))].add(cost, u, out, think)
                by["day"][str(day) if day else "unknown"].add(cost, u, out, think)
                by["session"][(rec.get("sessionId") or f.stem)[:8]].add(cost, u, out, think)

    return total, by, unpriced, len(files)


def fmt(n: int) -> str:
    for unit, div in (("B", 1_000_000_000), ("M", 1_000_000), ("K", 1_000)):
        if n >= div:
            return f"{n / div:.1f}{unit}"
    return str(n)


def table(title: str, buckets: dict[str, Bucket], total_cost: float, top: int | None) -> None:
    rows = sorted(buckets.items(), key=lambda kv: kv[1].cost, reverse=True)
    shown = rows[:top] if top else rows
    print(f"\n{title}")
    print(f"  {'':<38} {'cost':>9} {'share':>7} {'turns':>7} {'in':>8} {'out':>8} {'cached':>8}")
    for name, b in shown:
        share = (b.cost / total_cost * 100) if total_cost else 0
        label = name if len(name) <= 38 else "…" + name[-37:]
        print(
            f"  {label:<38} {b.cost:>8.2f}$ {share:>6.1f}% {b.turns:>7} "
            f"{fmt(b.inp):>8} {fmt(b.out):>8} {fmt(b.cache_read):>8}"
        )
    if top and len(rows) > top:
        rest = sum(b.cost for _, b in rows[top:])
        print(f"  {f'… {len(rows) - top} more':<38} {rest:>8.2f}$")


def main() -> int:
    p = argparse.ArgumentParser(description="Estimate personal Claude Code spend from local transcripts.")
    p.add_argument("--by", choices=["model", "project", "day", "session", "all"], default="all")
    p.add_argument("--since", help="YYYY-MM-DD or ISO datetime (inclusive)")
    p.add_argument("--until", help="YYYY-MM-DD or ISO datetime (inclusive)")
    p.add_argument("--last-hours", type=float,
                   help="rolling window ending now, e.g. 24 (overrides --since)")
    p.add_argument("--top", type=int, default=15, help="rows per table (0 = all)")
    p.add_argument("--sidechains", action="store_true", help="include subagent turns")
    p.add_argument("--json", action="store_true", dest="as_json")
    p.add_argument("--root", default=os.path.expanduser("~/.claude/projects"))
    a = p.parse_args()

    root = Path(a.root)
    if not root.is_dir():
        print(f"No transcripts at {root}", file=sys.stderr)
        return 1

    def bound(s: str | None, end_of_day: bool) -> datetime | None:
        """Parse a date or ISO datetime into a tz-aware local datetime."""
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            print(f"Could not parse {s!r} as a date or ISO datetime", file=sys.stderr)
            raise SystemExit(2)
        if dt.time() == datetime.min.time() and end_of_day and len(s) <= 10:
            dt = dt.replace(hour=23, minute=59, second=59)
        return dt.astimezone() if dt.tzinfo is None else dt

    if a.last_hours is not None:
        until = datetime.now().astimezone()
        since = until - timedelta(hours=a.last_hours)
    else:
        since, until = bound(a.since, False), bound(a.until, True)

    total, by, unpriced, nfiles = collect(root, since, until, a.sidechains)

    if a.as_json:
        print(json.dumps({
            "total_usd": round(total.cost, 4),
            "turns": total.turns,
            "tokens": {
                "input": total.inp, "output": total.out,
                "cache_read": total.cache_read, "cache_write": total.cache_write,
                "thinking": total.think,
            },
            "by": {
                dim: {k: round(v.cost, 4) for k, v in sorted(
                    b.items(), key=lambda kv: kv[1].cost, reverse=True)}
                for dim, b in by.items()
            },
            "unpriced_models": dict(unpriced),
            "note": "Estimate at public list rates; not a billing statement.",
        }, indent=2))
        return 0

    span = f" ({since or 'start'} → {until or 'now'})" if (since or until) else ""
    print(f"Claude Code usage{span} — {nfiles} transcript file(s), {total.turns} assistant turns")
    print(f"\n  Estimated cost:  ${total.cost:,.2f}   (public list rates, not a bill)")
    print(f"  Uncached input:  {fmt(total.inp):>9}")
    print(f"  Cache reads:     {fmt(total.cache_read):>9}  (billed at {CACHE_READ_MULT:g}x input; "
          f"{CACHE_READ_MULT_BY_MODEL['claude-fable-5-1']:g}x on claude-fable-5-1)")
    print(f"  Cache writes:    {fmt(total.cache_write):>9}")
    print(f"  Output:          {fmt(total.out):>9}  (incl. {fmt(total.think)} thinking)")
    prefix = total.inp + total.cache_read + total.cache_write
    if prefix:
        hit = total.cache_read / prefix * 100
        print(f"  Cache hit rate:  {hit:>8.1f}%  (reads / all prompt tokens)")

    dims = ["model", "project", "day", "session"] if a.by == "all" else [a.by]
    top = a.top or None
    titles = {"model": "By model", "project": "By project",
              "day": "By day", "session": "By session"}
    for d in dims:
        table(titles[d], by[d], total.cost, top)

    if unpriced:
        print("\nUnpriced models (no rate in PRICES — excluded from totals):")
        for m, n in sorted(unpriced.items(), key=lambda kv: -kv[1]):
            print(f"  {m}  ({n} turns)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
