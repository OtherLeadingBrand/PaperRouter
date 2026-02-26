@echo off
REM PaperRouter - CLI Launcher
REM Double-click to see help, or pass arguments for direct use.
REM TIP: Use start.bat instead for automatic venv setup and update checking.
setlocal
cd /d "%~dp0"

echo ================================================
echo   PaperRouter - CLI Mode
echo ================================================
echo.

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

REM Check if required libraries are installed
%PY% -c "import requests, flask, psutil" >nul 2>&1
if errorlevel 1 (
    echo Installing required dependencies...
    %PIP% install -r "%~dp0requirements.txt"
    echo.
)

REM If no arguments given, show help
if "%~1"=="" (
    echo No arguments given. Showing help:
    echo.
    %PY% "%~dp0downloader.py" --help
    echo.
    echo ================================================
    echo TIP: To download the Freeland Tribune, run:
    echo   %PY% "%~dp0downloader.py" --lccn sn87080287
    echo.
    echo To search for a newspaper:
    echo   %PY% "%~dp0downloader.py" --search "newspaper name"
    echo ================================================
) else (
    %PY% "%~dp0downloader.py" %*
)

echo.
pause
