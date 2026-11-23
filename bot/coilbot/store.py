"""SQLite 持久化：数据包、决策、交易、心跳、运行统计。

24/7 运行的关键是「重启之后还知道刚才发生过什么」：
  - packets 表按 dedupe_key 幂等，TradingView 重发不会重复开单
  - decisions 跟着包一起存，事后可以逐条回放审计
  - kv 表存最后心跳、最后收包时间、上次日报日期
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import Decision, Packet

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS packets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key   TEXT NOT NULL UNIQUE,
    tag          TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    timeframe    TEXT NOT NULL,
    event        TEXT NOT NULL,
    kline_ts     TEXT NOT NULL,
    session      TEXT NOT NULL,
    bias         TEXT NOT NULL,
    status       TEXT NOT NULL,
    verdict      TEXT NOT NULL,
    price        REAL,
    raw_json     TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    packet_ts    TEXT,
    received_at  TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_packets_created ON packets(created_at);
CREATE INDEX IF NOT EXISTS idx_packets_symbol ON packets(symbol, created_at);
CREATE INDEX IF NOT EXISTS idx_packets_event ON packets(event);

CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    side        TEXT NOT NULL,
    mode        TEXT NOT NULL,             -- signal | paper | live
    entry       REAL,
    stop        REAL,
    qty         REAL,
    target      REAL,
    target_r    REAL,
    risk_amount REAL,
    status      TEXT NOT NULL,             -- pending | open | closed | cancelled
    opened_at   TEXT NOT NULL,
    closed_at   TEXT,
    exit_price  REAL,
    result_r    REAL,
    note        TEXT,
    order_id    TEXT,
    source_key  TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status, opened_at);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    level      TEXT NOT NULL,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(stamp: datetime) -> str:
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA_SQL)
            self._conn.commit()

    # ------------------------------------------------------------ 基础
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _exec(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._conn.execute(sql, tuple(args))
            self._conn.commit()
            return cursor

    def _query(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(args)))

    # ------------------------------------------------------------ kv
    def set_kv(self, key: str, value: str) -> None:
        self._exec(
            "INSERT INTO kv(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, _iso(_utc_now())),
        )

    def get_kv(self, key: str, default: str | None = None) -> str | None:
        rows = self._query("SELECT value FROM kv WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def touch(self, key: str) -> None:
        self.set_kv(key, _iso(_utc_now()))

    def get_kv_dt(self, key: str) -> datetime | None:
        raw = self.get_kv(key)
        if not raw:
            return None
        try:
            return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    # ------------------------------------------------------------ 包与决策
    def ingest(self, packet: Packet, decision: Decision) -> bool:
        """存包 + 决策。返回 False 表示这是重发包（已存在）。"""
        payload = (
            packet.dedupe_key,
            packet.tag,
            packet.symbol,
            packet.timeframe,
            packet.event,
            packet.ts,
            packet.session,
            packet.bias,
            packet.status,
            decision.verdict,
            packet.price,
            json.dumps(packet.raw, ensure_ascii=False),
            json.dumps(
                {
                    "verdict": decision.verdict,
                    "checks": [
                        {"name": c.name, "ok": c.ok, "detail": c.detail, "hard": c.hard}
                        for c in decision.checks
                    ],
                    "reasons": decision.reasons,
                    "plan": decision.plan,
                },
                ensure_ascii=False,
            ),
            packet.ts,
            packet.received_at,
            _iso(_utc_now()),
        )
        try:
            self._exec(
                "INSERT INTO packets(dedupe_key,tag,symbol,timeframe,event,kline_ts,session,bias,status,"
                "verdict,price,raw_json,decision_json,packet_ts,received_at,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                payload,
            )
        except sqlite3.IntegrityError:
            return False
        self.touch("last_packet_at")
        return True

    def recent_packets(self, limit: int = 20) -> list[sqlite3.Row]:
        return self._query("SELECT * FROM packets ORDER BY id DESC LIMIT ?", (limit,))

    def report_window(self, since: datetime) -> dict[str, Any]:
        rows = self._query(
            "SELECT verdict, COUNT(*) AS n FROM packets WHERE created_at >= ? GROUP BY verdict",
            (_iso(since),),
        )
        by_verdict = {row["verdict"]: row["n"] for row in rows}
        events = self._query(
            "SELECT event, COUNT(*) AS n FROM packets WHERE created_at >= ? GROUP BY event",
            (_iso(since),),
        )
        closed = self._query(
            "SELECT COUNT(*) AS n, COALESCE(SUM(result_r),0) AS r, "
            "COALESCE(SUM(CASE WHEN result_r>0 THEN 1 ELSE 0 END),0) AS wins "
            "FROM trades WHERE mode='paper' AND status='closed' AND closed_at >= ?",
            (_iso(since),),
        )
        symbols = self._query(
            "SELECT symbol, COUNT(*) AS n FROM packets WHERE created_at >= ? GROUP BY symbol "
            "ORDER BY n DESC LIMIT 8",
            (_iso(since),),
        )
        return {
            "by_verdict": by_verdict,
            "by_event": {row["event"]: row["n"] for row in events},
            "symbols": [(row["symbol"], row["n"]) for row in symbols],
            "paper_closed": closed[0]["n"],
            "paper_r": closed[0]["r"],
            "paper_wins": closed[0]["wins"],
        }

    # ------------------------------------------------------------ 交易
    def open_trade(
        self,
        *,
        symbol: str,
        side: str,
        mode: str,
        entry: float | None,
        stop: float | None,
        qty: float | None,
        target: float | None,
        target_r: float | None,
        risk_amount: float | None,
        status: str,
        note: str,
        order_id: str | None,
        source_key: str,
    ) -> int | None:
        try:
            cursor = self._exec(
                "INSERT INTO trades(symbol,side,mode,entry,stop,qty,target,target_r,risk_amount,"
                "status,opened_at,note,order_id,source_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    symbol, side, mode, entry, stop, qty, target, target_r, risk_amount,
                    status, _iso(_utc_now()), note, order_id, source_key,
                ),
            )
        except sqlite3.IntegrityError:
            return None
        return int(cursor.lastrowid or 0)

    def update_trade(self, source_key: str, **fields: Any) -> None:
        if not fields:
            return
        allowed = {"status", "entry", "stop", "qty", "target", "target_r", "exit_price", "result_r", "note", "closed_at"}
        assignments = []
        args: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            assignments.append(f"{key}=?")
            args.append(value)
        if not assignments:
            return
        args.append(source_key)
        self._exec(f"UPDATE trades SET {', '.join(assignments)} WHERE source_key=?", args)

    def open_trades(self, mode: str | None = None) -> list[sqlite3.Row]:
        if mode:
            return self._query(
                "SELECT * FROM trades WHERE status IN ('open','pending') AND mode=? ORDER BY id", (mode,)
            )
        return self._query("SELECT * FROM trades WHERE status IN ('open','pending') ORDER BY id")

    def trade_history(self, limit: int = 20) -> list[sqlite3.Row]:
        return self._query("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))

    # ------------------------------------------------------------ 告警
    def record_alert(self, level: str, title: str, body: str) -> None:
        self._exec(
            "INSERT INTO alerts(level,title,body,created_at) VALUES(?,?,?,?)",
            (level, title, body, _iso(_utc_now())),
        )

    # ------------------------------------------------------------ 维护
    def prune(self, retention_days: int) -> int:
        if retention_days <= 0:
            return 0
        cutoff = _iso(_utc_now() - timedelta(days=retention_days))
        cursor = self._exec("DELETE FROM packets WHERE created_at < ?", (cutoff,))
        self._exec("DELETE FROM alerts WHERE created_at < ?", (cutoff,))
        return cursor.rowcount or 0

    def stats(self) -> dict[str, Any]:
        counts = self._query("SELECT COUNT(*) AS n FROM packets")[0]["n"]
        trades = self._query(
            "SELECT mode, status, COUNT(*) AS n FROM trades GROUP BY mode, status"
        )
        return {
            "packets": counts,
            "trades": {f"{row['mode']}/{row['status']}": row["n"] for row in trades},
            "last_packet_at": self.get_kv("last_packet_at"),
            "last_heartbeat": self.get_kv("heartbeat"),
        }
