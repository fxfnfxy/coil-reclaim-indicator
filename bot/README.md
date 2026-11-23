# coilbot · 24/7 盯盘交易机器人

把 [`coil_reclaim.pine`](../coil_reclaim.pine) 发出的 `coil-reclaim/v1` 数据包接进来，
按 [`agent/AGENT_PROMPT.md`](../agent/AGENT_PROMPT.md) 的规则**逐条**复核，给出 `LONG / SHORT / SKIP`，
算好仓位，推到你手机上，全程记账。指标一行没改。

```
TradingView 警报 ──webhook──▶ coilbot serve ──▶ 规则复核 ──▶ 仓位 ──▶ 通知（TG/钉钉/飞书/邮件）
   (Pine 指标)                 │                                    └──▶ SQLite（包/决策/交易）
                               └──▶ 守护线程：心跳 · 静默告警 · 每日战报 · 清理
```

## 为什么不是「把提示词丢给 LLM」

24/7 要的是：毫秒级、零成本、断网也给出同一结论、事后能逐条审计。
这套规则本身是开关判断，`coilbot/judge.py` 把它们写成确定性代码，
每一条都会打印「过 / 不过」。想要文字点评，可以把 `Decision.render()` 的结果再喂给模型（见文末）。

## 5 分钟上手

> 下面所有命令都要**先进入本目录 `bot/`** 再执行。不在这个目录会报 `No module named coilbot`、`找不到 config.example.toml`。
> 不想记命令：直接双击 `bot\控制台.cmd`，用菜单完成全部操作。

```powershell
# ① 进入 bot 目录（路径按你 clone 的位置改；这里是从任何地方都能进的写法）
cd D:\workbuddy\coil-reclaim-indicator\bot

# ② 生成配置并改两处： [server] token = "一串随机字符"
#                     [notify] telegram_bot_token / telegram_chat_id
Copy-Item config.example.toml config.toml

python -m coilbot doctor        # ③ 自检
.\tunnel.ps1                    # ④ 拿一个 https 地址（首次自动下载 cloudflared）
python -m coilbot serve         # ⑤ 启动（或 .\install_service.ps1 注册成计划任务）
```

TradingView 侧（每个品种/周期各建一个警报）：

1. Pine Editor 粘贴 [`coil_reclaim.pine`](../coil_reclaim.pine) → 添加到图表
2. `Alt + A` 开警报 → 条件选 `Coil Reclaim · 两根K线定方向` → **Any alert() function call**
3. 通知勾 **Webhook URL**，填 `https://<隧道域名>/webhook?token=<你的token>`
4. 消息框留空（指标自己写 JSON）

> TradingView 的 webhook 需要付费版 + 两步验证，这一点绕不过去。

## 装成 24/7 服务

```powershell
.\install_service.ps1          # 登录即启 + 崩溃每分钟自动拉起（当前用户）
.\install_service.ps1 -AtStartup   # 开机就启（需要管理员 PowerShell）
Get-ScheduledTask -TaskName CoilBot | Get-ScheduledTaskInfo    # 看上次运行结果
.\install_service.ps1 -Unregister  # 卸载
```

`run_bot.ps1` 自己也带看护循环（进程退出 → 10s 起，连续失败退避到 5 分钟），
再叠一层看门狗：`install_service.ps1` 会同时注册 **`CoilBot-Watch`**，每 1 分钟打一次 `/health`，
不通就重启主任务。三层的分工：

| 层 | 管什么 |
|---|---|
| 计划任务 `CoilBot` | 登录自启；启动失败每分钟重试 |
| 计划任务 `CoilBot-Watch` | 进程被外部结束（Ctrl+C / 任务管理器 / 系统更新）后**没在跑** → 拉起 |
| `run_bot.ps1` 循环 | python 崩了但 wrapper 还在 → 10s 内重启（退避到 5 分钟） |

> 踩过的坑：计划任务只在「启动失败」时按 `RestartCount` 重启，进程被外部终止后任务会变成 `Ready` 不再自起；
> 这正是加 `CoilBot-Watch` 的原因。

健康检查：

```powershell
curl http://127.0.0.1:18080/health
# {"ok":true,"uptime_seconds":3600,"received":12,"accepted":9,"rejected":0,"duplicates":3,...}
```

