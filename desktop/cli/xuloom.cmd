@echo off
setlocal DisableDelayedExpansion
"%~dp0resources\python\python.exe" -I -X utf8 -m xuloom %*
exit /b %errorlevel%
