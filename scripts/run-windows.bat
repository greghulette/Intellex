@echo off
REM Run the Intellex host on Windows.
REM
REM Creates the venv and installs dependencies on first run, so a fresh clone
REM works with no setup. Exists because "python src/host.py" uses whatever
REM interpreter is on PATH, which is NOT the venv the dependencies live in --
REM the resulting ModuleNotFoundError looks like a broken app rather than a
REM missing activation step.
REM
REM   run-windows.bat                    serve, attach nothing
REM   run-windows.bat --serial COM5      attach a port at startup
REM   run-windows.bat --ws 192.168.4.1   attach the droid's SoftAP

setlocal
cd /d "%~dp0\.."

set PY=.venv\Scripts\python.exe

if not exist "%PY%" (
  echo First run: creating the virtual environment...
  REM Prefer the py launcher; fall back to whatever python is on PATH.
  where py >nul 2>&1
  if %ERRORLEVEL%==0 ( py -3 -m venv .venv ) else ( python -m venv .venv )
  if not exist "%PY%" (
    echo.
    echo Could not create a virtual environment.
    echo Install Python 3.11+ from https://www.python.org/downloads/ and retry.
    exit /b 1
  )
  echo Installing dependencies...
  "%PY%" -m pip install --quiet --upgrade pip
  "%PY%" -m pip install --quiet -r requirements.txt
  if errorlevel 1 (
    echo Dependency install failed.
    exit /b 1
  )
)

REM Say it on every run, not only at setup: the venv outlives the decision that
REM made it, and one built months ago on an older Python is the case worth
REM surfacing. A note, not a refusal -- 3.9 runs everything today, and blocking a
REM working setup would be worse than the silence it replaces. No interpreter
REM picking is needed here: `py -3` already selects the newest installed, which is
REM the macOS launchers' actual problem, not this one's.
"%PY%" -c "import sys; raise SystemExit(sys.version_info < (3,11))" >nul 2>&1
if errorlevel 1 (
  echo.
  echo Note: this venv is older than Python 3.11. Intellex is developed on 3.14
  echo       and only 3.11+ is exercised. It works, but nothing tests it -- to
  echo       move up, install a newer Python, delete .venv, and run this again.
  echo.
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
  if errorlevel 1 echo Could not fetch it. Intellex will start and explain.
)

"%PY%" src\app.py %*
endlocal
