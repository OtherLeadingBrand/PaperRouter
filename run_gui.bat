@echo off
REM PaperRouter - Web GUI Launcher
REM Double-click this file to open the web interface in your browser.

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH.
    echo.
    echo Install Python from: https://www.python.org/downloads/
    echo IMPORTANT: Check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

REM Install dependencies if missing
python -c "import requests" >nul 2>&1
if errorlevel 1 (
    echo Installing required dependencies...
    python -m pip install requests
    echo.
)
python -c "import flask" >nul 2>&1
if errorlevel 1 (
    echo Installing Flask...
    python -m pip install flask
    echo.
)

REM Launch the web GUI (opens browser automatically)
python "%~dp0web_gui.py"
pause
