@echo off
rem Usage: Render.bat project.odv output.mp4 [--start 10 --end 100 --width 1920]
cd /d "%~dp0"
".venv\Scripts\python.exe" -m odv render %*
pause
