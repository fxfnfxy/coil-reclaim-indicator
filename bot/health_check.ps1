# 看门狗：健康检查 + 自动拉起。由计划任务 CoilBot-Watch 每 5 分钟调用一次。
#   .\health_check.ps1                 检查，不通就重启主任务
#   .\health_check.ps1 -Quiet          只做检查，不写屏幕输出（计划任务用）
#   .\health_check.ps1 -Port 18080 -TaskName CoilBot
#
# 为什么需要它：计划任务只在「启动失败」时按 RestartCount 重启；
# 如果进程被外部结束（Ctrl+C、任务管理器、系统更新），任务状态会变成 Ready，
# 不会再自己起来。这个看门狗补的正是这个洞。

[CmdletBinding()]
param(
    [int]$Port = 18080,
    [string]$TaskName = "CoilBot",
    [switch]$Quiet
)

$BotDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$logFile = Join-Path $BotDir "run\watchdog.log"

function Write-Log([string]$message) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $message"
    # 日志太大就只留最后 1MB，避免长期运行吃满磁盘
    if (Test-Path $logFile) {
        $size = (Get-Item $logFile).Length
        if ($size -gt 2MB) {
            $tail = Get-Content $logFile -Tail 4000
            [System.IO.File]::WriteAllText($logFile, ($tail -join "`r`n") + "`r`n", [System.Text.Encoding]::UTF8)
        }
    }
    [System.IO.File]::AppendAllText($logFile, $line + "`r`n", [System.Text.Encoding]::UTF8)
    if (-not $Quiet) { Write-Host $line }
}

function Test-Health {
    try {
        $result = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 8
        return [bool]$result.ok
    } catch {
        return $false
    }
}

if (Test-Health) {
    # 健康时也留痕（15 分钟一条），证明看门狗在跑，而不是悄悄死掉
    $needTick = $true
    if (Test-Path $logFile) {
        $lastOk = Select-String -Path $logFile -Pattern '\[ok\]' | Select-Object -Last 1
        if ($lastOk) {
            try {
                $stamp = [datetime]::ParseExact($lastOk.Line.Substring(0, 19), 'yyyy-MM-dd HH:mm:ss', $null)
                if (((Get-Date) - $stamp).TotalMinutes -lt 15) { $needTick = $false }
            } catch { }
        }
    }
    if ($needTick) { Write-Log "[ok] $Port 健康，服务在跑" }
    elseif (-not $Quiet) { Write-Host "健康：$Port 正常，无需处理" }
    exit 0
}

# 可能是正在启动，给三次机会（共约 18 秒）
for ($i = 1; $i -le 3; $i++) {
    Start-Sleep -Seconds 6
    if (Test-Health) {
        if (-not $Quiet) { Write-Host "健康：等待后恢复正常" }
        exit 0
    }
}

$state = "Unknown"
try { $state = (Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop).State } catch {
    Write-Log "[错误] 找不到计划任务 $TaskName，请先运行 install_service.ps1"
    exit 2
}

Write-Log "[异常] $Port 无响应，任务状态=$state → 重启 $TaskName"

# 进程还在但端口不响应 = 僵死。先把这些残留按命令行清掉，
# 否则新旧实例会抢同一个端口，恢复反而变慢。
$stale = @()
try {
    $stale = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction Stop |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'coilbot' } |
        Select-Object -ExpandProperty ProcessId
} catch { }
if ($stale.Count -gt 0) {
    Write-Log "[清理] 结束僵死的 coilbot 进程：$($stale -join ',')"
    foreach ($procId in $stale) {
        try { Stop-Process -Id $procId -Force -ErrorAction Stop } catch { }
    }
    Start-Sleep -Seconds 2
}

if ($state -eq "Running") {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
}

Start-ScheduledTask -TaskName $TaskName

# 等它真正开始 accept（最多 40 秒）
for ($i = 1; $i -le 8; $i++) {
    Start-Sleep -Seconds 5
    if (Test-Health) {
        Write-Log "[恢复] $TaskName 已重新拉起（约 $($i * 5) 秒），健康检查通过"
        exit 0
    }
}

Write-Log "[失败] 重启后仍不健康，请手动排查：run\bot.out、run\logs\coilbot.log"
exit 1
