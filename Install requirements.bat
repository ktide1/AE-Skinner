@echo off
cd /d "%~dp0"
echo Installing AE Skinner's Python dependencies...
python -m pip install --upgrade pip
python -m pip install pefile Pillow tkinterdnd2
echo.
python "%~dp0aeskin_cli.py" doctor
pause
