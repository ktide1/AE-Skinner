@echo off
title AE Skinner
cd /d "%~dp0"

net session >nul 2>&1
if errorlevel 1 (
    echo Some After Effects installs live under C:\Program Files and need
    echo administrator rights. Re-launching elevated...
    powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~f0'"
    exit /b
)

where pythonw >nul 2>&1
if not errorlevel 1 (
    start "" pythonw "%~dp0AESkinner.pyw"
    exit /b
)
where python >nul 2>&1
if errorlevel 1 (
    echo Python 3.9+ is required. Install it from https://python.org and tick
    echo "Add python.exe to PATH", then run this file again.
    pause
    exit /b 1
)
python "%~dp0AESkinner.pyw"
