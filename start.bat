@echo off
cd /d "%~dp0"
if exist "C:\Python\Python\pythonw.exe" (
  start "" "C:\Python\Python\pythonw.exe" "%~dp0app.py"
) else (
  python app.py
  if errorlevel 1 pause
)
