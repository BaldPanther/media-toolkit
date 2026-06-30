# Creates "MKV default tracks" shortcuts (Desktop + project folder) that launch the
# tool via pythonw.exe (an EXE, not blocked like .bat) with a default start folder.
# Run:  powershell -NoProfile -File make_shortcut.ps1
$ErrorActionPreference = 'Stop'

$pythonw   = 'D:\Programs\Python\Python314\pythonw.exe'
$appPy     = 'D:\Projects\Apps\mkv-default-tracks\app.py'
$workDir   = 'D:\Projects\Apps\mkv-default-tracks'
$defFolder = 'Y:\tv-ssd-1'                     # папка по умолчанию (стартовая точка)

if (-not (Test-Path $pythonw)) { Write-Host "pythonw not found: $pythonw"; exit 1 }
if (-not (Test-Path $appPy))   { Write-Host "app.py not found: $appPy"; exit 1 }

$argLine = '"' + $appPy + '" "' + $defFolder + '"'

function New-Lnk([string]$path) {
    $ws  = New-Object -ComObject WScript.Shell
    $lnk = $ws.CreateShortcut($path)
    $lnk.TargetPath       = $pythonw
    $lnk.Arguments        = $argLine
    $lnk.WorkingDirectory = $workDir
    $lnk.IconLocation     = "$pythonw,0"
    $lnk.Description       = 'MKV: set default audio/subtitle tracks'
    $lnk.Save()
    Write-Host "Shortcut created: $path"
}

$desktop = [Environment]::GetFolderPath('Desktop')
New-Lnk (Join-Path $desktop 'MKV default tracks.lnk')
New-Lnk (Join-Path $workDir 'MKV default tracks.lnk')
