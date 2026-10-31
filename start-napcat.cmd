@echo off
setlocal DisableDelayedExpansion
set "NAPCAT_HOME=%~dp0napcat\v4.18.42"
if not exist "%NAPCAT_HOME%\launcher.bat" (
    echo NapCat v4.18.42 is missing from the local napcat folder.
    pause
    exit /b 1
)
call "%NAPCAT_HOME%\launcher.bat" %*
exit /b %ERRORLEVEL%
