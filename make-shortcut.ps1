# Creates the AEGIS desktop icon (and a Start-menu entry).
#
# The shortcut points at pythonw.exe, not python.exe, so launching it does not
# leave a black console window sitting behind the app. Because that also means
# a crash would be silent, AEGIS writes its own startup log to
#   %LOCALAPPDATA%\Aegis\logs\startup.log

$ErrorActionPreference = "Stop"

$app = Split-Path -Parent $MyInvocation.MyCommand.Path
$pyw = Join-Path $app ".venv\Scripts\pythonw.exe"
$py  = Join-Path $app ".venv\Scripts\python.exe"
$ico = Join-Path $app "aegis.ico"

if (Test-Path $pyw) { $target = $pyw }
elseif (Test-Path $py) { $target = $py }
else { Write-Output "FAIL: no python in .venv - run the setup script first"; exit 1 }

function New-AegisShortcut([string]$linkPath) {
    $shell = New-Object -ComObject WScript.Shell
    $sc = $shell.CreateShortcut($linkPath)
    $sc.TargetPath = $target
    $sc.Arguments = "-m aegis"
    $sc.WorkingDirectory = $app
    $sc.Description = "AEGIS - local AI harness"
    if (Test-Path $ico) { $sc.IconLocation = "$ico,0" }
    $sc.Save()
    Write-Output "Created: $linkPath"
}

# Desktop. OneDrive may have redirected it, so ask Windows rather than guessing.
$desktop = [Environment]::GetFolderPath("Desktop")
if (-not (Test-Path $desktop)) { $desktop = Join-Path $env:USERPROFILE "Desktop" }
New-AegisShortcut (Join-Path $desktop "AEGIS.lnk")

# Start menu, so typing "aegis" into Start finds it too.
$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
if (Test-Path $startMenu) {
    New-AegisShortcut (Join-Path $startMenu "AEGIS.lnk")
}

Write-Output "Target: $target -m aegis"
Write-Output "Working folder: $app"
