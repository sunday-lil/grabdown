@echo off
pushd "%~dp0"
python -m grabdown %*
popd
