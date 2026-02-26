@echo off
REM ============================================================================
REM   PaperRouter - One-Click Launcher
REM   Creates a virtual environment, installs dependencies, checks for updates,
REM   and launches the web GUI in your browser.
REM ============================================================================
setlocal
cd /d "%~dp0"

echo.
echo   PaperRouter - Starting...
echo.

REM ---------- Check Python ----------
python --version >nul 2>&1
if errorlevel 1 (
    echo   ERROR: Python is not installed or not in PATH.
    echo.
    echo   Install Python from: https://www.python.org/downloads/
    echo   IMPORTANT: Check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

REM ---------- Create virtual environment if needed ----------
if not exist ".venv\Scripts\python.exe" (
    echo   Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo   ERROR: Failed to create virtual environment.
        echo   Try running: python -m pip install --upgrade pip
        echo.
        pause
        exit /b 1
    )
    echo   Virtual environment created.
    echo.
)

REM ---------- Install / update dependencies ----------
.venv\Scripts\python.exe -c "import requests, flask, psutil" >nul 2>&1
if errorlevel 1 (
    echo   Installing dependencies...
    .venv\Scripts\pip.exe install -r requirements.txt --quiet --disable-pip-version-check
    if errorlevel 1 (
        echo   ERROR: Failed to install dependencies.
        echo   Try deleting the .venv folder and running this script again.
        echo.
        pause
        exit /b 1
    )
    echo   Dependencies installed.
    echo.
)

REM ---------- Check for updates (quick, non-blocking) ----------
.venv\Scripts\python.exe updater.py --check-only 2>nul

REM ---------- Launch the web GUI ----------
echo.
echo   Launching PaperRouter Web GUI...
echo   (The browser will open automatically. Close this window to stop the server.)
echo.
.venv\Scripts\python.exe web_gui.py
pause
