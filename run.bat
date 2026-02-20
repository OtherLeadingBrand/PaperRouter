@echo off
REM LOC Newspaper Downloader - Easy Windows Launcher
REM Double-click to start downloading, or drag to a command prompt for options.

echo ================================================
echo   LOC Newspaper Downloader
echo   Library of Congress - Chronicling America
echo ================================================
echo.

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

REM Check if required libraries are installed
python -c "import requests, flask, psutil" >nul 2>&1
if errorlevel 1 (
    echo Installing required dependencies...
    python -m pip install -r "%~dp0requirements.txt"
    echo.
)

REM If no arguments given, show help
if "%~1"=="" (
    echo No arguments given. Showing help:
    echo.
    python "%~dp0downloader.py" --help
    echo.
    echo ================================================
    echo TIP: To download the Freeland Tribune, run:
    echo   python "%~dp0downloader.py" --lccn sn87080287
    echo.
    echo To search for a newspaper:
    echo   python "%~dp0downloader.py" --search "newspaper name"
    echo ================================================
) else (
    python "%~dp0downloader.py" %*
)

echo.
pause
