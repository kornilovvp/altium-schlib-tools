@echo off
rem Two-panel (Norton Commander style) browser / editor for Altium .SchLib files.
rem Usage:  SchLibCommander.bat [left.SchLib] [right.SchLib]   (files can also be dropped onto this .bat)
setlocal
cd /d "%~dp0"
start "" pythonw "%~dp0src\schlib_commander.py" %*
if errorlevel 1 (
    echo Could not start Python. Run install.bat first.
    pause
)
endlocal
