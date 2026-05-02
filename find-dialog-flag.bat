@echo off
REM Discover the in-dialog memory flag and save it into config.yaml.
REM
REM Open Azahar with your Gen 6/7 game on the overworld first, then
REM double-click this file. The terminal will walk you through four
REM in-game states (off / on / off / on) and write the top candidate
REM to offsets.dialog_flag automatically.

setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 -m pokebot.find_dialog_flag --save-config config.yaml %*
) else (
    where python >nul 2>nul
    if %errorlevel%==0 (
        python -m pokebot.find_dialog_flag --save-config config.yaml %*
    ) else (
        echo.
        echo Python is not installed or not on PATH.
        echo Install Python 3.10+ from https://python.org and try again.
        echo.
        pause
        exit /b 1
    )
)

REM Always pause at the end so the user can read the candidate list
REM before the window closes.
echo.
pause
endlocal
