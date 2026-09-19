@echo off
chcp 65001 >nul
title OpenClaw 微信 Bot 控制台 - 一键启动
echo ============================================
echo    OpenClaw 微信 Bot 控制台 - 一键启动
echo ============================================
echo.

REM ===== 1. 启动网关（端口 18789，微信消息收发）=====
netstat -ano | findstr ":18789 " | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
    echo [1/3] 网关已在运行
) else (
    echo [1/3] 正在启动网关（后台隐藏）...
    start "" wscript.exe "C:\Users\33386\.openclaw\gateway.vbs"
)

REM ===== 2. 启动控制台（端口 8000，Web 管理面板）=====
netstat -ano | findstr ":8000 " | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
    echo [2/3] 控制台已在运行
) else (
    echo [2/3] 正在启动控制台（后台隐藏）...
    start "" wscript.exe "C:\Users\33386\Desktop\aichat_wnagye\_console_start.vbs"
)

REM ===== 3. 等待就绪并打开浏览器 =====
echo [3/3] 等待服务就绪，约 10 秒...
timeout /t 10 /nobreak >nul
start "" "http://127.0.0.1:8000/"

echo.
echo ============================================
echo    启动完成！
echo    控制台: http://127.0.0.1:8000/
echo    网关:   http://127.0.0.1:18789
echo    日志:   项目根目录 _uvicorn.log
echo ============================================
echo.
pause
