@echo off
cd /d %~dp0server
if not exist main.py (
    echo ERROR: main.py not found. Run this from hongguo folder.
    pause
    exit /b 1
)
echo.
echo Starting server...
echo Phone URL: http://192.168.3.56:8000  (check ipconfig if different)
echo.
py -3.12 -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
