"""纸面账户：跟随指标信号记账。

说明（重要）：
    指标的 ENTRY / EXIT 事件本身就带着成交与出场结果（entry / exit_price / result_r），
    所以纸面账户不需要自己撮合，只要按事件把仓位和 R 记下来、累计净值即可。
    这样纸面结果与 TradingView 上的标记严格一致，不会因为本地K线口径不同算偏。

    `execution.mode = "signal"` 时也只记账（用于统计），不下单。
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from .store import Store

LOGGER = logging.getLogger("coilbot.paper")


@dataclass(slots=True)
class PaperAccount:
    start_equity: float = 10_000.0

    def equity(self, store: Store) -> float:
        rows = store._query(  # noqa: SLF001 - 内部工具类，读同一份库
            "SELECT COALESCE(SUM(result_r * COALESCE(risk_amount,0)),0) AS pnl "
            "FROM trades WHERE mode='paper' AND status='closed'"
        )
        pnl = rows[0]["pnl"] if rows else 0.0
        return self.start_equity + float(pnl or 0.0)

    def summary(self, store: Store) -> dict:
        rows = store._query(
            "SELECT COUNT(*) AS n, COALESCE(SUM(result_r),0) AS r, "
            "COALESCE(SUM(CASE WHEN result_r>0 THEN 1 ELSE 0 END),0) AS wins, "
            "COALESCE(SUM(CASE WHEN result_r<0 THEN 1 ELSE 0 END),0) AS losses, "
            "COALESCE(SUM(result_r * COALESCE(risk_amount,0)),0) AS pnl "
            "FROM trades WHERE mode='paper' AND status='closed'"
        )
        row = rows[0]
        closed = int(row["n"] or 0)
        wins = int(row["wins"] or 0)
        return {
            "closed": closed,
            "wins": wins,
            "losses": int(row["losses"] or 0),
            "win_rate": (wins / closed * 100.0) if closed else 0.0,
            "total_r": float(row["r"] or 0.0),
            "pnl_usdt": float(row["pnl"] or 0.0),
            "equity": self.equity(store),
            "open": len(store.open_trades(mode="paper")),
        }

    def render(self, store: Store) -> str:
        data = self.summary(store)
        lines = [
            f"纸面权益 {data['equity']:.2f} USDT（累计 {data['pnl_usdt']:+.2f}）",
            f"已平 {data['closed']} 笔 · 胜 {data['wins']} 负 {data['losses']} · "
            f"胜率 {data['win_rate']:.1f}% · 合计 {data['total_r']:+.2f}R",
            f"在场 {data['open']} 笔",
        ]
        return "\n".join(lines)
