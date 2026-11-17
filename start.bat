@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto install
py -3.12 -m venv .venv
if errorlevel 1 goto fail
:install
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto fail
".venv\Scripts\python.exe" spatial_audio.py
if errorlevel 1 goto fail
exit /b 0
:fail
echo Launch failed. Install Python 3.12 and check the error above.
pause
exit /b 1
