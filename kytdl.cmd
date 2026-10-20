@echo off
set "APP_PY=%~dp0runtime\python\python.exe"
if not exist "%APP_PY%" set "APP_PY=%~dp0.venv\Scripts\python.exe"
if not exist "%APP_PY%" set "APP_PY=python"
"%APP_PY%" "%~dp0kytdl.py" %*
exit /b %ERRORLEVEL%
