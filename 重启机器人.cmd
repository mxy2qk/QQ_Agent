@echo off
call "%~dp0scripts\manage-agent.cmd" restart "%~1"
exit /b %ERRORLEVEL%
