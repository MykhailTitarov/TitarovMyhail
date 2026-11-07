@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY set "PY=python"
%PY% -m pip install -q -r requirements.txt
%PY% run.py
pause
