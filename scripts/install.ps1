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

Write-Host 'Creating shortcuts...'
& $venvPy -m murmur --install-shortcuts
if ($LASTEXITCODE -ne 0) { throw 'shortcut creation failed' }

# If an older Murmur is running, stop it and start the new version in the background.
$pyw = Join-Path $repo '.venv\Scripts\pythonw.exe'
$old = @(Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*-m murmur*' -and $_.CommandLine -notlike '*-shortcuts*' -and $_.ProcessId -ne $PID })
if ($old.Count -gt 0) {
    Write-Host 'Restarting the running Murmur...'
    $old | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Start-Sleep -Seconds 1
}
Start-Process -FilePath $pyw -ArgumentList '-m murmur --background' -WorkingDirectory $repo
Write-Host 'Murmur is running (tray icon; the model loads in a few seconds).'
Write-Host ''
Write-Host 'Pin it: open Start, find Murmur, right-click -> Pin to taskbar.'
