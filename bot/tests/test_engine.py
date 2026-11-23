"""端到端：包 → 决策 → 记账 → 去重。用临时目录，不联网。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from coilbot.config import Config  # noqa: E402
from coilbot.engine import Engine  # noqa: E402
from coilbot.notify import Delivery, NotifyResult  # noqa: E402

SAMPLE = ROOT.parent / "examples" / "sample_packet.json"


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, title: str, body: str, level: str = "info") -> NotifyResult:
        self.sent.append((title, body, level))
        return NotifyResult(deliveries=[Delivery("fake", True, "ok")])

    def test(self) -> NotifyResult:
        return self.send("test", "test")


class EngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        config = Config()
        config.execution.mode = "paper"
        config.judge.stale_minutes = 100_000
        config.store.db_path = str(base / "test.db")
        config.store.archive_dir = str(base / "packets")
        config.watch.state_dir = str(base)
        config.notify.log_only = False
        self.config = config
        self.notifier = FakeNotifier()
        self.engine = Engine(config, notifier=self.notifier)
        self.payload = json.loads(SAMPLE.read_text(encoding="utf-8"))
        self.payload["ts"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def tearDown(self) -> None:
        self.engine.store.close()
        self.tmp.cleanup()

    def test_placed_creates_pending_trade_and_notifies(self) -> None:
        result = self.engine.handle_raw(self.payload)
        self.assertTrue(result.stored)
        self.assertEqual(result.decision.verdict, "LONG")
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("挂单信号", self.notifier.sent[0][0])
        pending = self.engine.store.open_trades(mode="paper")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["status"], "pending")

    def test_duplicate_packet_is_ignored(self) -> None:
        first = self.engine.handle_raw(self.payload)
        second = self.engine.handle_raw(self.payload)
        self.assertTrue(first.stored)
        self.assertFalse(second.stored)
        self.assertEqual(len(self.notifier.sent), 1)

    def test_bad_schema_rejected(self) -> None:
        result = self.engine.handle_raw({"schema": "other/v1", "event": "ENTRY"})
        self.assertFalse(result.stored)
        self.assertIn("schema", result.error)

    def test_full_lifecycle_closes_trade(self) -> None:
        self.engine.handle_raw(self.payload)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entry = dict(self.payload, event="ENTRY", status="OPEN", entry=100_320.0,
                     rubber_band=True, ts=now)
        self.engine.handle_raw(entry)
        exit_payload = dict(self.payload, event="EXIT", status="CLOSED", entry=100_320.0,
                            rubber_band=True, exit_price=100_900.0, result_r=1.85,
                            note="到达4小时天花板", ts=now)
        self.engine.handle_raw(exit_payload)
        history = self.engine.store.trade_history(5)
        closed = [row for row in history if row["status"] == "closed"]
        self.assertTrue(closed)
        self.assertAlmostEqual(closed[0]["result_r"], 1.85)
        summary = self.engine.paper.summary(self.engine.store)
        self.assertEqual(summary["closed"], 1)
        self.assertGreater(summary["equity"], 10_000)

    def test_cancelled_marks_pending_cancelled(self) -> None:
        self.engine.handle_raw(self.payload)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cancel = dict(self.payload, event="STOP_ORDER_CANCELLED", status="CANCELLED",
                      note="6 根没成交", ts=now)
        self.engine.handle_raw(cancel)
        rows = self.engine.store.trade_history(5)
        self.assertEqual(rows[0]["status"], "cancelled")

    def test_bar_close_is_not_notified(self) -> None:
        payload = dict(self.payload, event="BAR_CLOSE", status="FLAT")
        result = self.engine.handle_raw(payload)
        self.assertTrue(result.stored)
        self.assertEqual(len(self.notifier.sent), 0)

    def test_status_shape(self) -> None:
        self.engine.handle_raw(self.payload)
        status = self.engine.status()
        self.assertEqual(status["packets"], 1)
        self.assertIn("paper", status)


if __name__ == "__main__":
    unittest.main()
