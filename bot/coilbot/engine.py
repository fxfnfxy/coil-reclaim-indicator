"""编排层：数据包 → 规则复核 → 记账/下单 → 通知 → 落库。

一次 handle() 就是一个完整周期。所有副作用都走这里，方便回放和审计。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .binance import BinanceFutures
from .config import Config, ensure_dirs
from .judge import judge
from .models import Decision, Packet, PacketError, parse_packet
from .notify import NotifyResult, Notifier
from .paper import PaperAccount
from .store import Store

LOGGER = logging.getLogger("coilbot.engine")

# 哪些事件要打扰人
NOTIFY_EVENTS = {
    "SESSION_BIAS": ("info", True),
    "STOP_ORDER_PLACED": ("signal", True),
    "STOP_ORDER_CANCELLED": ("info", True),
    "ENTRY": ("trade", True),
    "BREAKEVEN": ("warn", True),
    "TOO_FAST_TAKE_PROFIT": ("warn", True),
    "EXIT": ("trade", True),
    "BAR_CLOSE": ("info", False),
}


@dataclass(slots=True)
class HandleResult:
    stored: bool
    decision: Decision | None = None
    notify: NotifyResult | None = None
    error: str = ""
    notes: list[str] = field(default_factory=list)


class Engine:
    def __init__(self, config: Config, store: Store | None = None, notifier: Notifier | None = None) -> None:
        self.config = config
        ensure_dirs(config)
        self.store = store or Store(config.store.db_path)
        self.notifier = notifier or Notifier(config.notify)
        self.paper = PaperAccount(config.execution.paper_start_equity)
        self._lock = threading.RLock()
        self._binance: BinanceFutures | None = None
        if config.execution.mode == "live":
            self._binance = BinanceFutures(config.execution)

    # ------------------------------------------------------------ 入口
    def handle_raw(self, raw: Any, *, received_at: datetime | None = None) -> HandleResult:
        try:
            packet = parse_packet(raw, received_at=received_at)
        except PacketError as exc:
            LOGGER.warning("拒绝数据包：%s", exc)
            return HandleResult(stored=False, error=str(exc))

        decision = judge(packet, self.config)
        with self._lock:
            stored = self.store.ingest(packet, decision)
            if not stored:
                LOGGER.info("重发包，跳过：%s", packet.dedupe_key)
                return HandleResult(stored=False, decision=decision)

            notes = self._execute(packet, decision)
            notify = self._maybe_notify(packet, decision, notes)
        return HandleResult(stored=True, decision=decision, notify=notify, notes=notes)

    # ------------------------------------------------------------ 执行
    def _execute(self, packet: Packet, decision: Decision) -> list[str]:
        mode = self.config.execution.mode
        notes: list[str] = []
        plan = decision.plan or {}
        symbol = packet.base_symbol
        side = packet.order_side

        if packet.event == "STOP_ORDER_PLACED":
            if not decision.is_actionable:
                notes.append("未通过复核，不挂单")
                return notes
            order_id = ""
            if mode == "live" and self._binance is not None:
                result = self._binance.place_stop_entry(
                    symbol=symbol,
                    side=side,
                    stop_price=float(plan.get("entry") or 0),
                    qty=float(plan.get("qty") or 0),
                )
                notes.append(f"实盘挂单：{result.detail}")
                order_id = result.order_id
            self.store.open_trade(
                symbol=symbol,
                side=side,
                mode=mode if mode != "signal" else "signal",
                entry=plan.get("entry"),
                stop=plan.get("stop"),
                qty=plan.get("qty"),
                target=plan.get("target"),
                target_r=plan.get("target_r"),
                risk_amount=plan.get("risk_amount"),
                status="pending",
                note=packet.note or "突破挂单，等回踩",
                order_id=order_id or None,
                source_key=f"placed|{packet.dedupe_key}",
            )
            notes.append("已记录挂单")
            return notes

        if packet.event == "STOP_ORDER_CANCELLED":
            key = f"placed|{packet.tag}|{packet.symbol}|STOP_ORDER_PLACED|"
            for row in self.store.open_trades(mode=mode if mode != "signal" else "signal"):
                if row["source_key"].startswith(key) and row["status"] == "pending":
                    if mode == "live" and self._binance is not None and row["order_id"]:
                        cancel = self._binance.cancel_order(symbol, row["order_id"])
                        notes.append(f"实盘撤单：{cancel.detail}")
                    self.store.update_trade(
                        row["source_key"],
                        status="cancelled",
                        note=packet.note or "挂单作废",
                        closed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    )
                    notes.append("挂单已标记作废")
            return notes

        if packet.event == "ENTRY":
            order_id = ""
            if mode == "live" and self._binance is not None:
                result = self._binance.place_protective_stop(
                    symbol=symbol,
                    side=side,
                    stop_price=float(packet.stop or 0),
                    qty=float(packet.raw.get("qty") or plan.get("qty") or 0),
                )
                notes.append(f"实盘保护止损：{result.detail}")
                order_id = result.order_id
            quantity = float(packet.raw.get("qty") or plan.get("qty") or 0) or None
            # 先把同一品种的挂单升级成持仓，一笔交易只留一条记录
            pending = [r for r in self.store.open_trades() if r["symbol"] == symbol and r["status"] == "pending"]
            if pending:
                row = pending[0]
                self.store.update_trade(
                    row["source_key"],
                    status="open",
                    entry=packet.entry if packet.entry else row["entry"],
                    stop=packet.stop if packet.stop else row["stop"],
                    qty=quantity or row["qty"],
                    note=packet.note or "回踩后成交",
                )
                if order_id:
                    self.store.update_trade(row["source_key"], note=(packet.note or "回踩后成交") + f" · 止损单 {order_id}")
                notes.append("挂单已转为持仓")
                return notes
            self.store.open_trade(
                symbol=symbol,
                side=side,
                mode=mode if mode != "signal" else "signal",
                entry=packet.entry,
                stop=packet.stop,
                qty=quantity,
                target=packet.target,
                target_r=plan.get("target_r"),
                risk_amount=plan.get("risk_amount"),
                status="open",
                note=packet.note or "回踩后成交",
                order_id=order_id or None,
                source_key=f"entry|{packet.dedupe_key}",
            )
            notes.append("已记录成交（没找到对应挂单，单独记一笔）")
            return notes

        if packet.event == "BREAKEVEN":
            if mode == "live" and self._binance is not None:
                for row in self.store.open_trades(mode="live"):
                    if row["symbol"] == symbol and row["status"] == "open":
                        if row["order_id"]:
                            self._binance.cancel_order(symbol, row["order_id"])
                        if packet.entry:
                            self._binance.place_protective_stop(
                                symbol=symbol, side=row["side"], stop_price=packet.entry,
                                qty=row["qty"] or 0,
                            )
            for row in self.store.open_trades():
                if row["symbol"] == symbol and row["status"] == "open":
                    self.store.update_trade(row["source_key"], stop=packet.entry, note="保本：止损挪到进场价")
            notes.append("止损已挪到进场价")
            return notes

        if packet.event in ("TOO_FAST_TAKE_PROFIT", "EXIT"):
            result_r = packet.result_r
            rows = self.store.open_trades()
            # 只结真正成交的那笔；如果 ENTRY 事件丢了，退一步用 pending 兜底
            targets = [r for r in rows if r["symbol"] == symbol and r["status"] == "open"]
            if not targets:
                targets = [r for r in rows if r["symbol"] == symbol and r["status"] == "pending"][:1]
            for row in targets:
                if row["symbol"] != symbol:
                    continue
                if mode == "live" and self._binance is not None and packet.event == "EXIT" and row["qty"]:
                    close = self._binance.market_close(symbol, row["side"], row["qty"])
                    notes.append(f"实盘平仓：{close.detail}")
                self.store.update_trade(
                    row["source_key"],
                    status="closed",
                    exit_price=packet.exit_price,
                    result_r=result_r,
                    note=packet.note or packet.event,
                    closed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                )
                notes.append(f"记录结果 {result_r}R")
            return notes

        return notes

    # ------------------------------------------------------------ 通知
    def _maybe_notify(self, packet: Packet, decision: Decision, notes: list[str]) -> NotifyResult | None:
        level, enabled = NOTIFY_EVENTS.get(packet.event, ("info", False))
        if packet.event == "BAR_CLOSE":
            enabled = self.config.notify.send_bar_close
        if not enabled:
            return None

        title = _title(packet, decision)
        body = decision.render()
        if notes:
            body += "\n\n执行：" + "；".join(notes)
        result = self.notifier.send(title, body, level=level)
        if not result.ok:
            self.store.record_alert("error", title, body + f"\n通知失败：{result.summary()}")
        return result

    # ------------------------------------------------------------ 其它
    def replay_file(self, path: str | Path) -> HandleResult:
        import json

        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return self.handle_raw(payload)

    def status(self) -> dict[str, Any]:
        stats = self.store.stats()
        stats["mode"] = self.config.execution.mode
        stats["paper"] = self.paper.summary(self.store)
        stats["config_source"] = self.config.source_path or "默认值"
        return stats


def _title(packet: Packet, decision: Decision) -> str:
    label = {
        "SESSION_BIAS": "时段方向",
        "STOP_ORDER_PLACED": "挂单信号",
        "STOP_ORDER_CANCELLED": "挂单作废",
        "ENTRY": "成交",
        "BREAKEVEN": "保本",
        "TOO_FAST_TAKE_PROFIT": "走太快止盈",
        "EXIT": "出场",
        "BAR_CLOSE": "收盘背景",
    }.get(packet.event, packet.event)
    head = f"{decision.verdict} · {packet.base_symbol} · {label}"
    if packet.session:
        head += f" · {packet.session}"
    return head
