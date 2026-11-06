@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY (
  for /f "tokens=1,2" %%a in ('python --version 2^>nul') do if "%%a %%b" neq "Python " if "%%a"=="Python" set "PY=python"
)
if not defined PY (
  echo Python не найден. Установите его с https://www.python.org/downloads/
  echo и при установке отметьте галочку "Add python.exe to PATH".
  pause
  exit /b 1
)
echo Использую: %PY%
%PY% -m pip install -q -r requirements.txt
%PY% run.py
pause
