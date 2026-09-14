#!/usr/bin/env python3
"""Pricing regression tests for cc_cost.py — stdlib only.

    python3 test_cc_cost.py

Each test writes a tiny synthetic transcript and checks the priced total against a
hand-computed figure, so a wrong rate or multiplier fails loudly instead of quietly
skewing the daily report.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import cc_cost as cc

TS = "2026-09-10T12:00:00.000Z"


def turn(model: str, uid: str, *, inp=0, read=0, w5=0, w1h=0, out=0, speed="standard") -> dict:
    return {
        "type": "assistant", "uuid": uid, "timestamp": TS, "sessionId": "sess", "cwd": "/p",
        "message": {"model": model, "usage": {
            "input_tokens": inp, "cache_read_input_tokens": read, "output_tokens": out,
            "cache_creation_input_tokens": w5 + w1h,
            "cache_creation": {"ephemeral_5m_input_tokens": w5, "ephemeral_1h_input_tokens": w1h},
            "speed": speed,
        }},
    }


class PricingTest(unittest.TestCase):
    def collect(self, *records: dict):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "s.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")
            return cc.collect(Path(d), None, None, False)

    def test_fable_5_1_rates_and_cheap_cache_reads(self):
        # 1M of each bucket makes the arithmetic legible:
        #   input  1M * $10        = 10.00
        #   reads  1M * $10 * .025 =  0.25   (Fable 5.1 cache reads are $0.25/MTok)
        #   w5     1M * $10 * 1.25 = 12.50
        #   w1h    1M * $10 * 2.00 = 20.00
        #   output 1M * $50        = 50.00
        total, by, unpriced, _ = self.collect(
            turn("claude-fable-5-1", "a", inp=1_000_000, read=1_000_000,
                 w5=1_000_000, w1h=1_000_000, out=1_000_000))
        self.assertAlmostEqual(total.cost, 92.75, places=6)
        self.assertEqual(by["model"]["claude-fable-5-1"].turns, 1)
        self.assertEqual(unpriced, {})

    def test_other_models_keep_default_cache_read_rate(self):
        # Opus 5: 1M reads * $5 * 0.10 = 0.50. Guards against the Fable override leaking.
        total, *_ = self.collect(turn("claude-opus-5", "b", read=1_000_000))
        self.assertAlmostEqual(total.cost, 0.50, places=6)
        # Fable 5 (not 5.1) stays at 0.1x: 1M * $10 * 0.10 = 1.00.
        total, *_ = self.collect(turn("claude-fable-5", "c", read=1_000_000))
        self.assertAlmostEqual(total.cost, 1.00, places=6)

    def test_fable_5_1_has_no_fast_mode_premium(self):
        # speed=fast is only priced differently for Opus 5 / 4.8; Fable 5.1 must not change.
        std, *_ = self.collect(turn("claude-fable-5-1", "d", out=1_000_000))
        fast, *_ = self.collect(turn("claude-fable-5-1", "e", out=1_000_000, speed="fast"))
        self.assertAlmostEqual(std.cost, 50.00, places=6)
        self.assertAlmostEqual(fast.cost, std.cost, places=6)

    def test_unknown_model_is_excluded_not_guessed(self):
        total, _, unpriced, _ = self.collect(turn("claude-nonexistent-9", "f", out=1_000_000))
        self.assertEqual(total.cost, 0.0)
        self.assertEqual(unpriced, {"claude-nonexistent-9": 1})


if __name__ == "__main__":
    unittest.main(verbosity=2)
