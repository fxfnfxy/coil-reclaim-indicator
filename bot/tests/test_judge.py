"""规则引擎回归测试：python -m unittest discover -s bot/tests -v"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from coilbot.config import Config  # noqa: E402
from coilbot.judge import judge  # noqa: E402
from coilbot.models import parse_packet  # noqa: E402

SAMPLE_PATH = ROOT.parent / "examples" / "sample_packet.json"


def sample(**overrides) -> dict:
    payload = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    payload.update(overrides)
    return payload


def fresh_packet(**overrides):
    """用当前时间做一个新鲜的包（示例包是过去的时间，会被新鲜度否掉）。"""
    payload = sample(**overrides)
    payload["ts"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return parse_packet(payload)


class JudgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = Config()
        self.config.account.equity_usdt = 10_000

    def test_sample_packet_is_long(self) -> None:
        packet = fresh_packet()
        decision = judge(packet, self.config)
        self.assertEqual(decision.verdict, "LONG", decision.render())
        self.assertAlmostEqual(decision.plan["entry"], 100310.0)
        self.assertAlmostEqual(decision.plan["stop"], 100060.0)
        # 1R = entry + (entry - stop)
        self.assertAlmostEqual(decision.plan["take_profit_1r"], 100560.0)
        # 目标 100900 → (100900-100310)/250 = 2.36R
        self.assertAlmostEqual(decision.plan["target_r"], 2.36, places=2)
        # 0.5% of 10000 = 50 USDT 风险 / 250 每单位 = 0.2
        self.assertAlmostEqual(decision.plan["qty"], 0.2, places=6)

    def test_bias_none_skips(self) -> None:
        decision = judge(fresh_packet(bias="NONE", side="NONE"), self.config)
        self.assertEqual(decision.verdict, "SKIP")
        self.assertIn("方向", "".join(decision.failed_hard))

    def test_blocked_today_skips(self) -> None:
        decision = judge(fresh_packet(blocked_today=True), self.config)
        self.assertEqual(decision.verdict, "SKIP")
        self.assertIn("今天没被封", decision.failed_hard)

    def test_outside_window_skips(self) -> None:
        decision = judge(fresh_packet(session_live=False), self.config)
        self.assertEqual(decision.verdict, "SKIP")
        self.assertIn("首小时窗口", decision.failed_hard)

    def test_unknown_session_skips(self) -> None:
        decision = judge(fresh_packet(session="Sydney"), self.config)
        self.assertEqual(decision.verdict, "SKIP")
        self.assertIn("时段允许", decision.failed_hard)

    def test_dirty_zone_skips(self) -> None:
        decision = judge(fresh_packet(breakout_in_clean_zone=False), self.config)
        self.assertEqual(decision.verdict, "SKIP")
        self.assertIn("突破在干净区", decision.failed_hard)

    def test_wrong_stop_side_skips(self) -> None:
        # 做多但止损在进场上方 → 结构错误
        decision = judge(fresh_packet(stop=100400.0), self.config)
        self.assertEqual(decision.verdict, "SKIP")
        self.assertIn("止损在正确一侧", decision.failed_hard)

    def test_short_bias_mismatch_skips(self) -> None:
        decision = judge(fresh_packet(bias="BUY", side="SHORT",
                                      order_price=100060.0, stop=100310.0), self.config)
        self.assertEqual(decision.verdict, "SKIP")
        self.assertIn("不逆方向", decision.failed_hard)

    def test_target_too_close_skips(self) -> None:
        # 目标只有 0.4R
        decision = judge(fresh_packet(next_ceiling=100410.0, target=None), self.config)
        self.assertEqual(decision.verdict, "SKIP")
        self.assertIn("目标距离", decision.failed_hard)

    def test_stale_packet_skips(self) -> None:
        payload = sample(ts=(datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        decision = judge(parse_packet(payload), self.config)
        self.assertEqual(decision.verdict, "SKIP")
        self.assertIn("数据新鲜", decision.failed_hard)

    def test_entry_requires_rubber_band(self) -> None:
        payload = sample(
            event="ENTRY", status="OPEN", entry=100320.0, rubber_band=False,
            ts=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        decision = judge(parse_packet(payload), self.config)
        self.assertEqual(decision.verdict, "SKIP")
        self.assertIn("已经回踩", decision.failed_hard)

    def test_entry_with_rubber_band_passes(self) -> None:
        payload = sample(
            event="ENTRY", status="OPEN", entry=100320.0, rubber_band=True,
            ts=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        decision = judge(parse_packet(payload), self.config)
        self.assertEqual(decision.verdict, "LONG", decision.render())

    def test_cancelled_is_skip(self) -> None:
        decision = judge(fresh_packet(event="STOP_ORDER_CANCELLED", status="CANCELLED",
                                      note="没先回踩"), self.config)
        self.assertEqual(decision.verdict, "SKIP")

    def test_session_bias_reports_without_order(self) -> None:
        decision = judge(fresh_packet(event="SESSION_BIAS", status="FLAT"), self.config)
        self.assertEqual(decision.verdict, "SKIP")
        self.assertIn("只做多", "".join(decision.reasons))

    def test_result_is_rendered_readable(self) -> None:
        text = judge(fresh_packet(), self.config).render()
        for expected in ("LONG", "进场", "止损", "1R", "目标", "仓位"):
            self.assertIn(expected, text)


if __name__ == "__main__":
    unittest.main()
