@echo off
REM LOC Newspaper Downloader - GUI Launcher
REM Double-click this file to open the graphical interface.

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

REM Launch the GUI
where pythonw >nul 2>&1
if errorlevel 1 (
    python "%~dp0gui.py"
) else (
    start "" pythonw "%~dp0gui.py"
)
