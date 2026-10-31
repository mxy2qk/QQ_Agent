@echo off
call "%~dp0scripts\manage-agent.cmd" stop "%~1"
exit /b %ERRORLEVEL%
