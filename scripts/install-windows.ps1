# =============================================================================
#  install-windows.ps1 - put Intellex in the Start Menu
#
#  Copies dist\Intellex.exe to %LOCALAPPDATA%\Programs\Intellex and creates a
#  Start Menu shortcut, so it behaves like an installed program: searchable,
#  launchable from the Start Menu, and pinnable.
#
#  PER-USER, NOT Program Files. No admin rights, no UAC prompt, and nothing that
#  needs elevation to update later. Program Files would need both.
#
#  ABOUT PINNING TO THE TASKBAR: Windows 10/11 deliberately removed the ability
#  for a program to pin itself -- the verb is blocked, and every workaround that
#  still circulates either stopped working or trips Defender. So this creates the
#  Start Menu entry, and pinning is one manual step: find Intellex in the Start
#  Menu, right-click, "Pin to taskbar". Once.
# =============================================================================
$ErrorActionPreference = 'Stop'

$repo = Split-Path -Parent $PSScriptRoot
$exe  = Join-Path $repo 'dist\Intellex.exe'
if (-not (Test-Path $exe)) {
    Write-Error "dist\Intellex.exe not found. Run scripts\build-windows.bat first."
}

$dest = Join-Path $env:LOCALAPPDATA 'Programs\Intellex'
New-Item -ItemType Directory -Force -Path $dest | Out-Null

# Stop a running copy first: Windows locks a running executable, and the copy
# below would fail with a permission error that reads like a rights problem
# rather than "it is already open".
Get-Process -Name 'Intellex' -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Host "Closing the running Intellex (pid $($_.Id))..."
    $_.CloseMainWindow() | Out-Null
    Start-Sleep -Seconds 2
    if (-not $_.HasExited) { $_ | Stop-Process -Force }
}

Copy-Item $exe (Join-Path $dest 'Intellex.exe') -Force
Write-Host "Installed to $dest"

$startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$lnk       = Join-Path $startMenu 'Intellex.lnk'
$shell     = New-Object -ComObject WScript.Shell
$s         = $shell.CreateShortcut($lnk)
$s.TargetPath       = Join-Path $dest 'Intellex.exe'
$s.WorkingDirectory = $dest
$s.IconLocation     = Join-Path $dest 'Intellex.exe'
$s.Description      = 'Configure a NaviCore droid and its WCBs'
$s.Save()

Write-Host "Start Menu shortcut created."
Write-Host ""
Write-Host "To pin it: open the Start Menu, find Intellex, right-click -> Pin to taskbar."
Write-Host "(Windows blocks programs from pinning themselves, so this part is manual.)"
