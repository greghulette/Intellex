@echo off
REM ============================================================================
REM  build-windows.bat - build dist\Intellex.exe
REM
REM  Mirrors ESP-Flasher-Companion's build script, which is the proven pipeline
REM  for this stack. Run it from anywhere; it works from the repo root.
REM
REM  FETCHES THE TOOLS FIRST, deliberately. src\webui\ and src\webui_wcb\ are
REM  gitignored, so a clean clone has neither -- and PyInstaller would happily
REM  build an app with no UI in it, which only shows up when someone launches it.
REM ============================================================================
setlocal
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo Creating .venv ...
    python -m venv .venv || goto :fail
    .venv\Scripts\python.exe -m pip install -q --upgrade pip || goto :fail
    .venv\Scripts\python.exe -m pip install -q -r requirements.txt || goto :fail
)

echo.
echo === Fetching the bundled tools ===
REM Not fatal: offline you can still build with whatever is already bundled.
.venv\Scripts\python.exe tools\fetch_webui.py --tool all
if errorlevel 1 echo   (fetch failed - building with the tools already present)

if not exist "src\webui\index.html" (
    echo.
    echo WARNING: src\webui\ is empty. The app will start with no config tool
    echo          until its Update button is used. Run this again with a network
    echo          connection to bundle it.
    echo.
)

echo.
echo === Caching firmware (so a fresh install can flash offline) ===
REM Not fatal either: the app still works, it just has to download at flash time.
.venv\Scripts\python.exe tools\fetch_firmware.py
if errorlevel 1 echo   (firmware fetch incomplete - see above)

REM Windows LOCKS a running executable, so PyInstaller's copy into dist\ fails with
REM a bare "PermissionError: Access is denied" that reads like a rights problem
REM rather than "your app is open". Say which it is.
REM Is the app running? Windows LOCKS a running executable, so PyInstaller's copy
REM into dist\ dies with a bare "PermissionError: Access is denied" that reads
REM like a rights problem rather than "your app is open".
REM
REM Checked in PYTHON, not with `tasklist | find`. Batch string-matching depends
REM on PATH: run from a Git Bash shell, cmd inherits /usr/bin ahead of System32,
REM "find" resolves to Git's UNIX find, the pipe fails and the guard silently
REM PASSES -- which is exactly how a build then died with the app open. Full
REM System32 paths did not fix it either (%SystemRoot% did not expand usefully
REM there). We already depend on this interpreter two lines below, so use it.
.venv\Scripts\python.exe -c "import subprocess,sys; o=subprocess.run(['tasklist','/FI','IMAGENAME eq Intellex.exe'],capture_output=True,text=True).stdout; sys.exit(1 if 'Intellex.exe' in o else 0)"
if errorlevel 1 (
    echo.
    echo Intellex is currently RUNNING, and Windows locks a running .exe.
    echo Close it and run this again.
    exit /b 1
)

echo === Stamping the build ===
for /f %%i in ('git rev-parse --short HEAD 2^>nul') do set NLSHA=%%i
if "%NLSHA%"=="" set NLSHA=unknown
.venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'src'); import pathlib, version; version.write_stamp(pathlib.Path('src/build_stamp.py'), '%NLSHA%')"

echo.
echo === Building ===
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean Intellex.spec || goto :fail

echo.
echo Built: dist\Intellex.exe
echo Install it (Start Menu shortcut) with:
echo    powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1
exit /b 0

:fail
echo.
echo BUILD FAILED
exit /b 1
