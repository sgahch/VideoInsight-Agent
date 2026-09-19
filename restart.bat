@echo off
REM ============================================================
REM  VideoInsight Agent - restart.bat
REM  Double-click to restart the local service:
REM    1) kill whatever is listening on port 8000
REM    2) start uvicorn with the project's own venv python
REM  This file MUST stay in the project root folder.
REM ============================================================

setlocal EnableDelayedExpansion

set "PROJ=%~dp0"
set "PORT=8000"
set "PY=%PROJ%.venv\Scripts\python.exe"

cd /d "%PROJ%"

echo ============================================================
echo   VideoInsight Agent - Restart Service
echo ============================================================
echo   Project : %PROJ%
echo   Python  : %PY%
echo   Port    : %PORT%
echo.

if not exist "%PY%" (
    echo [ERROR] venv python not found:
    echo   %PY%
    echo Keep this .bat in the project root folder.
    echo.
    pause
    exit /b 1
)

echo [1/3] Stopping old service on port %PORT% ...
set "KILLED=0"
REM NOTE: the service runs as a parent process plus a forked worker, and the
REM listening socket is held by the WORKER. A plain kill leaves that worker
REM orphaned and still holding the port (uvicorn then fails with error 10048).
REM So: "/T" kills the whole process tree, and we keep re-checking until the
REM port is really free.
for /L %%i in (1,1,6) do (
    set "FOUND=0"
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr LISTENING ^| findstr /C:":%PORT% "') do (
        echo     - killing PID %%a and its child processes
        taskkill /PID %%a /F /T >nul 2>&1
        set "FOUND=1"
        set "KILLED=1"
    )
    if "!FOUND!"=="0" goto :port_free
    timeout /t 1 /nobreak >nul
)
:port_free
if "!KILLED!"=="0" (
    echo     - nothing was running on port %PORT%
) else (
    echo     - port %PORT% is free now
)

echo [2/3] Waiting for the port to settle ...
timeout /t 1 /nobreak >nul

echo [3/3] Starting uvicorn ...
echo     URL: http://127.0.0.1:%PORT%/
echo     Press Ctrl+C in this window to stop the service.
echo ============================================================
echo.

"%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port %PORT% --log-level info

echo.
echo Service has stopped.
pause
