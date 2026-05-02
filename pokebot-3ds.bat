@echo off
REM pokebot-3ds launcher (Windows)
REM Double-click this file to start the GUI launcher.

setlocal
cd /d "%~dp0"

REM Prefer the Windows Python launcher 'py' (handles multiple installs);
REM fall back to 'python' on PATH.
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 launcher.py %*
) else (
    where python >nul 2>nul
    if %errorlevel%==0 (
        python launcher.py %*
    ) else (
        echo.
        echo Python is not installed or not on PATH.
        echo Install Python 3.10+ from https://python.org and try again.
        echo.
        pause
        exit /b 1
    )
)

if errorlevel 1 (
    echo.
    echo Launcher exited with an error. See the messages above.
    pause
)

endlocal
