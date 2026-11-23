"""通知：多通道 fan-out，单通道失败不影响其它通道，也不影响盯盘主流程。

支持的通道（填哪个开哪个）：
  telegram  —— Bot API，支持 http 代理
  dingtalk  —— 群机器人，支持加签
  wecom     —— 企业微信群机器人
  feishu    —— 飞书自定义机器人，支持加签
  smtp      —— 邮件
  desktop   —— Windows 弹窗（BurntToast / msg 兜底）+ 蜂鸣
  log       —— 永远写日志

所有请求都带超时和有限重试；失败原因写进返回值，由调用方落库。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import platform
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from .config import NotifyConfig

LOGGER = logging.getLogger("coilbot.notify")

LEVEL_ICON = {"info": "ℹ️", "signal": "🎯", "warn": "⚠️", "error": "🛑", "trade": "💰"}


@dataclass(slots=True)
class Delivery:
    channel: str
    ok: bool
    detail: str = ""


@dataclass(slots=True)
class NotifyResult:
    deliveries: list[Delivery] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return any(d.ok for d in self.deliveries)

    @property
    def configured(self) -> bool:
        return any(d.channel != "log" for d in self.deliveries)

    def summary(self) -> str:
        parts = [f"{d.channel}:{'ok' if d.ok else 'fail'}" for d in self.deliveries]
        return " ".join(parts) if parts else "无通道"


def _post_json(url: str, payload: dict, timeout: float, proxy: str = "") -> tuple[bool, str]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json; charset=utf-8"}, method="POST"
    )
    opener = urllib.request.build_opener()
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(2048).decode("utf-8", "replace")
            return response.status < 300, f"{response.status} {body[:200]}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code} {exc.read(200).decode('utf-8', 'replace')}"
    except Exception as exc:  # noqa: BLE001 - 网络错误种类多，统一降级为失败
        return False, f"{type(exc).__name__}: {exc}"


def _dingtalk_sign(secret: str) -> tuple[str, str]:
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), string_to_sign, hashlib.sha256).digest()
    return timestamp, urllib.parse.quote_plus(base64.b64encode(digest))


def _feishu_sign(secret: str) -> tuple[str, str]:
    timestamp = str(int(time.time()))
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(string_to_sign, b"", hashlib.sha256).digest()
    return timestamp, base64.b64encode(digest).decode("utf-8")


class Notifier:
    def __init__(self, config: NotifyConfig) -> None:
        self.config = config

    # ------------------------------------------------------------ public
    def send(self, title: str, body: str, level: str = "info") -> NotifyResult:
        result = NotifyResult()
        icon = LEVEL_ICON.get(level, "•")
        text = f"{icon} {title}\n{body}".strip()
        LOGGER.info("[%s] %s\n%s", level, title, body)

        if self.config.log_only:
            result.deliveries.append(Delivery("log", True, "写入日志"))

        for channel, sender in (
            ("telegram", self._telegram),
            ("dingtalk", self._dingtalk),
            ("wecom", self._wecom),
            ("feishu", self._feishu),
            ("smtp", self._smtp),
            ("desktop", self._desktop),
        ):
            if not sender:
                continue
            try:
                outcome = sender(title, body, text)
            except Exception as exc:  # noqa: BLE001
                outcome = (False, f"{type(exc).__name__}: {exc}")
            if outcome is None:            # 该通道没配置，静默跳过
                continue
            ok, detail = outcome
            if detail == "skipped":
                continue
            result.deliveries.append(Delivery(channel, ok, detail))
            if not ok:
                LOGGER.warning("通知通道 %s 失败：%s", channel, detail)
        return result

    def test(self) -> NotifyResult:
        return self.send(
            "Coil Bot 自检",
            "如果你看到这条消息，说明通知链路是通的。\n"
            f"时间 {time.strftime('%Y-%m-%d %H:%M:%S')}",
            level="info",
        )

    # ------------------------------------------------------------ channels
    def _telegram(self, title: str, body: str, text: str):
        cfg = self.config
        if not (cfg.telegram_bot_token and cfg.telegram_chat_id):
            return None
        return self._send_with_retry(
            "telegram",
            lambda: _post_json(
                f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage",
                {
                    "chat_id": cfg.telegram_chat_id,
                    "text": text[:4000],
                    "disable_web_page_preview": True,
                },
                cfg.timeout_seconds,
                cfg.telegram_proxy,
            ),
        )

    def _dingtalk(self, title: str, body: str, text: str):
        cfg = self.config
        if not cfg.dingtalk_webhook:
            return None
        url = cfg.dingtalk_webhook
        if cfg.dingtalk_secret:
            timestamp, sign = _dingtalk_sign(cfg.dingtalk_secret)
            joiner = "&" if "?" in url else "?"
            url = f"{url}{joiner}timestamp={timestamp}&sign={sign}"
        return self._send_with_retry(
            "dingtalk",
            lambda: _post_json(
                url,
                {"msgtype": "text", "text": {"content": text[:4000]}},
                cfg.timeout_seconds,
            ),
        )

    def _wecom(self, title: str, body: str, text: str):
        cfg = self.config
        if not cfg.wecom_webhook:
            return None
        return self._send_with_retry(
            "wecom",
            lambda: _post_json(
                cfg.wecom_webhook,
                {"msgtype": "text", "text": {"content": text[:4000]}},
                cfg.timeout_seconds,
            ),
        )

    def _feishu(self, title: str, body: str, text: str):
        cfg = self.config
        if not cfg.feishu_webhook:
            return None
        payload = {"msg_type": "text", "content": {"text": text[:4000]}}
        if cfg.feishu_secret:
            timestamp, sign = _feishu_sign(cfg.feishu_secret)
            payload["timestamp"] = timestamp
            payload["sign"] = sign
        return self._send_with_retry(
            "feishu",
            lambda: _post_json(cfg.feishu_webhook, payload, cfg.timeout_seconds),
        )

    def _smtp(self, title: str, body: str, text: str):
        cfg = self.config
        if not (cfg.smtp_host and cfg.smtp_to and cfg.smtp_user):
            return None

        def send() -> tuple[bool, str]:
            import smtplib
            from email.message import EmailMessage

            message = EmailMessage()
            message["Subject"] = f"[CoilBot] {title}"
            message["From"] = cfg.smtp_user
            message["To"] = cfg.smtp_to
            message.set_content(body)
            if cfg.smtp_ssl:
                server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=cfg.timeout_seconds)
            else:
                server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=cfg.timeout_seconds)
                try:
                    server.starttls()
                except smtplib.SMTPException:
                    pass
            try:
                server.login(cfg.smtp_user, cfg.smtp_password)
                server.send_message(message)
            finally:
                try:
                    server.quit()
                except smtplib.SMTPException:
                    pass
            return True, "sent"

        return self._send_with_retry("smtp", send)

    def _desktop(self, title: str, body: str, text: str):
        cfg = self.config
        if not cfg.desktop:
            return None
        if platform.system() != "Windows":
            return False, "非 Windows，跳过弹窗"

        def send() -> tuple[bool, str]:
            safe_title = title.replace('"', "'").replace("\n", " ")[:60]
            safe_body = body.replace('"', "'").replace("\n", " ")[:180]
            script = (
                "try {"
                " Import-Module BurntToast -ErrorAction Stop;"
                f' New-BurntToastNotification -Text "{safe_title}","{safe_body}" -ErrorAction Stop'
                "} catch {"
                f' $wshell = New-Object -ComObject Wscript.Shell; $wshell.Popup("{safe_title} - {safe_body}", 8) | Out-Null'
                "}"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                timeout=20,
                check=False,
            )
            try:
                import winsound

                winsound.MessageBeep(0x40)
            except Exception:  # noqa: BLE001
                pass
            return True, "弹窗已发送"

        return self._send_with_retry("desktop", send)

    # ------------------------------------------------------------ helper
    def _send_with_retry(self, channel: str, action) -> tuple[bool, str]:
        attempts = max(1, self.config.retry + 1)
        detail = ""
        for attempt in range(attempts):
            ok, detail = action()
            if ok:
                return True, detail
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
        return False, detail
