# Murmur uninstaller: removes the Startup shortcut and stops running instances.
$ErrorActionPreference = 'Continue'
$lnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'Murmur.lnk'
if (Test-Path $lnk) { Remove-Item $lnk -Force; Write-Host 'Removed startup shortcut.' }

Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*-m murmur*' -and $_.ProcessId -ne $PID } |
    ForEach-Object {
        Write-Host "Stopping PID $($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force
    }
Write-Host 'Done. Notes and config (%APPDATA%\Murmur) were left in place.'
