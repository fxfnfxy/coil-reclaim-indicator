"""命令行入口。

    python -m coilbot serve              # 24/7 常驻（webhook + 守护线程）
    python -m coilbot judge 包.json       # 判一个包，打印结论
    python -m coilbot replay 目录/        # 批量回放（演习/回归）
    python -m coilbot doctor             # 自检：配置、端口、时区、通知
    python -m coilbot notify-test        # 发一条测试通知
    python -m coilbot status             # 库里的统计 + 纸面汇总
    python -m coilbot demo               # 用示例包跑一遍完整流程（不联网）
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import Config, load_config, ensure_dirs
from .engine import Engine
from .judge import judge
from .models import load_packet_file, parse_packet
from .store import Store

LOGGER = logging.getLogger("coilbot")


def setup_logging(level: str = "INFO", log_dir: str = "run/logs") -> Path:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    log_path = Path(log_dir) / "coilbot.log"
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s", "%H:%M:%S"))
    root.addHandler(console)

    rotating = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    rotating.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s"))
    root.addHandler(rotating)
    return log_path


def build_engine(config: Config) -> Engine:
    ensure_dirs(config)
    return Engine(config)


# ---------------------------------------------------------------- 子命令
def cmd_serve(args: argparse.Namespace) -> int:
    from .server import WebhookServer
    from .watch import Watchdog

    config = load_config(args.config)
    if args.mode:
        config.execution.mode = args.mode
    if args.port:
        config.server.port = args.port
    if args.equity:
        config.account.equity_usdt = args.equity

    setup_logging(args.log_level, str(Path(config.watch.state_dir) / "logs"))
    engine = build_engine(config)
    LOGGER.info("启动 coilbot v1.0 · 配置 %s · 模式 %s", config.source_path or "默认值", config.execution.mode)
    if not config.server.token:
        LOGGER.warning("没有设置 token，任何知道地址的人都能发包（建议在 [server] token 里设一个）")
    if config.execution.mode == "live" and not config.execution.live_dry_run:
        LOGGER.warning("实盘模式已开启（live_dry_run=false）：信号会真的下单")

    server = WebhookServer(config, engine)
    try:
        server.start_background()
    except OSError as exc:
        LOGGER.error("端口 %s 起不来：%s", config.server.port, exc)
        return 2

    watchdog = Watchdog(config, engine, webhook_alive=lambda: server.httpd is not None)
    watchdog.start()

    # 每小时打一次心跳日志，便于确认进程没僵
    LOGGER.info(
        "就绪：webhook http://%s:%s%s/health · 库里已有 %s 个包",
        config.server.host,
        config.server.port,
        "",
        engine.store.stats()["packets"],
    )
    try:
        while True:
            server._thread.join(timeout=3600)  # noqa: SLF001 - 前台等待
            if not server._thread.is_alive():
                LOGGER.error("webhook 线程退出，进程结束")
                return 3
            LOGGER.info("心跳：已收 %s 包，最近 %s", engine.store.stats()["packets"],
                        engine.store.get_kv("last_packet_at"))
    except KeyboardInterrupt:
        LOGGER.info("收到 Ctrl+C，正在停止")
    finally:
        watchdog.stop()
        server.stop()
        engine.store.close()
    return 0


def cmd_judge(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.mode:
        config.execution.mode = args.mode
    packet = load_packet_file(args.path)
    decision = judge(packet, config, allow_stale=args.allow_stale)
    print(decision.render())
    if args.json:
        print()
        print(json.dumps(
            {
                "verdict": decision.verdict,
                "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in decision.checks],
                "plan": decision.plan,
            },
            ensure_ascii=False,
            indent=2,
        ))
    return 0 if decision.verdict != "SKIP" else 1


def cmd_replay(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.mode:
        config.execution.mode = args.mode
    setup_logging("WARNING")
    engine = build_engine(config)
    folder = Path(args.path)
    files = sorted(folder.glob("*.json")) if folder.is_dir() else [folder]
    if not files:
        print(f"没找到 json：{folder}")
        return 1
    ok = dup = bad = 0
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"✗ {path.name}: {exc}")
            bad += 1
            continue
        result = engine.handle_raw(payload)
        if result.error:
            print(f"✗ {path.name}: {result.error}")
            bad += 1
        elif result.stored:
            ok += 1
            print(f"✓ {path.name}: {result.decision.verdict}  {result.decision.reasons[:1]}")
        else:
            dup += 1
            print(f"= {path.name}: 重发跳过")
    print(f"\n回放完成：新增 {ok}，重复 {dup}，失败 {bad}")
    print(engine.paper.render(engine.store))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    print("== 配置 ==")
    print(f"配置文件        : {config.source_path or '未找到（用默认值）'}")
    print(f"模式            : {config.execution.mode}")
    print(f"监听            : {config.server.host}:{config.server.port}{config.server.path}")
    print(f"token           : {'已设置' if config.server.token else '未设置（任何人都能发包）'}")
    print(f"账户权益        : {config.account.equity_usdt:g} USDT")
    print(f"默认风险        : {config.account.default_risk_pct:g}%")
    print(f"最低目标 R      : {config.account.min_target_r:g}R")
    print(f"数据库          : {Path(config.store.db_path).resolve()}")

    print("\n== 环境 ==")
    print(f"Python          : {sys.version.split()[0]}")
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo("Asia/Tokyo")
        print("时区数据        : 正常")
    except Exception as exc:  # noqa: BLE001
        print(f"时区数据        : 缺失（{exc}）→ pip install tzdata")

    print("\n== 端口 ==")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.6)
        busy = sock.connect_ex((config.server.host if config.server.host != "0.0.0.0" else "127.0.0.1",
                                config.server.port)) == 0
    print(f"{config.server.port:<15}: {'已被占用（可能已经在跑）' if busy else '空闲'}")

    print("\n== 通知通道 ==")
    channels = {
        "telegram": bool(config.notify.telegram_bot_token and config.notify.telegram_chat_id),
        "dingtalk": bool(config.notify.dingtalk_webhook),
        "wecom": bool(config.notify.wecom_webhook),
        "feishu": bool(config.notify.feishu_webhook),
        "smtp": bool(config.notify.smtp_host and config.notify.smtp_to),
        "desktop": config.notify.desktop,
        "log": config.notify.log_only,
    }
    for name, on in channels.items():
        print(f"{name:<15}: {'开' if on else '关'}")
    if not any(channels.values()):
        print("→ 一个都没开，信号只会写日志。先填 [notify] telegram_bot_token / telegram_chat_id")

    print("\n== 品种精度 ==")
    if config.instruments:
        for name, inst in config.instruments.items():
            print(f"{name:<20}: step={inst.qty_step:g} min={inst.min_qty:g} tick={inst.price_tick:g}")
    else:
        print("未配置 [instruments]，将按价格量级推断步长（实盘前建议写准）")

    print("\n== 数据库 ==")
    store = Store(config.store.db_path)
    stats = store.stats()
    print(f"数据包          : {stats['packets']} 个")
    print(f"交易            : {stats['trades'] or '无'}")
    print(f"最后收包        : {stats['last_packet_at'] or '还没收到过'}")
    print(f"最后心跳        : {stats['last_heartbeat'] or '无'}")
    store.close()

    print("\n== 下一步 ==")
    if not config.server.token:
        print("1. 在 config.toml 设 [server] token = \"一串随机字符\"")
    print("2. 建 https 隧道：cloudflared tunnel --url http://localhost:%d" % config.server.port)
    print("3. 把 https://<隧道地址>%s?token=<你的token> 填进 TradingView 警报的 Webhook URL" % config.server.path)
    print("4. 启动：python -m coilbot serve")
    return 0


def cmd_notify_test(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    setup_logging("INFO", str(Path(config.watch.state_dir) / "logs"))
    engine = build_engine(config)
    result = engine.notifier.test()
    print(f"通道结果：{result.summary()}")
    for delivery in result.deliveries:
        print(f"  {delivery.channel:<10} {'OK ' if delivery.ok else 'FAIL'} {delivery.detail}")
    if not result.configured:
        print("没有任何通道被配置，只写了日志。")
    return 0 if result.ok else 1


def cmd_status(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    store = Store(config.store.db_path)
    engine = Engine(config, store=store)
    print(f"模式：{config.execution.mode}")
    print(json.dumps(store.stats(), ensure_ascii=False, indent=2, default=str))
    print()
    print(engine.paper.render(store))
    print("\n最近 10 个包：")
    for row in store.recent_packets(10):
        print(f"  {row['created_at']}  {row['event']:<22} {row['symbol']:<20} {row['verdict']:<6} {row['bias']}")
    print("\n最近 10 笔：")
    for row in store.trade_history(10):
        print(f"  {row['opened_at']}  {row['mode']:<7} {row['symbol']:<12} {row['side']:<5} "
              f"{row['status']:<9} qty={row['qty']} result={row['result_r']}")
    store.close()
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """用示例包跑完整流程：判单 → 记账 → 打印。不联网、不发通知。"""
    config = load_config(args.config)
    config.execution.mode = "paper"
    config.notify.log_only = True
    config.notify.desktop = False
    config.notify.telegram_bot_token = ""
    config.notify.dingtalk_webhook = ""
    config.notify.wecom_webhook = ""
    config.notify.feishu_webhook = ""
    config.notify.smtp_host = ""
    config.judge.stale_minutes = 100_000  # 示例包是过去的时间，跳新鲜度
    config.store.db_path = str(Path(config.watch.state_dir) / "demo.db")
    setup_logging("WARNING")

    root = Path(__file__).resolve().parent.parent.parent
    sample_path = root / "examples" / "sample_packet.json"
    if not sample_path.is_file():
        print(f"找不到示例包：{sample_path}")
        return 1
    payload = json.loads(sample_path.read_text(encoding="utf-8"))
    engine = build_engine(config)
    result = engine.handle_raw(payload)
    if result.error:
        print(f"判单失败：{result.error}")
        return 1
    print(result.decision.render())
    print()
    print("— 接下来模拟一笔完整生命周期 —")
    entry_payload = dict(payload, event="ENTRY", status="OPEN", entry=payload["order_price"],
                         rubber_band=True, stop=payload["stop"],
                         take_profit_1r=payload["order_price"] + (payload["order_price"] - payload["stop"]),
                         ts=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    engine.handle_raw(entry_payload)
    exit_payload = dict(payload, event="EXIT", status="CLOSED", entry=payload["order_price"],
                        rubber_band=True, exit_price=payload["next_ceiling"],
                        result_r=1.85, note="到达4小时天花板",
                        ts=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    engine.handle_raw(exit_payload)
    print(engine.paper.render(engine.store))
    return 0


# ---------------------------------------------------------------- 解析
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coilbot", description="Coil Reclaim 24/7 盯盘机器人")
    parser.add_argument("--config", help="配置文件路径（默认 ./config.toml）")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="常驻运行 webhook + 守护线程")
    serve.add_argument("--port", type=int, help="覆盖监听端口")
    serve.add_argument("--mode", choices=["signal", "paper", "live"], help="覆盖执行模式")
    serve.add_argument("--equity", type=float, help="覆盖账户权益")
    serve.add_argument("--log-level", default="INFO")
    serve.set_defaults(func=cmd_serve)

    judge_cmd = sub.add_parser("judge", help="判一个数据包")
    judge_cmd.add_argument("path")
    judge_cmd.add_argument("--mode", choices=["signal", "paper", "live"])
    judge_cmd.add_argument("--json", action="store_true", help="同时输出机器可读结果")
    judge_cmd.add_argument("--allow-stale", action="store_true", help="忽略新鲜度检查（回放用）")
    judge_cmd.set_defaults(func=cmd_judge)

    replay = sub.add_parser("replay", help="回放目录里的数据包")
    replay.add_argument("path")
    replay.add_argument("--mode", choices=["signal", "paper", "live"], default="paper")
    replay.set_defaults(func=cmd_replay)

    doctor = sub.add_parser("doctor", help="自检")
    doctor.set_defaults(func=cmd_doctor)

    notify_test = sub.add_parser("notify-test", help="发测试通知")
    notify_test.set_defaults(func=cmd_notify_test)

    status = sub.add_parser("status", help="统计与纸面汇总")
    status.set_defaults(func=cmd_status)

    demo = sub.add_parser("demo", help="用示例包跑一遍完整流程")
    demo.set_defaults(func=cmd_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
