@echo off
chcp 936 >nul
pushd "%~dp0"
echo ================================================
echo   grabdown download agent
echo.
echo   Keep THIS window open. The browser extension
echo   talks to  http://127.0.0.1:8899
echo ================================================
echo.
python -u -m grabdown agent
set RC=%ERRORLEVEL%
echo.
if "%RC%"=="3" (
  echo [i] An agent is ALREADY running. No need to start another one.
) else (
  echo [!] Agent exited with code %RC%.
  echo     If there was red text above, send it to me.
)
pause
