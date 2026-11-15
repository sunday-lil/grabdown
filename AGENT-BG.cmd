@echo off
chcp 936 >nul
pushd "%~dp0"
echo Starting grabdown agent in background, log -^> agent.log
start "" pythonw -u -m grabdown agent >> "%~dp0agent.log" 2>&1
timeout /t 2 >nul
python -m grabdown doctor
echo.
echo To stop it: run STOP.cmd
pause
