@echo off
rem Installs the Python packages needed by the SchLib tools (olefile, pywin32, openpyxl, PyQt5).
rem Requires Python 3.10+ for Windows with pip (https://www.python.org/downloads/windows/).
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found in PATH. Install Python 3 from python.org and tick "Add python.exe to PATH".
    pause
    exit /b 1
)

echo Using:
python --version
echo.
python -m pip install --upgrade pip
python -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo.
    echo Installation FAILED - see the messages above.
    pause
    exit /b 1
)
echo.
echo Done. Start the programs with SchLibCommander.bat or SchLibTableEditor.bat
pause
endlocal
