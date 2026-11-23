"""时段窗口判断（静默告警的基础）。"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from coilbot.watch import active_window, get_tz  # noqa: E402


class WindowTests(unittest.TestCase):
    def test_asia_window(self) -> None:
        # 00:30 UTC = 09:30 东京
        self.assertEqual(active_window(datetime(2026, 11, 23, 0, 30, tzinfo=timezone.utc)), "Asia")

    def test_london_window(self) -> None:
        # 08:30 UTC = 08:30 伦敦（11 月为 GMT）
        self.assertEqual(active_window(datetime(2026, 11, 23, 8, 30, tzinfo=timezone.utc)), "London")

    def test_nyse_window(self) -> None:
        # 14:30 UTC = 09:30 纽约（EST）
        self.assertEqual(active_window(datetime(2026, 11, 23, 14, 30, tzinfo=timezone.utc)), "NYSE")

    def test_outside_all_windows(self) -> None:
        # 03:00 UTC：东京 12:00、伦敦 03:00、纽约 22:00，都不在首小时
        self.assertIsNone(active_window(datetime(2026, 11, 23, 3, 0, tzinfo=timezone.utc)))

    def test_window_closes_after_one_hour(self) -> None:
        self.assertEqual(active_window(datetime(2026, 11, 23, 0, 59, tzinfo=timezone.utc)), "Asia")
        self.assertIsNone(active_window(datetime(2026, 11, 23, 1, 0, tzinfo=timezone.utc)))

    def test_tz_lookup_never_throws(self) -> None:
        tzinfo, exact = get_tz("America/New_York")
        self.assertIsNotNone(tzinfo)
        self.assertIn(exact, (True, False))
        tzinfo2, exact2 = get_tz("Nowhere/Unknown")
        self.assertIsNotNone(tzinfo2)


if __name__ == "__main__":
    unittest.main()