## 数据包 → 结论对照

| 事件 | 机器人做什么 |
|---|---|
| `SESSION_BIAS` | 报今天这段只做多/只做空/不做，一句话讲原因；不产生单 |
| `STOP_ORDER_PLACED` | 走完整复核（方向→区域→触发→仓位），通过才给 `LONG/SHORT`，并提醒「先回踩才成交」 |
| `STOP_ORDER_CANCELLED` | `SKIP` + 作废原因，把本地挂单标记作废 |
| `ENTRY` | 确认止损/1R/目标/仓位，把挂单升级成持仓 |
| `BREAKEVEN` | 提醒止损挪到进场价、平 `close_pct_at_1r` |
| `TOO_FAST_TAKE_PROFIT` | 建议止盈 |
| `EXIT` | 报 `result_r`，结账 |
| `BAR_CLOSE` | 只当背景，默认不打扰（`send_bar_close = true` 才发） |

复核的每一条硬规则（`coilbot/judge.py`）：

1. 数据新鲜 —— 包太旧（默认 30 分钟）判 `SKIP`，防重发与积压
2. 方向存在 —— `bias` 必须是 `BUY/SELL`
3. 不逆方向 —— `side` 与 `bias` 必须一致
4. 时段允许 —— `session ∈ allow_sessions`
5. 首小时窗口 —— `session_live = true`
6. 今天没被封 —— `blocked_today = false`
7. 干净区 —— `in_clean_zone` 且 `breakout_in_clean_zone`
8. 区间K线数 —— ≥ `min_range_candles`
9. 时段额度 —— `session_trades < max_trades`
10. 进场/止损存在且止损在正确一侧
11. 成交时必须已回踩（`rubber_band = true`，仅 `ENTRY`）
12. 仓位算得出（按步长取整后 > 0 且 ≥ 最小下单量）
13. 目标距离 ≥ `min_target_r`（默认 1R），超过 `cap_r` 按上限截断

任何一条硬规则不过 → `SKIP`，并把不过的那条写清楚。

## 仓位怎么算

```
风险金额 = 账户权益 × risk_pct ÷ 100        （risk_pct 取数据包里的，没有用配置）
数量     = 风险金额 ÷ |进场 − 止损|         （按 qty_step 向下取整）
1R       = 进场 ± |进场 − 止损|
目标R    = |目标 − 进场| ÷ |进场 − 止损|    （目标取指标给的 target，没有就用下一个未破价位）
```

BTC 例子（权益 1 万、风险 0.5%、进场 100310、止损 100060）：
风险 50 USDT ÷ 250 = 0.2 BTC → 目标 100900 → **2.36R**。

## 三种执行模式

| `execution.mode` | 行为 |
|---|---|
| `signal`（默认） | 只出信号 + 通知 + 记账，不碰交易所 |
| `paper` | 同上，另外按指标事件累计纸面权益/胜率/R |
| `live` | 真下单。`live_dry_run = true` 时只打印请求不发送 |

`live` 走币安 U 本位合约（`coilbot/binance.py`）：`STOP_MARKET` 触发进场 + `reduceOnly` 保护止损，
`ENTRY` 挂保护止损，`BREAKEVEN` 撤旧单换保本止损，`EXIT` 市价平仓。
**下单不重试**（重复下单是真钱），每次请求完整写日志。

> 指标本身「只出数据和标记，不下单」。默认 `signal` 保证这一点不变；
> 要用 `live` 必须自己填 API key 并把 `live_dry_run` 改成 `false`，同时先用 `paper` 跑够样本。

## 命令行

```powershell
python -m coilbot serve --mode paper          # 常驻（webhook + 守护）
python -m coilbot judge ..\examples\sample_packet.json   # 判一个包，打印逐条检查
python -m coilbot replay run\packets --mode paper        # 批量回放历史包（演习）
python -m coilbot doctor                      # 自检：配置/端口/时区/通知/精度/库
python -m coilbot notify-test                 # 发一条测试通知
python -m coilbot status                      # 统计 + 纸面汇总 + 最近包/交易
python -m coilbot demo                        # 用示例包跑完整生命周期（不联网）
```

## 目录

