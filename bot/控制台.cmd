@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set PYTHONPATH=%~dp0

if not exist config.toml (
  copy config.example.toml config.toml >nul
  echo [初始化] 已生成 config.toml，请先填 [server] token 与 [notify]
)

rem 带参数可跳过菜单：控制台.cmd 1 / 2 / 3 ...
if not "%~1"=="" (
  set choice=%~1
  goto run
)

:menu
cls
echo ============================================================
echo    Coil Reclaim 24/7 盯盘机器人
echo    目录: %~dp0
echo ============================================================
echo    1. 自检（配置 / 端口 / 时区 / 通知 / 精度）
echo    2. 启动盯盘（前台运行，能看日志，Ctrl+C 停）
echo    3. 查看运行状态（统计 + 纸面权益 / 胜率）
echo    4. 发送测试通知
echo    5. 判断示例数据包（不联网，看规则逐条打勾）
echo    6. 启动公网隧道（给 TradingView 填 Webhook）
echo    7. 计划任务：启动
echo    8. 计划任务：停止
echo    9. 计划任务：重新注册（改过配置后用）
echo    0. 退出
echo ============================================================
set "choice="
set /p choice=请输入编号回车:
:run
if "%choice%"=="1" goto doctor
if "%choice%"=="2" goto serve
if "%choice%"=="3" goto status
if "%choice%"=="4" goto notify
if "%choice%"=="5" goto demo
if "%choice%"=="6" goto tunnel
if "%choice%"=="7" goto task_start
if "%choice%"=="8" goto task_stop
if "%choice%"=="9" goto task_install
if "%choice%"=="0" exit /b 0
goto menu

:doctor
python -m coilbot doctor
goto done

:serve
echo 提示：这个窗口要保持打开；关掉就等于停掉。长期运行请用 9 注册计划任务。
echo.
python -m coilbot --config config.toml serve
goto done

:status
python -m coilbot status
echo.
echo --- 健康检查 ---
curl -s http://127.0.0.1:18080/health
echo.
goto done

:notify
python -m coilbot notify-test
goto done

:demo
python -m coilbot demo
goto done

:tunnel
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tunnel.ps1" -Port 18080
goto done

:task_start
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_service.ps1" -Start
goto done

:task_stop
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_service.ps1" -Stop
goto done

:task_install
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_service.ps1"
goto done

:done
echo.
pause
if "%~1"=="" goto menu
exit /b 0
