@echo off
REM Run the NaviLink host on Windows.
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

"%PY%" src\app.py %*
endlocal
