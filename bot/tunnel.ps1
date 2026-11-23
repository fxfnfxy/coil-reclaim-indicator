# 给 TradingView 一个 https 地址（TradingView 只往 80/443 的 https 发 webhook）。
#
#   .\tunnel.ps1                # 首次会自动下载 cloudflared，然后起临时隧道
#   .\tunnel.ps1 -Token <令牌>  # 用 Cloudflare 命名隧道（固定域名，推荐长期用）
#   .\tunnel.ps1 -Stop
#
# 临时隧道（quick tunnel）每次重启换一个域名，域名变了要回 TradingView 改 Webhook URL。
# 长期 24/7 建议：cloudflare 账号 + 一个域名 + 命名隧道，域名固定，一次填好不用再改。

[CmdletBinding()]
param(
    [int]$Port = 18080,
    [string]$Token = "",
    [string]$Hostname = "",
    [switch]$Stop,
    [switch]$InstallOnly
)

$ErrorActionPreference = "Stop"
$BotDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$binDir = Join-Path $BotDir "bin"
$runDir = Join-Path $BotDir "run"
New-Item -ItemType Directory -Force -Path $binDir, $runDir | Out-Null
$exe = Join-Path $binDir "cloudflared.exe"
$logFile = Join-Path $runDir "tunnel.log"
$urlFile = Join-Path $runDir "tunnel-url.txt"

if ($Stop) {
    Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force
    Write-Host "已停止 cloudflared" -ForegroundColor Yellow
    return
}

if (-not (Test-Path $exe)) {
    Write-Host "下载 cloudflared …" -ForegroundColor Cyan
    $url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
    try {
        Invoke-WebRequest -Uri $url -OutFile $exe -UseBasicParsing -TimeoutSec 180
    } catch {
        Write-Host "下载失败：$_" -ForegroundColor Red
        Write-Host "可以手动下载后放到：$exe" -ForegroundColor Yellow
        Write-Host "或者：winget install --id Cloudflare.cloudflared" -ForegroundColor Yellow
        return
    }
    Write-Host "已下载：$exe" -ForegroundColor Green
}
if ($InstallOnly) { Write-Host "cloudflared 就绪：$exe"; return }

$common = @("--no-autoupdate", "--logfile", $logFile, "--loglevel", "info")
if ($Token) {
    Write-Host "启动命名隧道（固定域名）…" -ForegroundColor Cyan
    $proc = Start-Process -FilePath $exe -ArgumentList (@("tunnel", "--no-autoupdate", "run", "--token", $Token)) `
        -PassThru -WindowStyle Hidden
    Start-Sleep -Seconds 6
    $target = if ($Hostname) { "https://$Hostname" } else { "（命名隧道域名）" }
} else {
    Write-Host "启动临时隧道 → http://127.0.0.1:$Port" -ForegroundColor Cyan
    $proc = Start-Process -FilePath $exe `
        -ArgumentList (@("tunnel") + $common + @("--url", "http://127.0.0.1:$Port")) `
        -PassThru -WindowStyle Hidden
    $target = ""
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 800
        if (Test-Path $logFile) {
            $match = Select-String -Path $logFile -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' -AllMatches |
                Select-Object -Last 1
            if ($match) {
                $target = $match.Matches[-1].Value
                break
            }
        }
    }
    if (-not $target) {
        Write-Host "还没拿到公网地址，稍等再看日志：$logFile" -ForegroundColor Yellow
        return
    }
}

$webhook = "$target/webhook?token=<你的 token>"
[System.IO.File]::WriteAllText($urlFile, $target, (New-Object System.Text.UTF8Encoding($false)))
Write-Host ""
Write-Host "公网地址：$target" -ForegroundColor Green
Write-Host "填进 TradingView 警报的 Webhook URL：" -ForegroundColor Green
Write-Host "  $webhook" -ForegroundColor Green
Write-Host ""
Write-Host "已写入：$urlFile（重启后会变，除非用命名隧道）"
Write-Host "验证：curl $target/health"
