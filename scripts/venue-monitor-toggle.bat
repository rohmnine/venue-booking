@echo off
REM ===========================================================================
REM  venue-monitor-toggle.bat -- one-click pause / resume of the auto monitor
REM
REM  Double-click this file:
REM     - if the monitor is RUNNING  -> it pauses
REM     - if the monitor is PAUSED   -> it resumes
REM  Run it again to switch back. The resulting state is always printed.
REM
REM  Why a .bat and not only a .ps1: double-clicking is the actual "one click".
REM  The .ps1 needs an -ExecutionPolicy flag, which you cannot pass from
REM  Explorer -- this launcher supplies it.
REM
REM  Keep this file PURE ASCII. cmd reads .bat with the OEM code page and
REM  non-ASCII text gets mangled. All Chinese messages come from the .ps1
REM  below, which is UTF-8 with BOM and renders correctly.
REM ===========================================================================

setlocal
set "RC=0"

set "SCRIPTS=%~dp0"
set "PS1=%SCRIPTS%setup-scheduler.ps1"

if not exist "%PS1%" (
  echo [FATAL] setup-scheduler.ps1 not found next to this file:
  echo         %PS1%
  set "RC=2"
  goto :done
)

REM  Use the Windows PowerShell that ships with the OS.
set "PSEXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PSEXE%" set "PSEXE=powershell.exe"

"%PSEXE%" -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -Action toggle
set "RC=%ERRORLEVEL%"

:done
echo.
REM  Pause ONLY when double-clicked, so calling it from a terminal does not hang.
REM  Double-click makes cmdcmdline contain this file's name; a plain cmd session
REM  does not.
echo "%cmdcmdline%" | find /i "%~nx0" >nul
if not errorlevel 1 (
  echo Press any key to close . . .
  pause >nul
)

endlocal & exit /b %RC%
