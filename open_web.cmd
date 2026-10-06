@echo off
REM ============================================================
REM  PortfolioNewsUpdater - open the hosted news web app
REM
REM  Double-click this file. It reads the site address from
REM  web_url.txt (written by the panel at startup and on every
REM  config upload), falling back to the web_url key in
REM  config_local.json, and opens it in your browser.
REM
REM  The parsing and the launch happen in open_web.py on purpose:
REM  pulling one key out of a JSON file in batch needs nested
REM  quoting that cmd.exe mangles.
REM
REM  NOTE: this comment block deliberately contains no double
REM  quotes - cmd.exe counts them BEFORE deciding a line is a
REM  comment (the full story is in open_panel.cmd).
REM ============================================================
cd /d "%~dp0"

REM --- Find a real Python interpreter (same logic as start_cloud.bat) ---
REM `where python` can return the Microsoft Store stub in WindowsApps,
REM which only opens the Store instead of running anything.
set "PY="
for %%P in (python.exe) do if not defined PY (
  for /f "delims=" %%W in ('where %%P 2^>nul') do (
    echo %%W | findstr /i "WindowsApps" >nul || if not defined PY set "PY=%%W"
  )
)
if not defined PY (
  where py >nul 2>nul && set "PY=py"
)
if not defined PY (
  if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
  if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  if exist "C:\Python314\python.exe" set "PY=C:\Python314\python.exe"
  if exist "C:\Python313\python.exe" set "PY=C:\Python313\python.exe"
  if exist "C:\Python312\python.exe" set "PY=C:\Python312\python.exe"
)
if not defined PY (
  echo.
  echo Could not find Python. Install it from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" during installation.
  echo.
  pause
  exit /b 1
)

"%PY%" open_web.py
if errorlevel 1 pause
