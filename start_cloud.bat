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

REM --- Start the panel in a separate window, then open the browser -----
REM
REM The browser step is a SEPARATE SCRIPT (open_panel.cmd) for two reasons:
REM
REM   1. It cannot be done inline here. The obvious one-liner
REM          start "" /b cmd /c "timeout /t 3 >nul & set /p U=<file & start ""%U%""
REM      expands %U% when the whole LINE is parsed - before `set` has run - so
REM      the browser was launched with an empty argument and nothing opened.
REM   2. Nesting a quoted path (%~dp0 ends with a backslash) inside another
REM      quoted cmd /c string is fragile. Its own script avoids all of it.
REM
REM The panel now starts in its own window FIRST and the helper then waits for
REM panel_url.txt (up to 20s). Previously the helper ran as a background job
REM started BEFORE the panel, so it depended on a 3-second delay and could be
REM torn down before it ever reached `start` - which is why the app sometimes
REM came up with no browser.
del "%~dp0panel_url.txt" >nul 2>nul
start "PortfolioNewsUpdater panel" cmd /k ""%PY%" cloud_manager.py"

REM Waits for panel_url.txt, then opens that URL. Runs in the foreground so it
REM cannot be killed early; it exits on its own once the browser is open.
call "%~dp0open_panel.cmd"

echo.
echo The panel is running in its own window. Close that window to stop it.
pause
