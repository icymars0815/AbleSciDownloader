@echo off
set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
"%PY%" "%~dp0kytdl.py" %*
exit /b %ERRORLEVEL%
