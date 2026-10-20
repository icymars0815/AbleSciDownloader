@echo off
rem KYTDL MCP stdio launcher: works from any working directory. Keep stdout clean (MCP protocol); no echo.
set "ROOT=%~dp0"
if not exist "%ROOT%.venv\Scripts\python.exe" (
  echo KYTDL: virtual environment not found. Run setup.cmd in "%ROOT%" first. 1>&2
  exit /b 1
)
"%ROOT%.venv\Scripts\python.exe" "%ROOT%mcp_server.py" %*
exit /b %ERRORLEVEL%
