@echo off
setlocal EnableExtensions
chcp 936 >nul
cd /d "%~dp0"
title GEO Tool

set PORT=8765
set HOST=127.0.0.1
set URL=http://%HOST%:%PORT%
set LOG=%~dp0data\start_error.log
if not exist "%~dp0data" mkdir "%~dp0data"

echo [%DATE% %TIME%] start > "%LOG%"
echo cwd=%CD% >> "%LOG%"

REM ---- resolve Python ----
set "PYEXE="
set "HERMES_PY=%LocalAppData%\hermes\hermes-agent\venv\Scripts\python.exe"
set "UV_PY=%AppData%\uv\python\cpython-3.11-windows-x86_64-none\python.exe"
if exist "%LocalAppData%\Programs\Python\Python314\python.exe" set "PYEXE=%LocalAppData%\Programs\Python\Python314\python.exe"
if not defined PYEXE if exist "%LocalAppData%\Programs\Python\Python313\python.exe" set "PYEXE=%LocalAppData%\Programs\Python\Python313\python.exe"
if not defined PYEXE if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set "PYEXE=%LocalAppData%\Programs\Python\Python312\python.exe"
if exist "%UV_PY%" set "PYEXE=%UV_PY%"
if exist "%HERMES_PY%" set "PYEXE=%HERMES_PY%"
if not defined PYEXE (
  where py >nul 2>&1
  if not errorlevel 1 set "PYEXE=py -3"
)
if not defined PYEXE (
  where python >nul 2>&1
  if not errorlevel 1 set "PYEXE=python"
)

if not defined PYEXE (
  echo [ERROR] Python not found >> "%LOG%"
  echo.
  echo Python not found. Install Python and check Add to PATH.
  echo Log: %LOG%
  pause
  exit /b 1
)
echo PYEXE=%PYEXE% >> "%LOG%"

REM ============================================================
REM 1. Hermes Proxy (port 8645)
REM ============================================================
set PROXY_PORT=8645
call :check_port %PROXY_PORT%
if not errorlevel 1 (
  echo Hermes Proxy already running on port %PROXY_PORT%
  echo hermes_proxy_already_running >> "%LOG%"
) else (
  echo Starting Hermes Proxy on port %PROXY_PORT%...
  where hermes >nul 2>&1
  if errorlevel 1 (
    echo [WARN] hermes command not found, skip proxy >> "%LOG%"
    echo Hermes Proxy skipped - hermes not in PATH
  ) else (
    start "Hermes Proxy" cmd /c "hermes proxy start --provider nous --host 127.0.0.1 --port %PROXY_PORT%"
    echo hermes_proxy_started >> "%LOG%"
  )
)
echo.

REM ============================================================
REM 2. KB Relay wiki-assistant (port 8080)
REM ============================================================
set RELAY_PORT=8080
call :check_port %RELAY_PORT%
if not errorlevel 1 (
  echo KB Relay already running on port %RELAY_PORT%
  echo kb_relay_already_running >> "%LOG%"
) else (
  echo Starting KB Relay on port %RELAY_PORT%...
  %PYEXE% "%~dp0resolve_kb_dir.py" check relay >nul 2>&1
  if not errorlevel 1 (
    start "KB Relay" cmd /c "%PYEXE% "%~dp0resolve_kb_dir.py" relay"
    echo kb_relay_started >> "%LOG%"
  ) else (
    echo [WARN] relay_server.py not found >> "%LOG%"
    echo KB Relay skipped - relay_server.py not found
  )
)
echo.

REM ============================================================
REM 3. Wiki API (port 8686)
REM ============================================================
set WIKI_PORT=8686
call :check_port %WIKI_PORT%
if not errorlevel 1 (
  echo Wiki API already running on port %WIKI_PORT%
  echo wiki_api_already_running >> "%LOG%"
) else (
  echo Starting Wiki API on port %WIKI_PORT%...
  %PYEXE% "%~dp0resolve_kb_dir.py" check wiki >nul 2>&1
  if not errorlevel 1 (
    start "Wiki API" cmd /c "%PYEXE% "%~dp0resolve_kb_dir.py" wiki"
    echo wiki_api_started >> "%LOG%"
  ) else (
    echo [WARN] api_server.py not found >> "%LOG%"
    echo Wiki API skipped
  )
)
echo.

REM ============================================================
REM 4. GEO Tool (port 8765)
REM ============================================================
call :check_port %PORT%
if not errorlevel 1 (
  echo GEO tool already running. Opening browser...
  echo already_running >> "%LOG%"
  start "" "%URL%"
  timeout /t 2 >nul
  exit /b 0
)

echo Starting GEO tool...
echo.
echo ============================================
echo   GEO Tool:       %URL%
echo   KB Relay:       http://127.0.0.1:8080
echo   Hermes Proxy:   http://127.0.0.1:8645
echo   Wiki API:       http://127.0.0.1:8686
echo ============================================
echo Close this window to stop GEO tool.
echo Log: %LOG%
echo.
echo Tip: If article generation fails on auth, run: hermes portal login
echo.
echo geo_starting >> "%LOG%"

start "" cmd /c "timeout /t 2 /nobreak >nul & start %URL%"

%PYEXE% -m uvicorn app.main:app --host %HOST% --port %PORT%
set ERR=%ERRORLEVEL%
echo exit_code=%ERR% >> "%LOG%"
if not "%ERR%"=="0" (
  echo.
  echo Start failed. Code %ERR%
  echo See log: %LOG%
  echo.
  echo Common fixes:
  echo  1. pip install -r requirements.txt
  echo  2. Port %PORT% already in use
  echo  3. Check .env exists
  pause
)
exit /b %ERR%

:check_port
REM call :check_port 8765  -> errorlevel 0 if listening
%PYEXE% -c "import socket,sys;p=int(sys.argv[1]);s=socket.socket();s.settimeout(0.6);e=s.connect_ex(('127.0.0.1',p));s.close();sys.exit(0 if e==0 else 1)" %~1
exit /b %ERRORLEVEL%
