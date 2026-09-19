@echo off
rem OpenClaw Console (uvicorn) - hidden launcher
cd /d C:\Users\33386\Desktop\aichat_wnagye
C:\Users\33386\Desktop\aichat_wnagye\.venv\Scripts\python.exe -u -m uvicorn app.main:app --host 127.0.0.1 --port 8000 >> _uvicorn.log 2>&1
