@echo off
call "%~dp0scripts\manage-agent.cmd" start "%~1"
exit /b %ERRORLEVEL%
