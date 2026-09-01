@echo off
REM ============================================================================
REM  NaviLink — double-click this.
REM ============================================================================
REM  At the repo root on purpose: scripts\ is where build and dev helpers live,
REM  and "how do I open the app" should not require knowing that.
REM
REM  Uses pythonw.exe, which has no console, so the app opens as a window instead
REM  of a window plus a black box that must be left open. The first run falls back
REM  to python.exe because setup prints progress worth seeing.
REM
REM    NaviLink.bat                     window + chooser
REM    NaviLink.bat --ws 192.168.4.1    skip the chooser
REM    NaviLink.bat --serial COM5
REM    NaviLink.bat --browser           no window, use the default browser
REM ============================================================================
setlocal
cd /d "%~dp0"

set PY=.venv\Scripts\python.exe
set PYW=.venv\Scripts\pythonw.exe

if not exist "%PY%" (
  echo First run: setting up. This takes a minute.
  where py >nul 2>&1
  if %ERRORLEVEL%==0 ( py -3 -m venv .venv ) else ( python -m venv .venv )
  if not exist "%PY%" (
    echo.
    echo Could not create a virtual environment.
    echo Install Python 3.11+ from https://www.python.org/downloads/ and try again.
    echo.
    pause
    exit /b 1
  )
  "%PY%" -m pip install --quiet --upgrade pip
  "%PY%" -m pip install --quiet -r requirements.txt
  if errorlevel 1 (
    echo Dependency install failed.
    pause
    exit /b 1
  )
  echo Done.
)

REM The config tool is NOT in the repo: src\webui\ is gitignored because the public
REM NaviCore repo is the single source of truth for the UI. A fresh clone therefore
REM has no UI at all, and without this the first thing anyone sees is the app
REM explaining why there is nothing to configure with. Fetch it once, here.
REM
REM Never fatal -- offline is legitimate, and the control API and the /_link byte
REM pipe work without the bundle.
if not exist "src\webui\index.html" (
  echo Fetching the config tool -- it is not in the repo, see README.
  "%PY%" tools\fetch_webui.py
  if errorlevel 1 echo Could not fetch it. NaviLink will start and explain.
)

REM start "" detaches, so the launching console (if any) does not stay tied to it.
if exist "%PYW%" (
  start "" "%PYW%" src\app.py %*
) else (
  start "" "%PY%" src\app.py %*
)
endlocal
