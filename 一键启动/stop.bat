@echo off
chcp 65001 >nul
title OpenClaw - 停止服务
echo ===== 停止 OpenClaw 服务 =====
echo.

REM 停止控制台 (端口 8000)
set "CONSOLE_STOPPED="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000 " ^| findstr "LISTENING"') do (
    taskkill /F /PID %%p >nul 2>&1
    if not errorlevel 1 set "CONSOLE_STOPPED=1"
)
if defined CONSOLE_STOPPED (echo [ok] 控制台已停止) else (echo [--] 控制台未运行)

REM 停止网关 (端口 18789 + gateway.cmd + node)
set "GATEWAY_STOPPED="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":18789 " ^| findstr "LISTENING"') do (
    taskkill /F /PID %%p >nul 2>&1
    if not errorlevel 1 set "GATEWAY_STOPPED=1"
)
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'cmd.exe' -and $_.CommandLine -match 'gateway\.cmd') -or ($_.Name -eq 'node.exe' -and $_.CommandLine -match 'gateway') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
if defined GATEWAY_STOPPED (echo [ok] 网关已停止) else (echo [--] 网关未运行)

echo.
echo 完成。启动请双击 start.bat
echo.
pause
