@echo off
rem Same as the start file but keeps a console window open to show errors.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run "Start Onboard DataVis.bat" once first to install everything.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m odv %*
pause
