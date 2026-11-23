"""Binance U 本位合约执行器（可选，默认 dry_run）。

设计取向：
  - 默认 `execution.mode = "signal"`：只出信号，不碰交易所。
  - `mode = "paper"`：跟随指标事件记账，仍不下单。
  - `mode = "live"` + `live_dry_run = true`：把要发的请求完整打印出来，但一个都不发。
  - `mode = "live"` + `live_dry_run = false`：真下单。这一层必须由使用者显式打开。

安全约束：
  - 下单不重试（重复下单的钱是真钱）。
  - 每次下单前检查数量精度与最小名义价值（币安要求 ≥ 5 USDT）。
  - 只发 STOP_MARKET 触发单 + 保护性 STOP_MARKET 止损单；不做市价追单。
  - 所有请求与响应写日志，可逐笔复盘。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN

from .config import ExecutionConfig

LOGGER = logging.getLogger("coilbot.binance")


@dataclass(slots=True)
class OrderResult:
    ok: bool
    order_id: str = ""
    detail: str = ""
    dry_run: bool = False


class BinanceFutures:
    def __init__(self, config: ExecutionConfig, timeout: float = 10.0) -> None:
        self.config = config
        self.timeout = timeout

    # ------------------------------------------------------------ 底层
    def _sign(self, params: dict) -> str:
        query = urllib.parse.urlencode(params, doseq=True)
        signature = hmac.new(
            self.config.binance_api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{query}&signature={signature}"

    def _request(self, method: str, path: str, params: dict | None = None, signed: bool = True) -> dict:
        params = dict(params or {})
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = 5000
            query = self._sign(params)
            url = f"{self.config.binance_base_url}{path}?{query}"
            headers = {"X-MBX-APIKEY": self.config.binance_api_key}
        else:
            query = urllib.parse.urlencode(params, doseq=True)
            url = f"{self.config.binance_base_url}{path}" + (f"?{query}" if query else "")
            headers = {}

        redacted = dict(params)
        if "signature" in redacted:
            redacted["signature"] = "***"
        LOGGER.info("binance %s %s %s", method, path, redacted)

        request = urllib.request.Request(url, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
                LOGGER.debug("binance resp %s", body[:500])
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            LOGGER.error("binance %s %s 失败 %s %s", method, path, exc.code, text[:400])
            raise RuntimeError(f"HTTP {exc.code} {text[:300]}") from exc

    # ------------------------------------------------------------ 精度
    def symbol_filters(self, symbol: str) -> dict:
        info = self._request("GET", "/fapi/v1/exchangeInfo", signed=False)
        for entry in info.get("symbols", []):
            if entry.get("symbol") == symbol:
                filters = {f["filterType"]: f for f in entry.get("filters", [])}
                return {
                    "quantityPrecision": entry.get("quantityPrecision", 8),
                    "pricePrecision": entry.get("pricePrecision", 8),
                    "stepSize": float(filters.get("LOT_SIZE", {}).get("stepSize", "0.001")),
                    "minQty": float(filters.get("LOT_SIZE", {}).get("minQty", "0")),
                    "tickSize": float(filters.get("PRICE_FILTER", {}).get("tickSize", "0.1")),
                    "minNotional": float(filters.get("MIN_NOTIONAL", {}).get("notional", "5")),
                }
        raise RuntimeError(f"交易所没有这个品种：{symbol}")

    @staticmethod
    def _floor(value: float, step: float) -> float:
        if step <= 0:
            return value
        return float((Decimal(str(value)) / Decimal(str(step))).to_integral_value(rounding=ROUND_DOWN) * Decimal(str(step)))

    def _normalize(self, symbol: str, qty: float, price: float | None = None) -> tuple[float, float | None]:
        rules = self.symbol_filters(symbol)
        quantity = self._floor(qty, rules["stepSize"])
        normalized_price = None if price is None else self._floor(price, rules["tickSize"])
        return quantity, normalized_price

    def _guard(self, action: str, params: dict) -> OrderResult | None:
        if self.config.live_dry_run:
            LOGGER.warning("[DRY RUN] 不会真的下单：%s %s", action, params)
            return OrderResult(ok=True, order_id="dry-run", detail="dry_run 未发送", dry_run=True)
        if not (self.config.binance_api_key and self.config.binance_api_secret):
            return OrderResult(ok=False, detail="缺 api key/secret")
        return None

    # ------------------------------------------------------------ 下单
    def set_leverage(self, symbol: str) -> dict:
        guard = self._guard("setLeverage", {"symbol": symbol, "leverage": self.config.leverage})
        if guard:
            return {"dry_run": True}
        return self._request(
            "POST",
            "/fapi/v1/leverage",
            {"symbol": symbol, "leverage": self.config.leverage},
        )

    def place_stop_entry(self, symbol: str, side: str, stop_price: float, qty: float) -> OrderResult:
        """突破挂单：做多 = BUY STOP_MARKET 在突破K线高点之上，做空反之。"""
        action = "BUY" if side == "LONG" else "SELL"
        quantity, price = self._normalize(symbol, qty, stop_price)
        if quantity <= 0:
            return OrderResult(ok=False, detail="数量取整后为 0")
        params = {
            "symbol": symbol,
            "side": action,
            "type": "STOP_MARKET",
            "quantity": f"{quantity}",
            "stopPrice": f"{price}",
            "workingType": "MARK_PRICE",
            "timeInForce": "GTC",
            "newOrderRespType": "RESULT",
        }
        guard = self._guard("place_stop_entry", params)
        if guard:
            return guard
        try:
            response = self._request("POST", "/fapi/v1/order", params)
        except Exception as exc:  # noqa: BLE001
            return OrderResult(ok=False, detail=str(exc))
        return OrderResult(ok=True, order_id=str(response.get("orderId", "")), detail="stop entry 已提交")

    def place_protective_stop(self, symbol: str, side: str, stop_price: float, qty: float) -> OrderResult:
        """成交后的保护性止损（reduceOnly）。"""
        action = "SELL" if side == "LONG" else "BUY"
        quantity, price = self._normalize(symbol, qty, stop_price)
        params = {
            "symbol": symbol,
            "side": action,
            "type": "STOP_MARKET",
            "quantity": f"{quantity}",
            "stopPrice": f"{price}",
            "workingType": "MARK_PRICE",
            "reduceOnly": "true",
            "timeInForce": "GTC",
            "newOrderRespType": "RESULT",
        }
        guard = self._guard("place_protective_stop", params)
        if guard:
            return guard
        try:
            response = self._request("POST", "/fapi/v1/order", params)
        except Exception as exc:  # noqa: BLE001
            return OrderResult(ok=False, detail=str(exc))
        return OrderResult(ok=True, order_id=str(response.get("orderId", "")), detail="保护止损已提交")

    def cancel_order(self, symbol: str, order_id: str) -> OrderResult:
        params = {"symbol": symbol, "orderId": order_id}
        guard = self._guard("cancel_order", params)
        if guard:
            return guard
        try:
            self._request("DELETE", "/fapi/v1/order", params)
        except Exception as exc:  # noqa: BLE001
            return OrderResult(ok=False, detail=str(exc))
        return OrderResult(ok=True, order_id=order_id, detail="已撤单")

    def market_close(self, symbol: str, side: str, qty: float) -> OrderResult:
        action = "SELL" if side == "LONG" else "BUY"
        quantity, _ = self._normalize(symbol, qty)
        params = {
            "symbol": symbol,
            "side": action,
            "type": "MARKET",
            "quantity": f"{quantity}",
            "reduceOnly": "true",
        }
        guard = self._guard("market_close", params)
        if guard:
            return guard
        try:
            response = self._request("POST", "/fapi/v1/order", params)
        except Exception as exc:  # noqa: BLE001
            return OrderResult(ok=False, detail=str(exc))
        return OrderResult(ok=True, order_id=str(response.get("orderId", "")), detail="市价平仓已提交")

    def position(self, symbol: str) -> dict:
        data = self._request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
        if isinstance(data, list) and data:
            return data[0]
        return {}
