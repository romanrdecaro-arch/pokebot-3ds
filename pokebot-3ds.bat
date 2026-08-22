@echo off
REM pokebot-3ds launcher (Windows)
REM Double-click this file to start the GUI launcher.

setlocal
cd /d "%~dp0"

REM Prefer the Windows Python launcher 'py' (handles multiple installs);
REM fall back to 'python' on PATH.
REM
REM Note the goto-based structure: cmd.exe expands %errorlevel% for an
REM ENTIRE if/else block at parse time, so a nested "if %errorlevel%==0"
REM inside the else branch reads the value from BEFORE the inner command
REM ran. That made this script report "Python is not installed" to every
REM user who has python.exe on PATH but not the py launcher. The
REM "if errorlevel N" keyword form is evaluated at run time instead.
where py >nul 2>nul
if not errorlevel 1 goto :use_py

where python >nul 2>nul
if not errorlevel 1 goto :use_python

echo.
echo Python is not installed or not on PATH.
echo Install Python 3.10+ from https://python.org and try again.
echo.
pause
exit /b 1

:use_py
py -3 launcher.py %*
goto :finished

:use_python
python launcher.py %*
goto :finished

:finished
if errorlevel 1 (
    echo.
    echo Launcher exited with an error. See the messages above.
    pause
)

endlocal
