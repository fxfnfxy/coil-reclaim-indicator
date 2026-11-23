"""仓位计算测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from coilbot.config import AccountConfig, InstrumentConfig  # noqa: E402
from coilbot.sizing import compute_target_r, size_position  # noqa: E402


class SizingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.account = AccountConfig(equity_usdt=10_000, default_risk_pct=0.5, max_notional_multiple=5.0)
        self.btc = InstrumentConfig(qty_step=0.001, min_qty=0.001, price_tick=0.1)

    def test_basic_long(self) -> None:
        result = size_position(
            symbol="BTCUSDT", side="LONG", entry=100_310, stop=100_060, target=100_900,
            risk_pct=0.5, account=self.account, instrument=self.btc,
        )
        self.assertTrue(result.ok, result.reason)
        self.assertAlmostEqual(result.risk_amount, 50.0)
        self.assertAlmostEqual(result.risk_per_unit, 250.0)
        self.assertAlmostEqual(result.qty, 0.2)
        self.assertAlmostEqual(result.target_r, 2.36, places=2)

    def test_short_side(self) -> None:
        result = size_position(
            symbol="BTCUSDT", side="SHORT", entry=100_060, stop=100_310, target=99_400,
            risk_pct=0.5, account=self.account, instrument=self.btc,
        )
        self.assertTrue(result.ok, result.reason)
        self.assertAlmostEqual(result.qty, 0.2)
        self.assertAlmostEqual(result.target_r, 2.64, places=2)

    def test_wrong_side_rejected(self) -> None:
        result = size_position(
            symbol="BTCUSDT", side="LONG", entry=100_060, stop=100_310, target=100_900,
            risk_pct=0.5, account=self.account, instrument=self.btc,
        )
        self.assertFalse(result.ok)
        self.assertIn("同一侧", result.reason)

    def test_too_wide_stop_rejected(self) -> None:
        result = size_position(
            symbol="BTCUSDT", side="LONG", entry=100_310, stop=90_000, target=120_000,
            risk_pct=0.1, account=self.account, instrument=self.btc,
        )
        self.assertFalse(result.ok)
        self.assertIn("止损太宽", result.reason)

    def test_step_rounding_is_down(self) -> None:
        # 0.1999 按 0.001 步长应向下取整到 0.199
        account = AccountConfig(equity_usdt=9_995, default_risk_pct=0.5)
        result = size_position(
            symbol="BTCUSDT", side="LONG", entry=100_310, stop=100_060, target=101_000,
            risk_pct=0.5, account=account, instrument=self.btc,
        )
        self.assertTrue(result.ok, result.reason)
        self.assertAlmostEqual(result.qty, 0.199, places=6)

    def test_notional_cap(self) -> None:
        account = AccountConfig(equity_usdt=1_000, default_risk_pct=2.0, max_notional_multiple=1.0)
        result = size_position(
            symbol="BTCUSDT", side="LONG", entry=100_310, stop=99_800, target=101_000,
            risk_pct=2.0, account=account, instrument=self.btc,
        )
        # 风险 20 USDT / 510 = 0.039 → 名义 3932 > 1000 → 应被压到 0.009
        self.assertTrue(result.ok)
        self.assertLessEqual(result.notional, 1_000 * 1.0 + 1e-6)
        self.assertTrue(result.notes)

    def test_inferred_step_warns(self) -> None:
        result = size_position(
            symbol="XYZUSDT", side="LONG", entry=12.345, stop=12.0, target=13.5,
            risk_pct=0.5, account=self.account, instrument=InstrumentConfig(),
        )
        self.assertTrue(result.ok)
        self.assertTrue(any("推断" in note or "推断" in result.reason for note in (result.notes or []) + [result.reason]))

    def test_cap_r_applied(self) -> None:
        result = size_position(
            symbol="BTCUSDT", side="LONG", entry=100_310, stop=100_060, target=110_000,
            risk_pct=0.5, account=self.account, instrument=self.btc, cap_r=3.0,
        )
        self.assertAlmostEqual(result.target_r, 3.0)
        self.assertTrue(any("3R" in note for note in result.notes))

    def test_compute_target_r(self) -> None:
        self.assertAlmostEqual(compute_target_r(100, 90, 120), 2.0)
        self.assertIsNone(compute_target_r(100, 100, 120))
        self.assertIsNone(compute_target_r(100, 90, None))


if __name__ == "__main__":
    unittest.main()
