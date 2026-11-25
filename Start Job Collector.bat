@echo off
rem Double-click to open Job Collector. The first time it installs what it needs (a few minutes).
setlocal
cd /d "%~dp0"

rem It keeps its own copy of Python's packages in .venv here, so the folder must be writable
rem A new, randomly named empty folder: mkdir never overwrites, rmdir only removes empty folders
set "PROBE=.jobcollector-write-check-%RANDOM%%RANDOM%"
mkdir "%PROBE%" 2>nul && rmdir "%PROBE%" || (
  echo Job Collector can't save files in this folder:
  echo   %CD%
  echo Move the whole Job Collector folder somewhere you own, like Documents, and double-click again.
  pause
  exit /b 1
)

rem Python 3.10 or newer: the py launcher first, then python on PATH
set "PY="
py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1 && set "PY=python"
if not defined PY (
  echo Job Collector needs Python 3.10 or newer. Opening the download page...
  echo When installing, tick "Add python.exe to PATH". Then double-click this file again.
  start "" "https://www.python.org/downloads/"
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Setting up for the first time...
  %PY% -m venv .venv || (echo Couldn't create the .venv folder. & pause & exit /b 1)
)

rem Install (again) only when requirements.txt changed
set "WANT="
for /f "skip=1 tokens=*" %%h in ('certutil -hashfile requirements.txt SHA256') do if not defined WANT set "WANT=%%h"
set "HAVE="
if exist ".venv\requirements.sha" set /p HAVE=<".venv\requirements.sha"
if not "%HAVE%"=="%WANT%" (
  echo Installing ^(the first time takes a few minutes^)...
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || (
    echo Install failed. Check your internet connection and double-click again.
    pause
    exit /b 1
  )
  >".venv\requirements.sha" echo %WANT%
)

echo.
echo Keep this window open while you use Job Collector. Close it to stop the page
echo ^(scheduled job checks keep running^).
echo.
".venv\Scripts\python.exe" -m collector ui
pause
