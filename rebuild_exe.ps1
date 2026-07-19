# Bygg Filmrulle.exe (en fristående fil, inget konsolfönster).
# Kör:  powershell -ExecutionPolicy Bypass -File rebuild_exe.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

pyinstaller --noconfirm --onefile --windowed `
    --name Filmrulle `
    --icon filmrulle.ico `
    --collect-all rawpy `
    --collect-all pillow_heif `
    filmrulle.py

Write-Host ""
Write-Host "Klart -> dist\Filmrulle.exe" -ForegroundColor Green
