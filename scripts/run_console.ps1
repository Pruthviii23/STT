# Debug run with a visible console (logs also go to %APPDATA%\Murmur\murmur.log)
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
& (Join-Path $repo '.venv\Scripts\python.exe') -m murmur
