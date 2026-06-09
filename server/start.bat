@echo off
cd /d %~dp0
echo.
echo If phone cannot connect, run first:
echo   ..\scripts\allow-firewall-8000.bat
echo.
py -3.12 -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
