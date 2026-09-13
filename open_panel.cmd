@echo off
REM ============================================================
REM  PortfolioNewsUpdater - open the panel URL in the browser
REM
REM  A separate script on purpose. The URL must be read from
REM  panel_url.txt AFTER the panel has started (it writes the file
REM  once it has bound a port, and that port may be 8002/8003).
REM
REM  Doing it inline in start_cloud.bat does NOT work:
REM      cmd /c "... & set /p U=<file & start "" "%U%""
REM  expands %U% while the LINE is parsed - before `set` has run -
REM  so the browser was launched with an empty argument.
REM
REM  Delay uses `ping`, not `timeout`: `timeout` needs a console stdin
REM  and exits immediately with 'Input redirection is not supported'
REM  when there is none (e.g. launched from another script or a
REM  non-interactive context), which made this loop give up instantly
REM  and open the fallback port instead.
REM
REM  NOTE: no double quotes appear anywhere in this comment block on
REM  purpose. cmd.exe counts double quotes BEFORE deciding a line is a
REM  comment, so an odd number of them in a REM line makes the parser
REM  swallow every following line until the quotes balance. That is
REM  exactly what happened here: setlocal enabledelayedexpansion and
REM  set URL= were silently eaten, so this script printed
REM  '[open_panel] opening !URL!' and Windows tried to open a FILE
REM  called !URL!. _audit/check_batch_quotes.py now guards against it.
REM ============================================================
setlocal enabledelayedexpansion
set "URLFILE=%~dp0panel_url.txt"
set "URL="

REM Wait up to ~20s for the panel to bind a port and write the file.
for /L %%i in (1,1,20) do (
  if not defined URL (
    if exist "%URLFILE%" set /p URL=<"%URLFILE%"
  )
  if not defined URL ping -n 2 127.0.0.1 >nul 2>nul
)

REM Strip a UTF-8 BOM or stray quotes/whitespace a previous write may leave.
if defined URL set "URL=!URL:"=!"
if defined URL set "URL=!URL: =!"
for /f "tokens=* delims=ï»¿" %%A in ("!URL!") do set "URL=%%A"

if not defined URL (
  echo [open_panel] panel_url.txt not found after 20s - falling back to port 8001.
  set "URL=http://localhost:8001"
)

echo [open_panel] opening !URL!
start "" "!URL!"
endlocal
