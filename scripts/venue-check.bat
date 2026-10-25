@echo off
REM ===========================================================================
REM  venue-check.bat -- venue booking monitor, scheduler edition
REM
REM  Called by Windows Task Scheduler once per hour.
REM  The "watch" subcommand decides what to do:
REM    - inside a poll window  -> poll every N seconds until the window ends
REM    - near a window start   -> run once, wait, then poll
REM    - outside any window    -> just run once and exit
REM    - before quiet hour     -> exit without any request
REM
REM  This file is SELF-LOCATING: every path is derived from where this file
REM  sits, so it works on any machine without editing paths.
REM  You only need to edit the two blocks marked  [EDIT]  below.
REM
REM  All paths must stay ASCII: non-ASCII paths in .bat files get mangled by
REM  the console code page and silently break things.
REM
REM  Manual test: double-click this file, then read the log it writes to
REM  ..\state\scheduler-logs\check-YYYYMMDD.log
REM ===========================================================================

setlocal

REM ---------------------------- [EDIT 1/2] ----------------------------------
REM  Which venue id to monitor.
REM  Leave EMPTY to monitor every enabled venue in venues.json.
set "VENUE="

REM ---------------------------- [EDIT 2/2] ----------------------------------
REM  Where to write the markdown/json reports. Empty = engine default.
REM  Defaults to <skill folder>\outputs -- change it if you want reports elsewhere.
set "OUT=%~dp0..\outputs"

REM =========================================================================
REM  Everything below is derived automatically -- do not edit.
REM =========================================================================

REM  %~dp0 always ends with a backslash, so it can be concatenated directly.
set "SCRIPTS=%~dp0"
set "SKILLPY=%SCRIPTS%venue_monitor.py"

REM  Normalize the "..\" out of the derived paths so logs show clean absolutes.
for %%I in ("%SCRIPTS%..\state\scheduler-logs")  do set "LOGDIR=%%~fI"
for %%I in ("%SCRIPTS%..\references\venues.json") do set "CFG=%%~fI"
if defined OUT for %%I in ("%OUT%") do set "OUT=%%~fI"

if not exist "%LOGDIR%" mkdir "%LOGDIR%"
if not exist "%SKILLPY%" (
  echo [FATAL] engine not found: %SKILLPY%
  echo [FATAL] engine not found: %SKILLPY% >> "%LOGDIR%\check-fatal.log"
  endlocal & exit /b 2
)
if not exist "%CFG%" (
  echo [FATAL] config not found: %CFG%
  echo [FATAL] config not found: %CFG% >> "%LOGDIR%\check-fatal.log"
  endlocal & exit /b 2
)

REM ---- locate a Python 3 interpreter --------------------------------------
REM  Probe order: common install locations, then whatever is on PATH.
REM  First hit wins. If none works, add your own path here.
set "PYCMD="
set "PYARG="

if not defined PYCMD if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PYCMD=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PYCMD if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYCMD=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PYCMD if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PYCMD=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PYCMD if exist "C:\Python313\python.exe" set "PYCMD=C:\Python313\python.exe"
if not defined PYCMD if exist "C:\Python312\python.exe" set "PYCMD=C:\Python312\python.exe"
if not defined PYCMD if exist "C:\Python311\python.exe" set "PYCMD=C:\Python311\python.exe"

if not defined PYCMD (
  for /f "delims=" %%Q in ('where python 2^>nul') do (
    if not defined PYCMD set "PYCMD=%%Q"
  )
)
if not defined PYCMD (
  for /f "delims=" %%Q in ('where py 2^>nul') do (
    if not defined PYCMD (
      set "PYCMD=%%Q"
      set "PYARG=-3"
    )
  )
)

if not defined PYCMD (
  echo [FATAL] no Python 3 found. Install Python 3.9+ or edit the probe list above.
  echo [FATAL] no Python 3 found. >> "%LOGDIR%\check-fatal.log"
  endlocal & exit /b 3
)

REM ---- one log file per day: check-YYYYMMDD.log ---------------------------
REM  NOTE: only pure-numeric slices of %date% are used. The raw %date% on a
REM  Chinese Windows contains a weekday token in GBK, which would corrupt
REM  this UTF-8 log file.
set "LOGFILE=%LOGDIR%\check-%date:~0,4%%date:~5,2%%date:~8,2%.log"

echo. >> "%LOGFILE%"
echo ========== start %date:~0,4%-%date:~5,2%-%date:~8,2% %time% ========== >> "%LOGFILE%"
echo [info] python : %PYCMD% %PYARG% >> "%LOGFILE%"
echo [info] config : %CFG% >> "%LOGFILE%"
echo [info] venue  : %VENUE% >> "%LOGFILE%"
echo [info] out    : %OUT% >> "%LOGFILE%"

REM ---- console banner -------------------------------------------------------
REM  Python's output is redirected to the log file (one line every 90s during a
REM  poll window would flood the console). The side effect is that a manual run
REM  shows NOTHING and looks like a hang -- especially because `watch` keeps
REM  polling until the window ends, which can be over an hour.
REM
REM  These lines print to the console only. When the Task Scheduler runs this
REM  file there is no console, so they vanish harmlessly.
REM
REM  Keep this file pure ASCII -- cmd reads it with the OEM code page and
REM  non-ASCII text gets mangled. Chinese explanations live in README.md.
echo.
echo  ============================================================
echo   venue monitor  ^|  %date:~0,4%-%date:~5,2%-%date:~8,2% %time%
echo  ============================================================
echo   [watch mode] This is NOT a one-shot command.
echo     - Inside a poll window : keeps polling every 90s until it ends.
echo     - Near a window start  : runs once, waits, then polls.
echo     - Outside any window   : runs once and exits (a few seconds).
echo   Example: window 13:50-15:40, now 13:53 --^> runs until 15:40.
echo.
echo   Want a single run?  python scripts\venue_monitor.py check
echo   Watch it live      :  powershell -c "Get-Content -Wait '%LOGFILE%'"
echo.
echo   Log : %LOGFILE%
echo  ============================================================
echo.

if defined VENUE (
  "%PYCMD%" %PYARG% "%SKILLPY%" watch -v %VENUE% -o "%OUT%" -c "%CFG%" >> "%LOGFILE%" 2>&1
) else (
  "%PYCMD%" %PYARG% "%SKILLPY%" watch -o "%OUT%" -c "%CFG%" >> "%LOGFILE%" 2>&1
)
set "RC=%ERRORLEVEL%"

echo ---------- end %date:~0,4%-%date:~5,2%-%date:~8,2% %time% (exit=%RC%) ---------- >> "%LOGFILE%"

echo  [done] exit code %RC%  --^> full output in the log above
echo.

endlocal & exit /b %RC%
