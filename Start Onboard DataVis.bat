@echo off
setlocal
cd /d "%~dp0"
set "VENV=%~dp0.venv"
if exist "%VENV%\Scripts\pythonw.exe" goto run

echo ============================================================
echo  Onboard DataVis - first start: installing Python packages
echo  This happens once and needs an internet connection.
echo ============================================================
where py >nul 2>nul
if errorlevel 1 goto nopy
py -3 -m venv "%VENV%"
goto made
:nopy
python -m venv "%VENV%"
:made
if not exist "%VENV%\Scripts\python.exe" goto nopython
"%VENV%\Scripts\python.exe" -m pip install --upgrade pip
"%VENV%\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto pipfail

:run
start "" "%VENV%\Scripts\pythonw.exe" -m odv %*
exit /b 0

:nopython
echo.
echo Python 3.10 or newer was not found.
echo Install it from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH", then run this file again.
pause
exit /b 1

:pipfail
echo.
echo Installing the packages failed - see the messages above.
echo Delete the .venv folder and try again.
pause
exit /b 1
