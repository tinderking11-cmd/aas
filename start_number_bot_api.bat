@echo off
title BDX TOP Number Bot
cd /d "%~dp0"

if exist "namberx\namberbot.py" (
    cd /d "%~dp0namberx"
)

echo Starting BDX TOP Number Bot...
python namberbot.py

echo.
echo Bot stopped. Press any key to close this window.
pause >nul
