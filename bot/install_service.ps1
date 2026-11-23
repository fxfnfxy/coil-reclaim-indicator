# 把盯盘机器人注册成 Windows 计划任务：登录即启、崩了自动拉起、开机不用手动点。
#
#   注册（当前用户，登录后自动运行）：
#       .\install_service.ps1
#   开机就运行（需要管理员 PowerShell）：
#       .\install_service.ps1 -AtStartup
#   立即启动 / 停止 / 查看：
#       .\install_service.ps1 -Start
#       .\install_service.ps1 -Stop
#       Get-ScheduledTask -TaskName CoilBot | Get-ScheduledTaskInfo
#   删除：
#       .\install_service.ps1 -Unregister

[CmdletBinding()]
param(
    [string]$TaskName = "CoilBot",
    [string]$ConfigPath = "config.toml",
    [switch]$AtStartup,
    [switch]$Start,
    [switch]$Stop,
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"
$BotDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$runScript = Join-Path $BotDir "run_bot.ps1"
if (-not (Test-Path $runScript)) { throw "找不到 $runScript" }

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal $identity).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
}

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName "$TaskName-Watch" -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "已删除计划任务 $TaskName 与 $TaskName-Watch" -ForegroundColor Yellow
    return
}
if ($Stop) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Write-Host "已停止 $TaskName" -ForegroundColor Yellow
    return
}
if ($Start) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "已启动 $TaskName" -ForegroundColor Green
    return
}

# 首次运行先把配置准备好
$configFull = Join-Path $BotDir $ConfigPath
if (-not (Test-Path $configFull)) {
    Copy-Item (Join-Path $BotDir "config.example.toml") $configFull
    Write-Host "[初始化] 已生成 $configFull —— 请先填 [server] token 与 [notify]，再 -Start" -ForegroundColor Yellow
}

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runScript`" -ConfigPath `"$ConfigPath`"" `
    -WorkingDirectory $BotDir

if ($AtStartup) {
    if (-not (Test-Admin)) { throw "-AtStartup 需要管理员 PowerShell" }
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    $trigger.Delay = "PT30S"
} else {
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive -RunLevel Limited
    $trigger.Delay = "PT20S"
}

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -DontStopOnIdleEnd `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings `
    -Description "Coil Reclaim 24/7 盯盘机器人（webhook + 规则复核 + 通知）" -Force | Out-Null

# ---- 看门狗：每 5 分钟健康检查，不健康就拉起主任务 ----
# 计划任务自己只在「启动失败」时重启；进程被外部结束（Ctrl+C / 任务管理器 / 系统更新）
# 之后任务会变成 Ready 不再自起，这个洞由它补。
$watchScript = Join-Path $BotDir "health_check.ps1"
$watchOnline = $false
if (Test-Path $watchScript) {
    $portFromConfig = 18080
    if (Test-Path $configFull) {
        $m = Select-String -Path $configFull -Pattern '^\s*port\s*=\s*(\d+)' | Select-Object -First 1
        if ($m) { $portFromConfig = [int]$m.Matches[0].Groups[1].Value }
    }
    $watchAction = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$watchScript`" -Quiet -Port $portFromConfig -TaskName $TaskName" `
        -WorkingDirectory $BotDir
    $watchTrigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(3)) `
        -RepetitionInterval (New-TimeSpan -Minutes 5) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
    $watchSettings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
    try {
        Register-ScheduledTask -TaskName "$TaskName-Watch" -Action $watchAction -Trigger $watchTrigger `
            -Principal $principal -Settings $watchSettings `
            -Description "看门狗：每 5 分钟检查 CoilBot 健康，不通则重启" -Force | Out-Null
        $watchOnline = $true
    } catch {
        Write-Host "看门狗注册失败（不影响主任务）：$_" -ForegroundColor Yellow
    }
}

Write-Host "已注册计划任务：$TaskName" -ForegroundColor Green
Write-Host "  触发    ：$(if ($AtStartup) { '开机后 30 秒' } else { '登录后 20 秒' })"
Write-Host "  失败重启：每 1 分钟，最多 999 次"
Write-Host "  看门狗  ：$(if ($watchOnline) { "$TaskName-Watch 每 5 分钟健康检查，挂了自动拉起" } else { '未注册' })"
Write-Host "  运行脚本：$runScript"
Write-Host ""
Write-Host "下一步：" -ForegroundColor Cyan
Write-Host "  1) 编辑 $configFull 填 token / notify"
Write-Host "  2) Start-ScheduledTask -TaskName $TaskName"
Write-Host "  3) 健康检查：curl http://127.0.0.1:18080/health"
