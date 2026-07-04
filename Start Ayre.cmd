@echo off
REM ============================================================================
REM  Ayre launcher -- double-click to start.
REM  Runs the local UI bridge (python -m ayre_ui), which serves the interface on
REM  http://localhost:2500/ and opens your browser. From there: Installer -> pick
REM  a model -> Start Ayre.
REM
REM  This window IS Ayre's server log -- leave it open while you use Ayre, and
REM  close it (or press Ctrl+C) to shut the interface down.
REM
REM  Path-relative (%~dp0) so it runs from whatever drive letter the USB mounts
REM  at -- no editing needed.
REM ============================================================================
title Ayre
cd /d "%~dp0Ayre-UI"

REM Python resolution order. The kit is stdlib-only, so any Python 3 works.
REM 1) The Python bundled on the drive (python\python.exe) -- fully portable, no
REM    install needed on the destination PC. This is what ships on the USB.
REM 2) A system `python` on PATH (developer machines).
REM 3) The Windows `py` launcher (developer machines).
if exist "%~dp0python\python.exe" (
  "%~dp0python\python.exe" -m ayre_ui
  goto :ended
)
where python >nul 2>nul
if %errorlevel%==0 (
  python -m ayre_ui
  goto :ended
)
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -m ayre_ui
  goto :ended
)

echo.
echo   Could not find Python -- neither bundled (python\python.exe on the drive)
echo   nor installed on this PC.
echo.
echo   A shipped Ayre USB drive includes its own Python, so this usually means a
echo   file was left out during USB prep -- see USB_PREP.md. To run from source
echo   instead, install Python 3 from https://www.python.org/ (tick "Add python.exe
echo   to PATH"), then double-click this file again.
echo.
pause
goto :eof

:ended
REM Keep the window open if the bridge exited with an error (e.g. the port was
REM busy) so the message stays readable instead of flashing closed.
if errorlevel 1 pause
