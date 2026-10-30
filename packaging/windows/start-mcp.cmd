@echo off
setlocal
"%~dp0python\python.exe" "%~dp0adapter\launch.py" %*
exit /b %errorlevel%
