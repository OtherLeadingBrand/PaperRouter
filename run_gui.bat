@echo off
REM PaperRouter - Web GUI Launcher
REM Double-click this file to open the web interface in your browser.
REM TIP: Use start.bat instead for automatic venv setup and update checking.
setlocal
cd /d "%~dp0"

REM Use virtual environment if available, otherwise fall back to system Python
if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
    set "PIP=.venv\Scripts\pip.exe"
) else (
    set "PY=python"
    set "PIP=python -m pip"
)

REM Check if Python is installed
%PY% --version >nul 2>&1
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
%PY% -c "import requests, flask, psutil" >nul 2>&1
if errorlevel 1 (
    echo Installing required dependencies...
    %PIP% install -r "%~dp0requirements.txt"
    echo.
)

REM Launch the web GUI (opens browser automatically)
%PY% "%~dp0web_gui.py"
pause
