#!/usr/bin/env python3
"""The memory report reader flags tight stacks and reports unused heap."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/bench"))
sys.path.insert(0, str(ROOT / "tools/dictionaries"))
import run_memory_report as memory

SAMPLE = {
    "version": 1, "uptime_ms": 90500,
    "heap": {"ok": 1, "size": 32768, "allocated": 1200, "free": 31568, "max_allocated": 6144},
    "stacks": [
        {"name": "engine", "size": 8192, "used": 7000},
        {"name": "javelin_web_hid", "size": 4096, "used": 1000},
        {"name": "ISR", "size": 2048, "used": 900},
    ],
}


class MemoryReportTest(unittest.TestCase):
    def test_tightest_stack_first_and_flagged(self):
        rows, heap = memory.analyse(SAMPLE, warn_free=25.0)
        self.assertEqual([row["name"] for row in rows], ["engine", "ISR", "javelin_web_hid"])
        self.assertTrue(rows[0]["warn"] and not rows[1]["warn"])
        self.assertEqual(rows[0]["free"], 1192)
        self.assertAlmostEqual(heap["peak_pct"], 18.75)
        self.assertEqual(heap["never_used"], 32768 - 6144)

    def test_render_mentions_the_findings(self):
        text = memory.render(SAMPLE)
        self.assertIn("<-- tight", text)
        self.assertIn("26624 B never used", text)

    def test_libc_heap_is_reported_when_present(self):
        report = {**SAMPLE, "version": 2,
                  "libc": {"ok": 1, "size": 19968, "allocated": 4000, "free": 15900, "max_allocated": 9984}}
        summary = memory.libc_summary(report)
        self.assertAlmostEqual(summary["peak_pct"], 50.0)
        self.assertEqual(summary["never_used"], 19968 - 9984)
        text = memory.render(report)
        self.assertIn("libc heap (the engine's malloc): 19968 B arena", text)
        self.assertIn("9984 B never used", text)

    def test_libc_heap_failure_and_absence_are_reported_not_invented(self):
        failed = memory.render({**SAMPLE, "version": 2, "libc": {"ok": 0, "size": 19968}})
        self.assertIn("libc heap: not reported", failed)
        self.assertIsNone(memory.libc_summary({**SAMPLE, "libc": {"ok": 0}}))
        self.assertIn("older than report version 2", memory.render(SAMPLE))

    def test_missing_heap_statistics_are_reported_not_invented(self):
        text = memory.render({**SAMPLE, "heap": {"ok": 0, "size": 32768}})
        self.assertIn("statistics unavailable", text)


if __name__ == "__main__":
    unittest.main()
