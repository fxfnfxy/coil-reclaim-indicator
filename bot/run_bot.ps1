# 启动 / 看护 24/7 盯盘机器人（Windows）
#   .\run_bot.ps1               前台带看护运行（Ctrl+C 停止）
#   .\run_bot.ps1 -Once         只跑一次，退出就退出（调试用）
# 真实 24/7 请用 .\install_service.ps1 注册成计划任务，登录即启、崩了自动拉起。

[CmdletBinding()]
param(
    [string]$ConfigPath = "config.toml",
    [switch]$Once,
    [string]$Mode,           # signal | paper | live
    [int]$Port = 0,
    [int]$RestartDelaySeconds = 10
)

$ErrorActionPreference = "Continue"
$BotDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $BotDir
New-Item -ItemType Directory -Force -Path (Join-Path $BotDir "run") | Out-Null
$outLog = Join-Path $BotDir "run\bot.out"
$errLog = Join-Path $BotDir "run\bot.err"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
$env:PYTHONPATH = $BotDir

# 首次运行：生成配置文件
if (-not (Test-Path $ConfigPath)) {
    Copy-Item "config.example.toml" $ConfigPath
    Write-Host "[初始化] 已生成 $ConfigPath（先填 [server] token 和 [notify] 再启动）" -ForegroundColor Yellow
}

# 找 Python
$python = $null
foreach ($candidate in @("python", "python3")) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) { $python = $cmd.Source; break }
}
if (-not $python) {
    "[$(Get-Date -Format s)] 找不到 python，请先安装 Python 3.11+" | Out-File $errLog -Append -Encoding utf8
    throw "找不到 python，请先安装 Python 3.11+"
}

$botArgs = @("-m", "coilbot", "--config", $ConfigPath, "serve")
if ($Mode) { $botArgs += @("--mode", $Mode) }
if ($Port -gt 0) { $botArgs += @("--port", "$Port") }

if ($Once) {
    & $python @botArgs
    exit $LASTEXITCODE
}

$failures = 0
while ($true) {
    $started = Get-Date
    "[$(Get-Date -Format s)] 启动：$python $($botArgs -join ' ')" | Out-File $outLog -Append -Encoding utf8
    try {
        # 显式重定向：计划任务下没有控制台，不能让子进程往无效句柄写
        $proc = Start-Process -FilePath $python -ArgumentList $botArgs `
            -NoNewWindow -Wait -PassThru `
            -RedirectStandardOutput $outLog -RedirectStandardError $errLog
        $code = $proc.ExitCode
    } catch {
        "[$(Get-Date -Format s)] 启动异常：$_" | Out-File $errLog -Append -Encoding utf8
        $code = 1
    }

    $ran = ((Get-Date) - $started).TotalSeconds
    if ($ran -gt 300) { $failures = 0 }
    $failures++
    if ($failures -gt 6) {
        $delay = 300
        "[$(Get-Date -Format s)] 连续失败 $failures 次，退避到 5 分钟后重启" | Out-File $outLog -Append -Encoding utf8
    } else {
        $delay = [Math]::Min(300, $RestartDelaySeconds * $failures)
    }
    "[$(Get-Date -Format s)] 进程退出（code=$code，运行 $([int]$ran)s），${delay}s 后重启" |
        Out-File $outLog -Append -Encoding utf8
    Start-Sleep -Seconds $delay
}
