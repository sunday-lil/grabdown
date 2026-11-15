@echo off
chcp 936 >nul
pushd "%~dp0"
set "EXT=%~dp0ext"
if not exist "%EXT%\manifest.json" (
  echo [x] ext\manifest.json not found.
  echo     Put this .cmd next to the ext folder.
  pause
  exit /b 1
)
echo Opening the extensions page and the folder to load:
echo   %EXT%
echo.
echo Steps:
echo   1. Turn ON "Developer mode"
echo   2. Click "Load unpacked"
echo   3. Pick the folder above
echo.
start "" msedge "edge://extensions" 2>nul || start "" chrome "chrome://extensions" 2>nul
timeout /t 1 >nul
start "" "%EXT%"
pause
