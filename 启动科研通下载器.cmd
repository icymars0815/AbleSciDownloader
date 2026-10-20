@echo off
cd /d "%~dp0"
if exist "%~dp0runtime\python\python.exe" (
  "%~dp0runtime\python\python.exe" "%~dp0portable_bootstrap.py"
  if errorlevel 1 pause
  exit /b
)
if not exist "%~dp0.venv\Scripts\pythonw.exe" (
  echo Please extract the full portable ZIP, or run setup.cmd for the source edition.
  pause
  exit /b 1
)
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0launcher.py"
