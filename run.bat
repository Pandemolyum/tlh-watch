@echo off
REM Start the Last Hunt watcher in a visible console window.
REM Close the window (or press Ctrl+C) to stop watching.
setlocal
cd /d "%~dp0"
title Last Hunt watcher - thelasthunt.com
py -3 tlhwatch.py %*
if errorlevel 1 (
  echo.
  echo The watcher exited with an error. Scroll up for details.
  pause
)
endlocal
