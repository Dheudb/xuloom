@echo off
setlocal DisableDelayedExpansion
"%~dp0resources\python\python.exe" -I -X utf8 -B "%~dp0cli_install.py" %*
exit /b %errorlevel%
