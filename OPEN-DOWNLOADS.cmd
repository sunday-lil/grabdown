@echo off
chcp 936 >nul
pushd "%~dp0"
if not exist "downloads" mkdir "downloads"
echo Opening: %~dp0downloads
start "" explorer "%~dp0downloads"
