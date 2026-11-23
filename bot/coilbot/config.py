"""配置：TOML 文件 + 环境变量覆盖，全部带默认值。

原则：没有配置文件也能跑（signal 模式，只写日志和本地库），
任何一项想开就填一项。敏感值优先从环境变量读，避免写进仓库。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

ENV_PREFIX = "COILBOT_"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(ENV_PREFIX + name, default)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(ENV_PREFIX + name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(slots=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    path: str = "/webhook"
    token: str = ""
    max_body_kb: int = 64
    rate_limit_per_min: int = 240
    trust_proxy: bool = False


@dataclass(slots=True)
class AccountConfig:
    equity_usdt: float = 10_000.0
    default_risk_pct: float = 0.5
    min_target_r: float = 1.0
    max_notional_multiple: float = 5.0   # 名义价值 ≤ 权益 × 这个倍数
    round_down: bool = True


@dataclass(slots=True)
class JudgeConfig:
    allow_sessions: list[str] = field(default_factory=lambda: ["Asia", "London", "NYSE"])
    require_session_live: bool = True
    require_clean_zone: bool = True
    require_bias_match: bool = True
    block_when_blocked_today: bool = True
    require_rubber_band_on_entry: bool = True
    min_range_candles: int = 3
    max_trades_per_session: int = 0     # 0 = 用数据包里的 max_trades
    stale_minutes: int = 30             # 包太旧就不采纳


@dataclass(slots=True)
class ExecutionConfig:
    mode: str = "signal"                # signal | paper | live
    paper_start_equity: float = 10_000.0
    live_dry_run: bool = True
    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_base_url: str = "https://fapi.binance.com"
    leverage: int = 3
    margin_type: str = "ISOLATED"


@dataclass(slots=True)
class InstrumentConfig:
    qty_step: float = 0.0
    min_qty: float = 0.0
    price_tick: float = 0.0
    contract_usd: float = 0.0           # 反向合约面值，正向填 0


@dataclass(slots=True)
class NotifyConfig:
    desktop: bool = False
    log_only: bool = True
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_proxy: str = ""
    dingtalk_webhook: str = ""
    dingtalk_secret: str = ""
    wecom_webhook: str = ""
    feishu_webhook: str = ""
    feishu_secret: str = ""
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_to: str = ""
    smtp_ssl: bool = True
    send_bar_close: bool = False
    retry: int = 2
    timeout_seconds: float = 8.0


@dataclass(slots=True)
class WatchConfig:
    heartbeat_seconds: int = 60
    alert_if_silent_minutes: int = 90     # 交易时段内超过这么久没有包就告警
    alert_repeat_minutes: int = 180
    daily_report_at: str = "08:05"        # 本地时间 HH:MM，空字符串关闭
    daily_report_tz: str = "Asia/Shanghai"
    retention_days: int = 120
    state_dir: str = "run"


@dataclass(slots=True)
class StoreConfig:
    db_path: str = "run/coilbot.db"
    archive_dir: str = "run/packets"


@dataclass(slots=True)
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    account: AccountConfig = field(default_factory=AccountConfig)
    judge: JudgeConfig = field(default_factory=JudgeConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    watch: WatchConfig = field(default_factory=WatchConfig)
    store: StoreConfig = field(default_factory=StoreConfig)
    instruments: dict[str, InstrumentConfig] = field(default_factory=dict)
    source_path: str = ""

    def instrument(self, *keys: str) -> InstrumentConfig:
        """按 `BINANCE:BTCUSDT.P` / `BTCUSDT` / `BTCUSDT.P` 依次找品种配置。"""
        for key in keys:
            if not key:
                continue
            found = self.instruments.get(key) or self.instruments.get(key.upper())
            if found:
                return found
            bare = key.split(":")[-1]
            found = self.instruments.get(bare) or self.instruments.get(bare.upper())
            if found:
                return found
        return InstrumentConfig()


def _fill(obj: Any, data: dict[str, Any]) -> None:
    known = {f.name: f for f in fields(obj)}
    for key, value in data.items():
        if key not in known:
            continue
        current = getattr(obj, key)
        if isinstance(current, bool):
            setattr(obj, key, bool(value))
        elif isinstance(current, int) and not isinstance(value, bool):
            setattr(obj, key, int(value))
        elif isinstance(current, float):
            setattr(obj, key, float(value))
        elif isinstance(current, list):
            setattr(obj, key, [str(v) for v in value] if isinstance(value, list) else current)
        else:
            setattr(obj, key, value)


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """读 TOML（缺省 config.toml，找不到就用默认值），再用环境变量覆盖敏感项。"""
    config = Config()
    candidates: list[Path] = []
    if path:
        candidates.append(Path(path))
    else:
        env_path = _env("CONFIG")
        if env_path:
            candidates.append(Path(env_path))
        candidates.append(Path("config.toml"))
        candidates.append(Path(__file__).resolve().parent.parent / "config.toml")

    for candidate in candidates:
        if candidate.is_file():
            with candidate.open("rb") as handle:
                data = tomllib.load(handle)
            config.source_path = str(candidate.resolve())
            _fill(config.server, data.get("server", {}))
            _fill(config.account, data.get("account", {}))
            _fill(config.judge, data.get("judge", {}))
            _fill(config.execution, data.get("execution", {}))
            _fill(config.notify, data.get("notify", {}))
            _fill(config.watch, data.get("watch", {}))
            _fill(config.store, data.get("store", {}))
            for name, entry in (data.get("instruments", {}) or {}).items():
                instrument = InstrumentConfig()
                _fill(instrument, entry or {})
                config.instruments[name] = instrument
            break

    # 环境变量优先（部署时不想把密钥写进文件）
    config.server.token = _env("TOKEN", config.server.token)
    config.server.port = int(_env("PORT", str(config.server.port)))
    config.server.host = _env("HOST", config.server.host)
    config.account.equity_usdt = float(_env("EQUITY", str(config.account.equity_usdt)))
    config.execution.mode = _env("MODE", config.execution.mode).lower()
    config.execution.binance_api_key = _env("BINANCE_KEY", config.execution.binance_api_key)
    config.execution.binance_api_secret = _env("BINANCE_SECRET", config.execution.binance_api_secret)
    config.notify.telegram_bot_token = _env("TG_TOKEN", config.notify.telegram_bot_token)
    config.notify.telegram_chat_id = _env("TG_CHAT", config.notify.telegram_chat_id)
    config.notify.dingtalk_webhook = _env("DINGTALK", config.notify.dingtalk_webhook)
    config.notify.dingtalk_secret = _env("DINGTALK_SECRET", config.notify.dingtalk_secret)
    config.notify.wecom_webhook = _env("WECOM", config.notify.wecom_webhook)
    config.store.db_path = _env("DB", config.store.db_path)
    config.watch.state_dir = _env("STATE_DIR", config.watch.state_dir)
    config.notify.desktop = _env_bool("DESKTOP", config.notify.desktop)

    if config.execution.mode not in ("signal", "paper", "live"):
        config.execution.mode = "signal"
    return config


def ensure_dirs(config: Config) -> None:
    for path in (config.store.db_path, config.store.archive_dir, config.watch.state_dir):
        parent = Path(path)
        target = parent if path.endswith(("/", "\\")) or parent.suffix == "" else parent.parent
        target.mkdir(parents=True, exist_ok=True)
