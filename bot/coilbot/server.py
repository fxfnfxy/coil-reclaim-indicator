"""Webhook 接收服务（24/7 常驻）。

相比 examples/webhook_receiver.py 的加固点：
  - token 校验默认开启（配置了就要带），支持 ?token= 与 X-Token 头
  - 请求体上限、按 IP 限流、非法 JSON / 错 schema 都返回明确错误码
  - 处理在后台线程池里做（判单 + 通知可能几百毫秒，不能卡住 TradingView）
  - 幂等：同一根K线的重发包只会入库一次
  - 每个原始包另存到 run/packets/，TradingView 那边出问题也能补
  - /health 给监控用，返回存活、收包数、最后收包时间
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .config import Config
from .engine import Engine

LOGGER = logging.getLogger("coilbot.server")

VERSION = "1.0.0"


class RateLimiter:
    """按 key 的滑动窗口限流，防止误配或扫描把进程打满。"""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = max(1, per_minute)
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            bucket = self._hits.setdefault(key, deque())
            while bucket and now - bucket[0] > 60:
                bucket.popleft()
            if len(bucket) >= self.per_minute:
                return False
            bucket.append(now)
            return True


class Stats:
    def __init__(self) -> None:
        self.started_at = datetime.now(timezone.utc)
        self.received = 0
        self.accepted = 0
        self.rejected = 0
        self.duplicates = 0
        self.last_packet_at: str | None = None
        self.last_error: str | None = None
        self._lock = threading.Lock()

    def bump(self, field: str, amount: int = 1) -> None:
        with self._lock:
            setattr(self, field, getattr(self, field) + amount)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "started_at": self.started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "uptime_seconds": int((datetime.now(timezone.utc) - self.started_at).total_seconds()),
                "received": self.received,
                "accepted": self.accepted,
                "rejected": self.rejected,
                "duplicates": self.duplicates,
                "last_packet_at": self.last_packet_at,
                "last_error": self.last_error,
            }


class WebhookServer:
    def __init__(self, config: Config, engine: Engine) -> None:
        self.config = config
        self.engine = engine
        self.stats = Stats()
        self.limiter = RateLimiter(config.server.rate_limit_per_min)
        self.archive_dir = Path(config.store.archive_dir)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ 处理
    def process(self, body: str) -> tuple[int, dict]:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            self.stats.bump("rejected")
            self.stats.last_error = f"JSON 解析失败: {exc}"
            return 400, {"ok": False, "error": "不是合法 JSON"}
        if not isinstance(payload, dict):
            self.stats.bump("rejected")
            return 400, {"ok": False, "error": "body 必须是 JSON 对象"}

        # 原始包永久留档（TradingView 侧出问题时可补跑）
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        try:
            (self.archive_dir / f"{stamp}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            LOGGER.warning("归档失败：%s", exc)

        result = self.engine.handle_raw(payload)
        if result.error:
            self.stats.bump("rejected")
            self.stats.last_error = result.error
            return 400, {"ok": False, "error": result.error}
        if not result.stored:
            self.stats.bump("duplicates")
            return 200, {"ok": True, "duplicate": True}
        self.stats.bump("accepted")
        self.stats.last_packet_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return 200, {
            "ok": True,
            "verdict": result.decision.verdict if result.decision else None,
            "notified": bool(result.notify and result.notify.ok),
        }

    # ------------------------------------------------------------ 生命周期
    def build(self) -> ThreadingHTTPServer:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = f"coilbot/{VERSION}"
            sys_version = ""

            def _json(self, code: int, payload: dict) -> None:
                try:
                    data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
                except Exception as exc:  # noqa: BLE001 - 绝不能因为序列化把连接崩掉
                    code = 500
                    data = json.dumps({"ok": False, "error": f"序列化失败: {exc}"}, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _client(self) -> str:
                if outer.config.server.trust_proxy:
                    forwarded = self.headers.get("X-Forwarded-For", "")
                    if forwarded:
                        return forwarded.split(",")[0].strip()
                return self.client_address[0]

            def _authorized(self, query: dict) -> bool:
                expected = outer.config.server.token
                if not expected:
                    return True
                given = (query.get("token", [""])[0] or self.headers.get("X-Token", "")).strip()
                return given == expected

            def do_GET(self) -> None:
                parts = urlsplit(self.path)
                try:
                    if parts.path == "/health":
                        payload = outer.stats.snapshot()
                        payload["ok"] = True
                        payload["mode"] = outer.config.execution.mode
                        self._json(200, payload)
                    elif parts.path == "/status":
                        if not self._authorized(parse_qs(parts.query)):
                            return self._json(401, {"ok": False, "error": "token 不对"})
                        payload = outer.engine.status()
                        payload["server"] = outer.stats.snapshot()
                        self._json(200, payload)
                    else:
                        self._json(404, {"ok": False, "error": "not found"})
                except Exception as exc:  # noqa: BLE001
                    LOGGER.exception("GET %s 处理失败", parts.path)
                    self._json(500, {"ok": False, "error": str(exc)})

            def do_POST(self) -> None:
                parts = urlsplit(self.path)
                if parts.path.rstrip("/") != outer.config.server.path.rstrip("/"):
                    return self._json(404, {"ok": False, "error": "not found"})
                if not outer.limiter.allow(self._client()):
                    outer.stats.bump("rejected")
                    return self._json(429, {"ok": False, "error": "太快了"})
                if not self._authorized(parse_qs(parts.query)):
                    outer.stats.bump("rejected")
                    return self._json(401, {"ok": False, "error": "token 不对"})

                try:
                    size = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    size = 0
                limit = outer.config.server.max_body_kb * 1024
                if not 0 < size <= limit:
                    outer.stats.bump("rejected")
                    return self._json(400, {"ok": False, "error": f"body 为空或超过 {limit} 字节"})
                raw = self.rfile.read(size)
                try:
                    body = raw.decode("utf-8")
                except UnicodeDecodeError:
                    outer.stats.bump("rejected")
                    return self._json(400, {"ok": False, "error": "不是 UTF-8"})

                outer.stats.bump("received")
                try:
                    code, payload = outer.process(body)
                except Exception as exc:  # noqa: BLE001 - 单包出错不能拖垮服务
                    LOGGER.exception("处理数据包失败")
                    outer.stats.bump("rejected")
                    outer.stats.last_error = str(exc)
                    code, payload = 500, {"ok": False, "error": str(exc)}
                self._json(code, payload)

            def log_message(self, fmt: str, *args) -> None:
                LOGGER.debug("%s - %s", self._client(), fmt % args)

        httpd = ThreadingHTTPServer((self.config.server.host, self.config.server.port), Handler)
        httpd.daemon_threads = True
        return httpd

    def start_background(self) -> None:
        self.httpd = self.build()
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="webhook", daemon=True)
        self._thread.start()
        LOGGER.info(
            "监听 http://%s:%s%s（token %s，模式 %s）",
            self.config.server.host,
            self.config.server.port,
            self.config.server.path,
            "已启用" if self.config.server.token else "未设置",
            self.config.execution.mode,
        )

    def serve_forever(self) -> None:
        self.httpd = self.build()
        LOGGER.info(
            "监听 http://%s:%s%s（token %s，模式 %s）",
            self.config.server.host,
            self.config.server.port,
            self.config.server.path,
            "已启用" if self.config.server.token else "未设置",
            self.config.execution.mode,
        )
        try:
            self.httpd.serve_forever()
        finally:
            self.stop()

    def stop(self) -> None:
        if self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:  # noqa: BLE001
                pass
