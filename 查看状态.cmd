@echo off
call "%~dp0scripts\manage-agent.cmd" status "%~1"
exit /b %ERRORLEVEL%
