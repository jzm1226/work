@echo off
setlocal
cd /d "%~dp0"

set "PY_CMD=py -3"
where py >nul 2>nul
if errorlevel 1 set "PY_CMD=python"

%PY_CMD% --version >nul 2>nul
if errorlevel 1 (
    echo Python 3 was not found. Install Python 3.10 or newer first.
    exit /b 1
)

if not exist .venv\Scripts\python.exe %PY_CMD% -m venv .venv
if errorlevel 1 exit /b 1
call .venv\Scripts\activate.bat
python -m pip install -r requirements.txt
if errorlevel 1 exit /b 1
python yed_ipd_tool.py
endlocal
