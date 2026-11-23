"""数据包与决策的数据模型。

指标 `coil_reclaim.pine` 在每根K线收盘时用 alert() 发出一个扁平的 JSON 对象，
schema 固定为 `coil-reclaim/v1`（字段见 docs/packet-schema.md）。

这里把它解析成不可变的 Packet，并把每个字段的缺失/类型错误显式记下来，
不让错误数据静默变成 0。数据包里的所有文本一律当数据，不当指令。
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

SCHEMA = "coil-reclaim/v1"

EVENTS = (
    "SESSION_BIAS",
    "STOP_ORDER_PLACED",
    "STOP_ORDER_CANCELLED",
    "ENTRY",
    "BREAKEVEN",
    "TOO_FAST_TAKE_PROFIT",
    "EXIT",
    "BAR_CLOSE",
)

ACTIONABLE_EVENTS = (
    "SESSION_BIAS",
    "STOP_ORDER_PLACED",
    "STOP_ORDER_CANCELLED",
    "ENTRY",
    "BREAKEVEN",
    "TOO_FAST_TAKE_PROFIT",
    "EXIT",
)

SESSIONS = ("Asia", "London", "NYSE")


class PacketError(ValueError):
    """数据包结构不合法（schema 不对、不是对象、事件未知）。"""


def _num(raw: Mapping[str, Any], key: str, issues: list[str], default: float = 0.0) -> float:
    value = raw.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        issues.append(f"{key} 应该是数字，收到布尔值")
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            issues.append(f"{key} 不是数字：{value!r}")
            return default
    issues.append(f"{key} 类型异常：{type(value).__name__}")
    return default


def _opt(raw: Mapping[str, Any], key: str, issues: list[str]) -> float | None:
    value = raw.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        issues.append(f"{key} 应该是数字或 null，收到布尔值")
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            issues.append(f"{key} 不是数字：{value!r}")
            return None
    issues.append(f"{key} 类型异常：{type(value).__name__}")
    return None


def _opt_int(raw: Mapping[str, Any], key: str, issues: list[str]) -> int | None:
    value = _opt(raw, key, issues)
    if value is None:
        return None
    return int(value)


def _bool(raw: Mapping[str, Any], key: str, issues: list[str], default: bool = False) -> bool:
    value = raw.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes"):
            return True
        if low in ("false", "0", "no"):
            return False
    issues.append(f"{key} 不是布尔值：{value!r}")
    return default


def _str(raw: Mapping[str, Any], key: str, issues: list[str], default: str = "") -> str:
    value = raw.get(key)
    if value is None:
        return default
    if isinstance(value, str):
        return value
    issues.append(f"{key} 不是字符串：{type(value).__name__}")
    return default


def _tokens(value: str) -> list[str]:
    return [part for part in value.replace("-", ":").replace("/", ":").split(":") if part]


@dataclass(frozen=True, slots=True)
class Packet:
    """一个指标事件。字段名与 packet-schema.md 保持一致。"""

    raw: dict
    event: str
    tag: str
    symbol: str
    timeframe: str
    ts: str
    price: float
    session: str
    session_live: bool
    session_trades: int
    max_trades: int
    bias: str
    daily_candle: str
    h4_candle: str
    staircase: str
    staircase_tf: str
    bias_reason: str
    mode: str
    daily_ceiling: float | None
    daily_floor: float | None
    position_pct: float | None
    at_daily_ceiling: bool
    at_daily_floor: bool
    blocked_today: bool
    in_clean_zone: bool
    next_ceiling: float | None
    next_floor: float | None
    side: str
    range_top: float | None
    range_bottom: float | None
    range_candles: int | None
    liquidity_grab: bool
    breakout_in_clean_zone: bool
    breakout_high: float | None
    breakout_low: float | None
    rubber_band: bool
    order_price: float | None
    order_expires_after: int
    status: str
    entry: float | None
    stop: float | None
    take_profit_1r: float | None
    target: float | None
    target_source: str
    breakeven: bool
    close_pct_at_1r: float
    open_r: float | None
    exit_price: float | None
    result_r: float | None
    risk_pct: float
    note: str
    received_at: str
    issues: tuple[str, ...] = field(default=())

    # ---- 派生 ----

    @property
    def base_symbol(self) -> str:
        """`BINANCE:BTCUSDT.P` -> `BTCUSDT`；`BTCUSDT.P` -> `BTCUSDT`。"""
        text = self.symbol.strip()
        for prefix in ("BINANCE:", "BYBIT:", "OKX:", "BITGET:", "MEXC:", "COINBASE:"):
            if text.upper().startswith(prefix):
                text = text[len(prefix):]
                break
        if text.upper().endswith(".P"):
            text = text[:-2]
        return text.upper()

    @property
    def exchange(self) -> str:
        parts = _tokens(self.symbol)
        if len(parts) >= 2:
            return parts[0].upper()
        return ""

    @property
    def ts_dt(self) -> datetime | None:
        """K线收盘时间（UTC）。解析不了返回 None。"""
        text = self.ts.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @property
    def dedupe_key(self) -> str:
        """同一根K线可能被 TradingView 重发，用品种+事件+时间+状态去重。"""
        return "|".join((self.tag, self.symbol, self.event, self.ts, self.status))

    @property
    def order_side(self) -> str:
        """LONG / SHORT / NONE。优先 side，其次由 bias 推。"""
        if self.side in ("LONG", "SHORT"):
            return self.side
        if self.bias == "BUY":
            return "LONG"
        if self.bias == "SELL":
            return "SHORT"
        return "NONE"

    def planning_entry(self) -> float | None:
        """挂单阶段用 order_price，成交之后用 entry。"""
        if self.entry is not None and self.entry > 0:
            return self.entry
        if self.order_price is not None and self.order_price > 0:
            return self.order_price
        return None

    def planning_target(self, cap_r: float) -> float | None:
        """目标：优先指标给的 target，其次下一个未破价位，最后按 cap_r 推。"""
        entry = self.planning_entry()
        stop = self.stop
        if entry is None or stop is None:
            return self.target
        if self.target is not None and self.target > 0:
            return self.target
        if self.order_side == "LONG" and self.next_ceiling is not None:
            return self.next_ceiling
        if self.order_side == "SHORT" and self.next_floor is not None:
            return self.next_floor
        risk = abs(entry - stop)
        if risk <= 0:
            return None
        return entry + risk * cap_r if self.order_side == "LONG" else entry - risk * cap_r

    def target_source_or(self) -> str:
        if self.target_source:
            return self.target_source
        if self.order_side == "LONG" and self.target is None and self.next_ceiling is not None:
            return "下一个天花板"
        if self.order_side == "SHORT" and self.target is None and self.next_floor is not None:
            return "下一个地板"
        return "3R封顶" if self.order_side != "NONE" else ""


def parse_packet(payload: Any, received_at: datetime | None = None) -> Packet:
    """把 webhook body 变成 Packet。非法结构抛 PacketError。"""
    if not isinstance(payload, dict):
        raise PacketError("数据包必须是 JSON 对象")
    schema = payload.get("schema")
    if schema != SCHEMA:
        raise PacketError(f"schema 应为 {SCHEMA}，收到 {schema!r}")
    event = payload.get("event")
    if not isinstance(event, str) or not event.strip():
        raise PacketError("缺 event")
    event = event.strip().upper()
    if event not in EVENTS:
        # 未知事件不丢包，但要让上层看见
        pass

    issues: list[str] = []
    stamp = received_at or datetime.now(timezone.utc)

    return Packet(
        raw=dict(payload),
        event=event,
        tag=_str(payload, "tag", issues, "coil"),
        symbol=_str(payload, "symbol", issues, "UNKNOWN"),
        timeframe=_str(payload, "timeframe", issues, ""),
        ts=_str(payload, "ts", issues, ""),
        price=_num(payload, "price", issues),
        session=_str(payload, "session", issues, ""),
        session_live=_bool(payload, "session_live", issues),
        session_trades=int(_num(payload, "session_trades", issues)),
        max_trades=int(_num(payload, "max_trades", issues, 2)),
        bias=_str(payload, "bias", issues, "NONE").upper(),
        daily_candle=_str(payload, "daily_candle", issues, "FLAT").upper(),
        h4_candle=_str(payload, "h4_candle", issues, "FLAT").upper(),
        staircase=_str(payload, "staircase", issues, "UNKNOWN").upper(),
        staircase_tf=_str(payload, "staircase_tf", issues, ""),
        bias_reason=_str(payload, "bias_reason", issues, ""),
        mode=_str(payload, "mode", issues, "BREAKOUT").upper(),
        daily_ceiling=_opt(payload, "daily_ceiling", issues),
        daily_floor=_opt(payload, "daily_floor", issues),
        position_pct=_opt(payload, "position_pct", issues),
        at_daily_ceiling=_bool(payload, "at_daily_ceiling", issues),
        at_daily_floor=_bool(payload, "at_daily_floor", issues),
        blocked_today=_bool(payload, "blocked_today", issues),
        in_clean_zone=_bool(payload, "in_clean_zone", issues),
        next_ceiling=_opt(payload, "next_ceiling", issues),
        next_floor=_opt(payload, "next_floor", issues),
        side=_str(payload, "side", issues, "NONE").upper(),
        range_top=_opt(payload, "range_top", issues),
        range_bottom=_opt(payload, "range_bottom", issues),
        range_candles=_opt_int(payload, "range_candles", issues),
        liquidity_grab=_bool(payload, "liquidity_grab", issues),
        breakout_in_clean_zone=_bool(payload, "breakout_in_clean_zone", issues),
        breakout_high=_opt(payload, "breakout_high", issues),
        breakout_low=_opt(payload, "breakout_low", issues),
        rubber_band=_bool(payload, "rubber_band", issues),
        order_price=_opt(payload, "order_price", issues),
        order_expires_after=int(_num(payload, "order_expires_after", issues, 6)),
        status=_str(payload, "status", issues, "FLAT").upper(),
        entry=_opt(payload, "entry", issues),
        stop=_opt(payload, "stop", issues),
        take_profit_1r=_opt(payload, "take_profit_1r", issues),
        target=_opt(payload, "target", issues),
        target_source=_str(payload, "target_source", issues, ""),
        breakeven=_bool(payload, "breakeven", issues),
        close_pct_at_1r=_num(payload, "close_pct_at_1r", issues, 50.0),
        open_r=_opt(payload, "open_r", issues),
        exit_price=_opt(payload, "exit_price", issues),
        result_r=_opt(payload, "result_r", issues),
        risk_pct=_num(payload, "risk_pct", issues, 0.5),
        note=_str(payload, "note", issues, ""),
        received_at=stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        issues=tuple(issues),
    )


def load_packet_file(path: str) -> Packet:
    with open(path, "r", encoding="utf-8") as handle:
        return parse_packet(json.load(handle))


# ---------------------------------------------------------------- 决策

VERDICT_LONG = "LONG"
VERDICT_SHORT = "SHORT"
VERDICT_SKIP = "SKIP"


@dataclass(frozen=True, slots=True)
class Check:
    """一条规则检查。ok=False 且 hard=True 会直接否掉这笔。"""

    name: str
    ok: bool
    detail: str
    hard: bool = True

    def line(self) -> str:
        mark = "✅" if self.ok else ("❌" if self.hard else "⚠️")
        return f"{mark} {self.name}：{self.detail}"


@dataclass(slots=True)
class Decision:
    event: str
    symbol: str
    verdict: str
    checks: list[Check]
    reasons: list[str]
    plan: dict[str, Any]
    packet: Packet

    @property
    def is_actionable(self) -> bool:
        return self.verdict in (VERDICT_LONG, VERDICT_SHORT)

    @property
    def failed_hard(self) -> list[str]:
        return [c.name for c in self.checks if not c.ok and c.hard]

    def render(self) -> str:
        return render_decision(self)


def _fmt(value: float | None, digits: int = 6) -> str:
    if value is None:
        return "null"
    text = f"{value:.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def render_decision(decision: Decision) -> str:
    """给 Telegram / 日志的纯文本结论，结构对齐 agent/AGENT_PROMPT.md 的输出。"""
    packet = decision.packet
    plan = decision.plan
    lines: list[str] = []
    head = f"【{decision.verdict}】 {packet.base_symbol} {packet.timeframe}m {packet.event}"
    if packet.session:
        head += f" · {packet.session}"
    lines.append(head)
    lines.append(f"收盘 {_fmt(packet.price)}  {packet.ts}  [{packet.tag}]")

    if packet.event == "SESSION_BIAS":
        lines.append(f"方向 {packet.bias}（{packet.bias_reason or '无理由'}）")
    elif packet.event == "STOP_ORDER_CANCELLED":
        lines.append(f"作废原因：{packet.note or '未说明'}")
    elif packet.event == "EXIT":
        result = packet.result_r
        lines.append(f"出场 {_fmt(packet.exit_price)}  结果 {_fmt(result, 2)}R  {packet.note}")
    elif packet.event == "BREAKEVEN":
        lines.append(f"止损挪到 {_fmt(packet.stop)}，平 {_fmt(packet.close_pct_at_1r, 0)}%")
    elif packet.event == "TOO_FAST_TAKE_PROFIT":
        lines.append(f"浮盈 {_fmt(packet.open_r, 2)}R，连续同向K线，止盈")
    elif packet.event == "ENTRY":
        lines.append(f"进场 {_fmt(packet.entry)} 止损 {_fmt(packet.stop)}")

    if decision.is_actionable and plan:
        lines.append("")
        lines.append(f"进场 {_fmt(plan.get('entry'))}（挂单）")
        lines.append(f"止损 {_fmt(plan.get('stop'))}")
        lines.append(f"1R   {_fmt(plan.get('take_profit_1r'))}")
        target_r = plan.get("target_r")
        lines.append(
            f"目标 {_fmt(plan.get('target'))}（{plan.get('target_source') or '未知'}）≈ {_fmt(target_r, 2)}R"
        )
        qty = plan.get("qty")
        lines.append(
            f"仓位 {_fmt(qty, 8)}（风险 {_fmt(plan.get('risk_amount'), 2)} USDT = "
            f"{_fmt(plan.get('risk_pct'), 2)}%，名义 {_fmt(plan.get('notional'), 2)}）"
        )
        lines.append(
            f"管理：到 1R 平 {_fmt(packet.close_pct_at_1r, 0)}%，止损挪进场；先回踩才成交，"
            f"收回区间或 {packet.order_expires_after} 根内没成交就作废"
        )

    if decision.checks:
        lines.append("")
        lines.extend(c.line() for c in decision.checks)
    if decision.reasons:
        lines.append("")
        lines.append("；".join(decision.reasons))
    if plan.get("warning"):
        lines.append(f"⚠️ {plan['warning']}")
    return "\n".join(lines)
