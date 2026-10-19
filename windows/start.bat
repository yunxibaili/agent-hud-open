@echo off
rem ---------------------------------------------------------------------------
rem  Agent HUD (Windows, PySide6) launcher.
rem
rem    double-click         -> start silently in the background (under watchdog)
rem    start.bat console    -> start with a console window (see errors/logs)
rem    start.bat once       -> query once, print the result, exit
rem    start.bat pip        -> install / upgrade PySide6
rem    start.bat stop       -> stop the HUD and its watchdog
rem
rem  The interpreter must have PySide6; each candidate is probed before use.
rem ---------------------------------------------------------------------------
setlocal enabledelayedexpansion
set "HERE=%~dp0"
set "MODE=%~1"
if /i "%MODE%"=="" set "MODE=silent"

set "PY="
set "PYW="

if /i "%MODE%"=="pip" (
  call :pick
  if not defined PY (
    echo Agent HUD: no Python found. Install Python 3.10+ first.
    pause
    exit /b 1
  )
  "%PY%" -m pip install --upgrade PySide6
  pause
  exit /b 0
)

if /i "%MODE%"=="stop" (
  echo Agent HUD: stopping...
  powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*agent-hud-open\windows\*' -and $_.Name -like 'python*' } | ForEach-Object { Write-Host ('  kill ' + $_.ProcessId + ' ' + $_.Name); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
  del /q "%HERE%.hud-quit" >nul 2>&1
  echo Agent HUD: stopped.
  exit /b 0
)

call :pick
if not defined PY (
  echo.
  echo Agent HUD needs Python 3.10+ with PySide6.
  echo   1^) install Python from https://www.python.org/downloads/
  echo   2^) run:  start.bat pip
  echo.
  pause
  exit /b 1
)

if /i "%MODE%"=="console" (
  "%PY%" "%HERE%hud.py" %2 %3 %4
  exit /b %ERRORLEVEL%
)

if /i "%MODE%"=="once" (
  "%PY%" "%HERE%check.py" %2 %3 %4 %5 %6
  exit /b %ERRORLEVEL%
)

rem --- silent background start, supervised by the watchdog ----------------------
if defined PYW (
  start "" "%PYW%" "%HERE%watchdog.py"
) else (
  start "" /min "%PY%" "%HERE%watchdog.py"
)
exit /b 0

rem ---------------------------------------------------------------------------
:pick
rem 1) explicit override
if defined AGENT_HUD_PYTHON call :try "%AGENT_HUD_PYTHON%"
rem 2) every Python 3.x installed per-user
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do call :try "%%~D\python.exe"
rem 3) the py launcher's default 3.x
for /f "delims=" %%L in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do call :try "%%L"
rem 4) conda installs
call :try "%USERPROFILE%\miniconda3\python.exe"
call :try "%USERPROFILE%\anaconda3\python.exe"
rem 5) whatever is on PATH
for %%P in (python.exe) do call :try "%%~$PATH:P"
exit /b 0

:try
if defined PY exit /b 0
if "%~1"=="" exit /b 0
if not exist "%~1" exit /b 0
"%~1" -c "import PySide6" >nul 2>&1
if errorlevel 1 exit /b 0
set "PY=%~1"
set "PYW=%~dp1pythonw.exe"
if not exist "!PYW!" set "PYW=%~1"
exit /b 0
