@echo off
setlocal EnableExtensions

net session >nul 2>&1
if %errorlevel% equ 0 goto do_rule

echo Requesting administrator permission...
set "ELEV=%TEMP%\hongguo_elev.vbs"
(
  echo Set shell = CreateObject^("Shell.Application"^)
  echo shell.ShellExecute "%~f0", "", "", "runas", 1
) > "%ELEV%"
cscript //nologo "%ELEV%"
del "%ELEV%" 2>nul
exit /b

:do_rule
set "RULE=Hongguo Uvicorn 8000"
netsh advfirewall firewall show rule name="%RULE%" >nul 2>&1
if %errorlevel% equ 0 (
    echo Rule already exists.
) else (
    netsh advfirewall firewall add rule name="%RULE%" dir=in action=allow protocol=TCP localport=8000 profile=private,public,domain
    if errorlevel 1 (
        echo Failed to add firewall rule.
        pause
        exit /b 1
    )
    echo Allowed inbound TCP port 8000.
)

set "IP="
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /i "IPv4"') do (
    set "IP=%%a"
    goto found_ip
)
:found_ip
if defined IP set "IP=%IP: =%"
echo.
if defined IP (
    echo Phone browser URL: http://%IP%:8000
) else (
    echo Phone browser URL: http://YOUR_PC_IP:8000
    echo Run ipconfig to find IPv4 address.
)
echo Use same WiFi. Turn off mobile data on phone.
echo.
pause
endlocal
