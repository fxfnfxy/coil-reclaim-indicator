"""守护线程：心跳、静默告警、每日战报、数据清理。

24/7 盯盘真正会出问题的地方不是策略，是「悄悄断了没人知道」：
  - TradingView 警报被停用 / 品种改代码 / 隧道掉线 → 一根包都不来
  - 进程还在，但 webhook 没起来
  - 磁盘写满

所以这里盯着两件事：进程自己活着（心跳），以及「该有包的时候有没有包」（静默告警）。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Config
from .engine import Engine

LOGGER = logging.getLogger("coilbot.watch")

# 三个时段的第一个小时（本地时区 -> 窗口），与指标默认设置一致
SESSION_WINDOWS = (
    ("Asia", "Asia/Tokyo", time(9, 0), time(10, 0)),
    ("London", "Europe/London", time(8, 0), time(9, 0)),
    ("NYSE", "America/New_York", time(9, 30), time(10, 30)),
)

WEEKDAYS = (0, 1, 2, 3, 4)  # 加密 7x24，这里只用来算日报，不影响收包

# Windows 上没装 tzdata 时 ZoneInfo 会失败；退化成固定偏移（不含夏令时，最多差 1 小时）
FALLBACK_OFFSETS = {
    "Asia/Tokyo": 9,
    "Asia/Shanghai": 8,
    "Europe/London": 0,
    "America/New_York": -5,
    "UTC": 0,
}


def get_tz(name: str):
    """取时区；缺 tzdata 时退化并让调用方知道（返回值第二项 False）。"""
    try:
        return ZoneInfo(name), True
    except Exception:  # noqa: BLE001 - 缺时区数据
        from datetime import timedelta, timezone as _timezone

        hours = FALLBACK_OFFSETS.get(name)
        if hours is None:
            return _timezone.utc, False
        return _timezone(timedelta(hours=hours)), False


def active_window(now_utc: datetime | None = None) -> str | None:
    """现在是否落在某个时段的首小时窗口内，返回时段名。"""
    now = now_utc or datetime.now(timezone.utc)
    for name, tz_name, start, end in SESSION_WINDOWS:
        tzinfo, exact = get_tz(tz_name)
        local = now.astimezone(tzinfo)
        current = local.time()
        if start <= current < end:
            return name
        if not exact:
            LOGGER.debug("时区 %s 缺数据，用固定偏移判断", tz_name)
    return None


class Watchdog:
    def __init__(self, config: Config, engine: Engine, webhook_alive=None) -> None:
        self.config = config
        self.engine = engine
        self.webhook_alive = webhook_alive or (lambda: True)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.state_dir = Path(config.watch.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._last_silence_alert: datetime | None = None
        self._last_report_day = self._initial_report_day()

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="watchdog", daemon=True)
        self._thread.start()
        LOGGER.info("守护线程已启动（心跳 %ss）", self.config.watch.heartbeat_seconds)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        interval = max(10, self.config.watch.heartbeat_seconds)
        last_prune = datetime.now(timezone.utc) - timedelta(days=1)
        while not self._stop.wait(interval):
            now = datetime.now(timezone.utc)
            try:
                self._heartbeat(now)
                self._check_silence(now)
                self._maybe_daily_report(now)
                if (now - last_prune).total_seconds() > 86400:
                    removed = self.engine.store.prune(self.config.watch.retention_days)
                    if removed:
                        LOGGER.info("清理过期数据包 %s 条", removed)
                    last_prune = now
            except Exception as exc:  # noqa: BLE001 - 守护线程不能死
                LOGGER.exception("守护线程异常：%s", exc)
                self.engine.store.record_alert("error", "守护线程异常", str(exc))

    # ------------------------------------------------------------ 心跳
    def _heartbeat(self, now: datetime) -> None:
        stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        self.engine.store.set_kv("heartbeat", stamp)
        alive = "1" if self.webhook_alive() else "0"
        self.engine.store.set_kv("webhook_alive", alive)
        (self.state_dir / "heartbeat.txt").write_text(
            f"{stamp}\nwebhook_alive={alive}\nmode={self.config.execution.mode}\n", encoding="utf-8"
        )
        if alive != "1":
            self.engine.store.record_alert("error", "webhook 不在监听", "健康检查失败，进程可能需要重启")

    # ------------------------------------------------------------ 静默告警
    def _check_silence(self, now: datetime) -> None:
        cfg = self.config.watch
        window = active_window(now)
        if window is None:
            self._last_silence_alert = None
            return
        last = self.engine.store.get_kv_dt("last_packet_at")
        if last is None:
            # 刚装好还没收到过包，等一轮再说
            self.engine.store.set_kv("last_packet_at", now.strftime("%Y-%m-%dT%H:%M:%SZ"))
            return
        silent_minutes = (now - last).total_seconds() / 60.0
        if silent_minutes < cfg.alert_if_silent_minutes:
            return
        repeat = max(cfg.alert_repeat_minutes, cfg.alert_if_silent_minutes)
        if self._last_silence_alert and (now - self._last_silence_alert).total_seconds() < repeat * 60:
            return
        self._last_silence_alert = now
        title = f"静默告警 · {window} 时段没有收到数据包"
        body = (
            f"距最后一根数据包已 {silent_minutes:.0f} 分钟（阈值 {cfg.alert_if_silent_minutes}）。\n"
            "请检查：1) TradingView 警报是否还在 2) 隧道是否在线 3) 本机 webhook /health 是否正常。"
        )
        self.engine.notifier.send(title, body, level="warn")
        self.engine.store.record_alert("warn", title, body)

    # ------------------------------------------------------------ 日报
    def _report_time(self) -> tuple[int, int] | None:
        raw = self.config.watch.daily_report_at
        if not raw:
            return None
        try:
            hour, minute = (int(part) for part in raw.split(":", 1))
        except ValueError:
            return None
        return hour, minute

    def _initial_report_day(self) -> str:
        """启动时如果今天的日报时间已经过了，就当今天发过，避免每次重启刷一条。"""
        window = self._report_time()
        if window is None:
            return ""
        tzinfo, _exact = get_tz(self.config.watch.daily_report_tz)
        local = datetime.now(timezone.utc).astimezone(tzinfo)
        if local.hour * 60 + local.minute >= window[0] * 60 + window[1]:
            return local.strftime("%Y-%m-%d")
        return ""

    def _maybe_daily_report(self, now: datetime) -> None:
        window = self._report_time()
        if window is None:
            return
        hour, minute = window
        tzinfo, _exact = get_tz(self.config.watch.daily_report_tz)
        local = now.astimezone(tzinfo)
        day_key = local.strftime("%Y-%m-%d")
        if local.hour * 60 + local.minute < hour * 60 + minute or self._last_report_day == day_key:
            return

        since = now - timedelta(hours=24)
        report = self.engine.store.report_window(since)
        paper = self.engine.paper.summary(self.engine.store)
        lines = [
            f"过去 24 小时：收到 {sum(report['by_verdict'].values())} 个包",
            f"结论分布：{report['by_verdict'] or '无'}",
            f"事件分布：{report['by_event'] or '无'}",
            f"品种：{', '.join(f'{s}×{n}' for s, n in report['symbols']) or '无'}",
            "",
            f"纸面：已平 {paper['closed']} 笔 · 胜率 {paper['win_rate']:.1f}% · 合计 {paper['total_r']:+.2f}R · "
            f"权益 {paper['equity']:.2f} USDT",
            f"模式：{self.config.execution.mode}",
        ]
        title = f"每日战报 · {day_key}"
        self.engine.notifier.send(title, "\n".join(lines), level="info")
        self.engine.store.record_alert("info", title, "\n".join(lines))
        self._last_report_day = day_key
