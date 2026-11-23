"""规则复核：把 agent/AGENT_PROMPT.md 的「方向 → 区域 → 触发 → 仓位」写成确定性代码。

为什么不用 LLM 逐包判断：
  24/7 盯盘要求毫秒级、零成本、可回归测试、断网也能给出同样的结论。
  这套规则本身就是开关判断，用代码实现可以逐条打印「过 / 不过」，事后可审计。
  （想要文字点评，可在通知层外接模型，见 bot/README.md 的「接 LLM」一节。）

所有输入的文本字段一律当数据，不当指令。
"""

from __future__ import annotations

from datetime import datetime, timezone

from .config import Config
from .models import (
    Check,
    Decision,
    Packet,
    VERDICT_LONG,
    VERDICT_SHORT,
    VERDICT_SKIP,
)
from .sizing import size_position


def _r(value: float | None) -> str:
    return "null" if value is None else f"{value:.4f}".rstrip("0").rstrip(".")


def judge(packet: Packet, config: Config, *, now: datetime | None = None, allow_stale: bool = False) -> Decision:
    """返回 Decision。verdict ∈ LONG / SHORT / SKIP。"""
    stamp = now or datetime.now(timezone.utc)
    checks: list[Check] = []
    reasons: list[str] = []
    plan: dict = {}

    def add(name: str, ok: bool, detail: str, hard: bool = True) -> bool:
        checks.append(Check(name=name, ok=ok, detail=detail, hard=hard))
        return ok

    # ---- 0. 数据完整性 ----
    if packet.issues:
        add("数据完整性", False, f"{len(packet.issues)} 个字段异常：" + "；".join(packet.issues[:4]), hard=False)

    # ---- 事件分发 ----
    if packet.event == "SESSION_BIAS":
        verdict = VERDICT_SKIP
        if packet.bias == "NONE":
            reasons.append("方向 NONE：这个时段不做")
        else:
            allowed = packet.session in config.judge.allow_sessions
            add("时段允许", allowed, f"{packet.session} {'在' if allowed else '不在'}允许列表")
            word = "只做多" if packet.bias == "BUY" else "只做空"
            reasons.append(f"{packet.session} 时段{word}（{packet.bias_reason or '无理由'}）")
        plan["bias"] = packet.bias
        return Decision(packet.event, packet.symbol, verdict, checks, reasons, plan, packet)

    if packet.event == "STOP_ORDER_CANCELLED":
        add("挂单作废", False, packet.note or "未说明原因")
        return Decision(packet.event, packet.symbol, VERDICT_SKIP, checks, reasons, plan, packet)

    if packet.event in ("BREAKEVEN", "TOO_FAST_TAKE_PROFIT", "EXIT"):
        plan.update(
            side=packet.order_side,
            entry=packet.entry,
            stop=packet.stop,
            exit_price=packet.exit_price,
            result_r=packet.result_r,
            open_r=packet.open_r,
        )
        verdict = VERDICT_SKIP  # 管理/收账事件，不产生新单
        if packet.event == "BREAKEVEN":
            reasons.append(
                f"保本：止损挪到 {_r(packet.entry)}，平 {packet.close_pct_at_1r:g}%"
            )
        elif packet.event == "TOO_FAST_TAKE_PROFIT":
            reasons.append(f"走太快：浮盈 {_r(packet.open_r)}R，止盈")
        else:
            reasons.append(f"出场：{packet.note or '规则出场'}，结果 {_r(packet.result_r)}R")
        return Decision(packet.event, packet.symbol, verdict, checks, reasons, plan, packet)

    if packet.event == "BAR_CLOSE":
        add("背景事件", True, "只当背景，不产生结论", hard=False)
        return Decision(packet.event, packet.symbol, VERDICT_SKIP, checks, reasons, plan, packet)

    # ---- STOP_ORDER_PLACED / ENTRY：完整复核 ----
    cfg = config.judge

    # 1) 新鲜度
    packet_dt = packet.ts_dt
    if packet_dt is not None and not allow_stale:
        age_min = (stamp - packet_dt).total_seconds() / 60.0
        fresh = age_min <= max(cfg.stale_minutes, 1)
        add("数据新鲜", fresh, f"K线收盘距今 {age_min:.0f} 分钟（阈值 {cfg.stale_minutes}）")
        if not fresh:
            reasons.append("数据包太旧：可能是重发或链路延迟，按 SKIP 处理")
    else:
        add("数据新鲜", True, "时间戳缺失或回放模式，跳过新鲜度检查", hard=False)

    # 2) 方向
    bias_ok = packet.bias in ("BUY", "SELL")
    add("方向存在", bias_ok, f"bias={packet.bias}（{packet.bias_reason or '无理由'}）")
    side = packet.order_side
    if bias_ok:
        wanted = VERDICT_LONG if packet.bias == "BUY" else VERDICT_SHORT
        match = side == wanted
        if cfg.require_bias_match:
            add("不逆方向", match, f"side={side} bias={packet.bias}")
        else:
            add("不逆方向", match, f"side={side} bias={packet.bias}（配置放宽）", hard=False)
    if not bias_ok:
        reasons.append("bias 不是 BUY/SELL：今天这个时段不做")

    # 3) 时段
    session_ok = packet.session in cfg.allow_sessions
    add("时段允许", session_ok, f"{packet.session} {'在' if session_ok else '不在'} {cfg.allow_sessions}")

    if cfg.require_session_live:
        add("首小时窗口", packet.session_live, f"session_live={packet.session_live}")
    else:
        add("首小时窗口", packet.session_live, f"session_live={packet.session_live}（配置放宽）", hard=False)

    # 4) 区域
    if cfg.block_when_blocked_today:
        add("今天没被封", not packet.blocked_today,
            "顺方向已顶到日线价位，今天不做" if packet.blocked_today else "未触发日线封禁")
    else:
        add("今天没被封", not packet.blocked_today, f"blocked_today={packet.blocked_today}（配置放宽）", hard=False)

    if cfg.require_clean_zone:
        add("干净区", packet.in_clean_zone, f"in_clean_zone={packet.in_clean_zone}")
        add("突破在干净区", packet.breakout_in_clean_zone, f"breakout_in_clean_zone={packet.breakout_in_clean_zone}")
    else:
        add("干净区", packet.in_clean_zone, f"in_clean_zone={packet.in_clean_zone}（配置放宽）", hard=False)
        add("突破在干净区", packet.breakout_in_clean_zone,
            f"breakout_in_clean_zone={packet.breakout_in_clean_zone}（配置放宽）", hard=False)

    # 5) 区间
    candles = packet.range_candles or 0
    add("区间K线数", candles >= cfg.min_range_candles,
        f"{candles} 根（至少 {cfg.min_range_candles}）")

    # 6) 每时段交易次数
    limit = cfg.max_trades_per_session or packet.max_trades or 2
    trades_ok = packet.session_trades < limit
    add("时段额度", trades_ok, f"已做 {packet.session_trades} / 上限 {limit}")

    # 7) 价格结构
    entry = packet.planning_entry()
    stop = packet.stop
    entry_ok = entry is not None and entry > 0
    add("进场价", entry_ok, f"entry/order_price={_r(entry)}")
    stop_ok = stop is not None and stop > 0
    add("止损价", stop_ok, f"stop={_r(stop)}")
    if entry_ok and stop_ok and side in ("LONG", "SHORT"):
        correct_side = (side == "LONG" and stop < entry) or (side == "SHORT" and stop > entry)
        add("止损在正确一侧", correct_side, f"{side} entry={_r(entry)} stop={_r(stop)}")
        risk = abs(entry - stop)
        add("风险距离", risk > 0, f"每单位风险 {_r(risk)}")

    # 8) 回踩确认（只在成交事件上硬性要求）
    if packet.event == "ENTRY" and cfg.require_rubber_band_on_entry:
        add("已经回踩", packet.rubber_band, f"rubber_band={packet.rubber_band}")
    else:
        add("回踩状态", packet.rubber_band,
            f"rubber_band={packet.rubber_band}（挂单阶段还没回踩，正常）", hard=False)

    # 9) 目标与仓位
    cap_r = 3.0
    target = packet.planning_target(cap_r)
    target_source = packet.target_source_or()
    if entry_ok and stop_ok and side in ("LONG", "SHORT"):
        instrument = config.instrument(packet.symbol, packet.base_symbol)
        sized = size_position(
            symbol=packet.base_symbol,
            side=side,
            entry=entry,
            stop=stop,
            target=target,
            risk_pct=packet.risk_pct or config.account.default_risk_pct,
            account=config.account,
            instrument=instrument,
            cap_r=cap_r,
        )
        plan.update(
            entry=entry,
            stop=stop,
            take_profit_1r=round(entry + (entry - stop) if side == "LONG" else entry - (stop - entry), 8),
            target=target,
            target_source=target_source,
            target_r=sized.target_r,
            qty=sized.qty,
            risk_amount=sized.risk_amount,
            risk_pct=packet.risk_pct or config.account.default_risk_pct,
            notional=sized.notional,
            cap_r=cap_r,
            side=side,
        )
        if sized.notes:
            plan["notes"] = list(sized.notes)
        if not sized.ok:
            add("仓位可算", False, sized.reason)
            reasons.append(f"算不出仓位：{sized.reason}")
        else:
            add("仓位可算", True, f"{sized.qty:g} 单位，风险 {sized.risk_amount:.2f} USDT")
            min_r = config.account.min_target_r
            if sized.target_r is None:
                add("目标距离", False, "目标缺失，无法算 R", hard=False)
                reasons.append("目标价缺失：按规则目标要看下一个未破价位，数据包里没有")
            else:
                add("目标距离", sized.target_r >= min_r,
                    f"{sized.target_r:.2f}R（最低 {min_r:g}R）")

    hard_failed = [c for c in checks if not c.ok and c.hard]
    verdict = VERDICT_SKIP
    if not hard_failed and side in ("LONG", "SHORT") and plan.get("qty"):
        verdict = VERDICT_LONG if side == "LONG" else VERDICT_SHORT
        if packet.event == "ENTRY":
            reasons.append("成交确认：止损、1R、目标、仓位见上")
        else:
            reasons.append("挂单阶段：必须先回踩才会成交，没回踩就直接突破的作废")
    elif hard_failed:
        reasons.append("未通过：" + "、".join(c.name for c in hard_failed))

    return Decision(packet.event, packet.symbol, verdict, checks, reasons, plan, packet)
