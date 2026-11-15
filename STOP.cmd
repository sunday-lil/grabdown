@echo off
chcp 936 >nul
pushd "%~dp0"
python -m grabdown agent --stop
timeout /t 2 >nul
pause
