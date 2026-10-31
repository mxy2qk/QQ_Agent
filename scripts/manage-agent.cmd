@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
pushd "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
    echo Project .venv is missing. Follow README.md to install it first.
    set "RESULT=1"
    goto done
)
".venv\Scripts\python.exe" -m qq_agent.runtime "%~1"
set "RESULT=%ERRORLEVEL%"
:done
if /i not "%~2"=="--no-pause" pause
popd
exit /b %RESULT%