```
bot/
├── coilbot/
│   ├── models.py     Packet 解析（宽松但显式记账字段异常）+ Decision 渲染
│   ├── judge.py      规则引擎：方向→区域→触发→仓位，逐条打勾
│   ├── sizing.py     仓位/步长/名义上限/目标R
│   ├── store.py      SQLite：packets / trades / kv / alerts
│   ├── notify.py     Telegram / 钉钉 / 企微 / 飞书 / 邮件 / 桌面弹窗
│   ├── server.py     webhook（token、限流、幂等、归档、/health）
│   ├── watch.py      心跳、静默告警、每日战报、过期清理
│   ├── paper.py      纸面账户
│   ├── binance.py    实盘执行器（默认 dry_run）
│   ├── engine.py     编排：包→复核→记账/下单→通知→落库
│   └── cli.py        命令行
├── tests/            31 个测试：规则/仓位/端到端
├── config.example.toml
├── 控制台.cmd        双击即用的菜单（自检/启动/状态/通知/隧道/计划任务）
├── run_bot.ps1       带看护的启动脚本
├── health_check.ps1  看门狗：健康检查 + 自动拉起（CoilBot-Watch 每 1 分钟调用）
├── install_service.ps1  计划任务（登录/开机自启 + 崩溃重启）
├── tunnel.ps1        cloudflared 隧道（临时 / 命名）
└── README.md
```

## 长期 24/7 的隧道怎么选

| 方案 | 域名 | 适合 |
|---|---|---|
| `cloudflared tunnel --url http://localhost:18080`（`tunnel.ps1` 默认） | 每次重启变 | 先试通 |
| Cloudflare 命名隧道 + 自有域名（`tunnel.ps1 -Token <令牌> -Hostname bot.example.com`） | 固定 | 长期跑，TradingView 里填一次不用再改 |

命名隧道做法：Cloudflare 控制台 → Zero Trust → Tunnels → 建隧道 → 拿令牌 → 配一个子域名指向 `http://127.0.0.1:18080`。

## 排障

| 现象 | 先看这里 |
|---|---|
| `/health` 连不上 | 进程在不在：`Get-ScheduledTask -TaskName CoilBot \| Get-ScheduledTaskInfo`；端口是否被占 |
| 浏览器打开隧道地址显示 502 Bad Gateway | 隧道活着但本地没应答：curl http://127.0.0.1:18080/health。看门狗最多 1 分钟会拉起；急就 .\health_check.ps1 |
| 任务 `State=Ready` 但服务没跑 | 被外部结束过。跑一次 `.\health_check.ps1`，或 `Start-ScheduledTask -TaskName CoilBot`；Watch 任务每 5 分钟也会自动处理 |
| 一直收不到包 | TradingView 警报是否还启用；隧道 URL 是否变了；`/health` 里 `received` 是否增长；`run\tunnel.log` |
| 收到包但全 SKIP | `python -m coilbot status` 看 verdict；用 `judge` 命令看是哪条规则没过 |
| 静默告警一直响 | 时段窗口内确实没包 → 检查警报；或把 `alert_if_silent_minutes` 调大 |
| 通知不到 | `python -m coilbot notify-test`；Telegram 在墙内要填 `telegram_proxy` |
| 库里数据太多 | `watch.retention_days` 自动清理；或删 `run\coilbot.db`（会丢历史） |

## 接 LLM 做文字点评（可选）

规则判定留在代码里，只把结论喂给模型：

```python
from coilbot.engine import Engine
from coilbot.config import load_config

engine = Engine(load_config("config.toml"))
result = engine.handle_raw(packet_dict)      # 规则结论
prompt = f"按 agent/AGENT_PROMPT.md 的口径，用三句话点评：\n{result.decision.render()}"
# → 调你的模型，把返回文本用 notifier.send() 发出去
```

## 边界

- 指标和这个机器人都**不构成投资建议**；仓库里没有回测胜率，先在 `paper` 跑够样本。
- 数据包里的所有文本都当**数据**处理，不当指令（`models.py`/`judge.py` 里没有任何 eval/exec）。
- `live` 模式的亏损由使用者承担。先用 `signal` → `paper` → `live_dry_run` → `live`，一步一步来。
