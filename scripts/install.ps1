# Murmur installer (idempotent). Run:  powershell -ExecutionPolicy Bypass -File scripts\install.ps1
# 'Continue' (not 'Stop'): in PS 5.1, native tools writing warnings to stderr would otherwise abort.
# Failures are detected via $LASTEXITCODE checks below.
$ErrorActionPreference = 'Continue'
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$venvPy = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    Write-Host 'Creating virtual environment (.venv, Python 3.11)...'
    & py -3.11 -m venv (Join-Path $repo '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create venv. Install Python 3.11 first.' }
}

Write-Host 'Installing requirements...'
& $venvPy -m pip install --disable-pip-version-check -q -r (Join-Path $repo 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }

$ico = Join-Path $repo 'assets\murmur.ico'
New-Item -ItemType Directory -Force (Split-Path $ico) | Out-Null
& $venvPy -c "from murmur.icon import save_ico; save_ico(r'$ico')"
if ($LASTEXITCODE -ne 0) { throw 'icon generation failed' }

$startup = [Environment]::GetFolderPath('Startup')
$lnkPath = Join-Path $startup 'Murmur.lnk'
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($lnkPath)
$lnk.TargetPath = Join-Path $repo '.venv\Scripts\pythonw.exe'
$lnk.Arguments = '-m murmur'
$lnk.WorkingDirectory = $repo
$lnk.IconLocation = $ico
$lnk.Description = 'Murmur - hold-to-talk voice notes'
$lnk.WindowStyle = 7
$lnk.Save()
Write-Host "Startup shortcut: $lnkPath"

# Launch now unless already running (the app also enforces a single instance).
$running = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like '*-m murmur*' }
if (-not $running) {
    Start-Process -FilePath $lnk.TargetPath -ArgumentList '-m murmur' -WorkingDirectory $repo
    Write-Host 'Murmur started. Look for the mic icon in the tray (model loads in a few seconds).'
} else {
    Write-Host 'Murmur is already running.'
}
