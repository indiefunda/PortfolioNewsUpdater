@echo off
REM ============================================================
REM  PortfolioNewsUpdater - start the control panel
REM
REM  Double-click this file. It starts the panel and opens the
REM  panel URL in your browser automatically.
REM ============================================================
cd /d "%~dp0"

REM --- Find a real Python interpreter -------------------------------
REM Note: `where python` can return the Microsoft Store stub
REM (WindowsApps\python.exe), which merely opens the Store instead of
REM running anything, so it is skipped explicitly. The old check
REM `if not exist "%PY%"` tested for a FILE named "python" in the
REM current directory, so it never matched a PATH command.
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

REM --- Open the browser at the port the panel ACTUALLY bound to -----
REM The panel may fall back to 8002/8003 if 8001 is taken, and it writes
REM panel_url.txt with the real URL. Opening a hardcoded :8001 could show a
REM completely different app (e.g. the price monitor) and you would then edit
REM the wrong service's configuration.
del "%TEMP%\pn_panel_url.txt" >nul 2>nul
start "" /b cmd /c "timeout /t 3 /nobreak >nul & (set /p U=<"%~dp0panel_url.txt" 2>nul || set U=http://localhost:8001) & start "" "%U%""

REM --- Run the panel (stays open until you close the window) --------
"%PY%" cloud_manager.py

echo.
echo The panel has stopped. Close this window.
pause
