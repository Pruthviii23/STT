# Murmur uninstaller: removes the Startup and Start Menu shortcuts and stops running instances.
$ErrorActionPreference = 'Continue'
$repo = Split-Path -Parent $PSScriptRoot
$venvPy = Join-Path $repo '.venv\Scripts\python.exe'
if (Test-Path $venvPy) {
    & $venvPy -m murmur --uninstall-shortcuts
} else {
    foreach ($d in @([Environment]::GetFolderPath('Startup'), [Environment]::GetFolderPath('Programs'))) {
        $l = Join-Path $d 'Murmur.lnk'
        if (Test-Path $l) { Remove-Item $l -Force; Write-Host "Removed $l" }
    }
}

Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*-m murmur*' -and $_.ProcessId -ne $PID } |
    ForEach-Object {
        Write-Host "Stopping PID $($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force
    }
Write-Host 'Done. Notes and config (%APPDATA%\Murmur) were left in place. Unpin Murmur from the taskbar if you pinned it.'
