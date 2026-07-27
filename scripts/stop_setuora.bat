@echo off
setlocal
cd /d "%~dp0.."
if /I "%~1"=="--no-pause" (
    shift
)

set "PROJECT_DIR=%CD%"
set "STOP_SCRIPT=%PROJECT_DIR%\deployment\windows\stop_setuora.ps1"

if not exist "%STOP_SCRIPT%" (
    echo The Setuora stop helper was not found:
    echo %STOP_SCRIPT%
    echo.
    echo Check that the deployment\windows folder is present, then try again.
    exit /b 1
)

echo Stopping Setuora QR Tally Bridge...
powershell -NoProfile -ExecutionPolicy Bypass -File "%STOP_SCRIPT%" -ProjectDir "%PROJECT_DIR%" %1 %2 %3 %4 %5 %6 %7 %8 %9
set "STOP_EXIT=%ERRORLEVEL%"
echo.

if "%STOP_EXIT%"=="0" (
    echo Setuora stop command completed.
) else (
    echo Setuora could not be stopped. If Setuora is installed as a Windows service,
    echo right-click scripts\stop_setuora.bat and choose Run as administrator.
)

exit /b %STOP_EXIT%
