@echo off
setlocal
set "SETUORA_SCRIPT_DIR=%~dp0"
cd /d "%SETUORA_SCRIPT_DIR%.."
if /I "%~1"=="--no-pause" (
    shift
)

set "PROJECT_DIR=%CD%"
set "START_SCRIPT=%PROJECT_DIR%\deployment\windows\start_setuora.ps1"
set "HOST_ADDRESS=127.0.0.1"
set "PORT=8000"
set "CONSOLE_ONLY=0"

:parse_args
if "%~1"=="" goto args_done
if /I "%~1"=="-Port" (
    set "PORT=%~2"
    shift
    shift
    goto parse_args
)
if /I "%~1"=="--port" (
    set "PORT=%~2"
    shift
    shift
    goto parse_args
)
if /I "%~1"=="/Port" (
    set "PORT=%~2"
    shift
    shift
    goto parse_args
)
if /I "%~1"=="-HostAddress" (
    set "HOST_ADDRESS=%~2"
    shift
    shift
    goto parse_args
)
if /I "%~1"=="--host" (
    set "HOST_ADDRESS=%~2"
    shift
    shift
    goto parse_args
)
if /I "%~1"=="--console-only" (
    set "CONSOLE_ONLY=1"
    shift
    goto parse_args
)
shift
goto parse_args

:args_done
if not exist "%START_SCRIPT%" (
    echo The Setuora start helper was not found:
    echo %START_SCRIPT%
    echo.
    echo Check that the deployment\windows folder is present, then try again.
    exit /b 1
)

if not exist "%PROJECT_DIR%\.venv\Scripts\python.exe" (
    echo Setuora is not set up yet. Run scripts\setup.bat first.
    echo.
    exit /b 1
)

set "CONSOLE_ONLY_ARG="
if "%CONSOLE_ONLY%"=="1" set "CONSOLE_ONLY_ARG=-ConsoleOnly"
powershell -NoProfile -ExecutionPolicy Bypass -File "%START_SCRIPT%" -ProjectDir "%PROJECT_DIR%" -HostAddress "%HOST_ADDRESS%" -Port "%PORT%" %CONSOLE_ONLY_ARG%
set "START_EXIT=%ERRORLEVEL%"
echo.
if not "%START_EXIT%"=="0" echo Setuora stopped with error code %START_EXIT%.
exit /b %START_EXIT%
