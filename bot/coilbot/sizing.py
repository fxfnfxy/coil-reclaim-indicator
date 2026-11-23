"""品种精度与仓位计算。

规则文档（agent/AGENT_PROMPT.md 的 `<仓位>` 一节）：
    风险金额 = 账户 × risk_pct ÷ 100
    数量     = 风险金额 ÷ |进场 − 止损|，按最小单位往下取整
    目标 R   = |目标 − 进场| ÷ |进场 − 止损|

这里把「按品种最小单位往下取整」具体化：优先读配置里的 qty_step，
没有就按价格量级推一个合理步长（BTC 0.001、ETH 0.01…），并在结果里注明是推断值。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config import AccountConfig, InstrumentConfig

# 常见品种的默认步长（U 本位永续）。配置里写了就以配置为准。
KNOWN_STEPS: dict[str, tuple[float, float, float]] = {
    # symbol: (qty_step, min_qty, price_tick)
    "BTCUSDT": (0.001, 0.001, 0.1),
    "ETHUSDT": (0.01, 0.01, 0.01),
    "SOLUSDT": (0.1, 0.1, 0.001),
    "BNBUSDT": (0.01, 0.01, 0.01),
    "XRPUSDT": (1.0, 1.0, 0.0001),
    "DOGEUSDT": (1.0, 1.0, 0.00001),
    "ADAUSDT": (1.0, 1.0, 0.0001),
    "AVAXUSDT": (0.01, 0.01, 0.001),
    "LINKUSDT": (0.01, 0.01, 0.001),
    "ARBUSDT": (1.0, 1.0, 0.0001),
}


@dataclass(slots=True)
class SizeResult:
    ok: bool
    qty: float = 0.0
    risk_amount: float = 0.0
    risk_per_unit: float = 0.0
    notional: float = 0.0
    target_r: float | None = None
    reason: str = ""
    notes: list[str] | None = None

    def __post_init__(self) -> None:
        if self.notes is None:
            self.notes = []


def infer_step(symbol: str, price: float) -> tuple[float, float, str]:
    """返回 (qty_step, min_qty, 来源说明)。"""
    bare = symbol.upper().split(":")[-1].replace(".P", "")
    if bare in KNOWN_STEPS:
        step, min_qty, _tick = KNOWN_STEPS[bare]
        return step, min_qty, "内置品种表"
    # 按价格量级推：让一手的名义价值大致落在 1~100 USDT 之间
    if price <= 0:
        return 0.001, 0.001, "价格缺失，按 0.001 兜底"
    magnitude = 10 ** math.floor(math.log10(max(price, 1e-8)))
    step = magnitude / 100_000.0
    if step <= 0:
        step = 0.001
    return step, step, f"按价格量级推断（{step:g}）"


def _round_down(value: float, step: float) -> float:
    if step <= 0:
        return value
    steps = math.floor(value / step + 1e-12)
    return max(0.0, round(steps * step, 12))


def _decimals(step: float) -> int:
    if step <= 0:
        return 8
    text = f"{step:.12f}".rstrip("0")
    if "." in text:
        return len(text.split(".", 1)[1])
    return 0


def compute_target_r(entry: float, stop: float, target: float | None) -> float | None:
    if target is None:
        return None
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    return abs(target - entry) / risk


def size_position(
    *,
    symbol: str,
    side: str,
    entry: float | None,
    stop: float | None,
    target: float | None,
    risk_pct: float,
    account: AccountConfig,
    instrument: InstrumentConfig,
    cap_r: float = 3.0,
    max_notional_multiple: float | None = None,
) -> SizeResult:
    notes: list[str] = []
    if side not in ("LONG", "SHORT"):
        return SizeResult(ok=False, reason="方向不是 LONG/SHORT")
    if entry is None or entry <= 0:
        return SizeResult(ok=False, reason="进场价缺失")
    if stop is None or stop <= 0:
        return SizeResult(ok=False, reason="止损价缺失")
    risk_per_unit = entry - stop if side == "LONG" else stop - entry
    if risk_per_unit <= 0:
        return SizeResult(
            ok=False,
            risk_per_unit=0.0,
            reason=f"止损在进场同一侧（entry={entry:g} stop={stop:g} side={side}）",
        )

    rp = risk_pct if risk_pct and risk_pct > 0 else account.default_risk_pct
    risk_amount = account.equity_usdt * rp / 100.0
    raw_qty = risk_amount / risk_per_unit
    if instrument.contract_usd > 0:
        # 反向合约：1 张 = contract_usd 美元面值，实际风险按张数折算
        raw_contracts = risk_amount / (risk_per_unit / entry * instrument.contract_usd)
        raw_qty = raw_contracts

    inferred_everything = False
    step = instrument.qty_step
    min_qty = instrument.min_qty
    if step <= 0:
        step, min_qty, source = infer_step(symbol, entry)
        notes.append(f"qty_step 未配置，{source}")
        inferred_everything = True
    if min_qty <= 0:
        min_qty = step

    qty = _round_down(raw_qty, step) if account.round_down else round(raw_qty, _decimals(step))
    if qty <= 0:
        return SizeResult(
            ok=False,
            risk_amount=risk_amount,
            risk_per_unit=risk_per_unit,
            reason=f"止损太宽：{risk_amount:.2f} USDT 风险 ÷ {risk_per_unit:g} = {raw_qty:g}，"
                   f"按步长 {step:g} 取整后为 0",
            notes=notes,
        )
    if qty < min_qty:
        return SizeResult(
            ok=False,
            qty=qty,
            risk_amount=risk_amount,
            risk_per_unit=risk_per_unit,
            reason=f"数量 {qty:g} 小于最小下单量 {min_qty:g}",
            notes=notes,
        )

    notional = qty * entry
    if instrument.contract_usd > 0:
        notional = qty * instrument.contract_usd

    multiple = max_notional_multiple if max_notional_multiple is not None else account.max_notional_multiple
    if multiple > 0 and notional > account.equity_usdt * multiple:
        allowed_qty = _round_down(account.equity_usdt * multiple / (entry if instrument.contract_usd <= 0 else instrument.contract_usd), step)
        if allowed_qty < min_qty or allowed_qty <= 0:
            return SizeResult(
                ok=False,
                risk_amount=risk_amount,
                risk_per_unit=risk_per_unit,
                notional=notional,
                reason=f"名义 {notional:.2f} 超过权益 {multiple:g} 倍，且下调后低于最小下单量",
                notes=notes,
            )
        notes.append(f"名义价值 {notional:.2f} 超限，数量从 {qty:g} 下调到 {allowed_qty:g}")
        qty = allowed_qty
        notional = qty * entry if instrument.contract_usd <= 0 else qty * instrument.contract_usd

    target_r = compute_target_r(entry, stop, target)
    if target is not None and target_r is not None and target_r > cap_r:
        notes.append(f"目标 {target_r:.2f}R 超过上限 {cap_r:g}R，按 {cap_r:g}R 执行")
        target_r = cap_r
    result = SizeResult(
        ok=True,
        qty=round(qty, _decimals(step)),
        risk_amount=round(risk_amount, 4),
        risk_per_unit=round(risk_per_unit, 8),
        notional=round(notional, 4),
        target_r=None if target_r is None else round(target_r, 4),
        notes=notes,
    )
    if inferred_everything:
        result.reason = "步长是推断值，实盘前请在 [instruments] 里写准"
    return result
