@echo off
rem Home Network Monitor - double-click updater for Windows.
rem Downloads the latest release from GitHub, keeps a backup of the old
rem version in data\backup, and the services restart themselves.
cd /d "%~dp0"
python update.py
if errorlevel 1 echo.
echo (You can close this window.)
pause
