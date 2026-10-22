@echo off
cd /d "%~dp0"

if not exist ".env\Scripts\python.exe" (
    echo Project Python environment was not found at .env\Scripts\python.exe
    echo Create it and install requirements.txt before starting the app.
    pause
    exit /b 1
)

".env\Scripts\python.exe" app.py
if errorlevel 1 pause
