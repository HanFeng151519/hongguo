@echo off
setlocal EnableExtensions
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

for /f "delims=" %%P in ('where py 2^>nul') do set "PY=%%P"
if not defined PY set "PY=py"
for /f "delims=" %%E in ('"%PY%" -3.12 -c "import sys; print(sys.executable)" 2^>nul') do set "PYEXE=%%E"
if not defined PYEXE (
    echo Cannot find Python 3.12
    pause
    exit /b 1
)

set "RULE=Hongguo Python Inbound"
netsh advfirewall firewall delete rule name="%RULE%" >nul 2>&1
netsh advfirewall firewall add rule name="%RULE%" dir=in action=allow program="%PYEXE%" enable=yes profile=private,public,domain
echo Allowed inbound for: %PYEXE%
echo Also ensure ports 8000 and 8080 rules exist.
pause
endlocal
