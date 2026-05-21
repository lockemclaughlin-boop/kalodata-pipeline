@echo off
REM Windows launcher — sign in to Kalodata and Google Flow.
REM Double-click to run. Do this AFTER the first start.bat launch (which
REM builds the environment). Only needed once, or when a session expires.

cd /d "%~dp0"

if not exist ".venv\" (
    echo The app isn't built yet.
    echo Double-click start.bat first, wait for the dashboard, then run this.
    pause
    exit /b 1
)

echo ================================================================
echo  Step 1 of 2 - Sign in to Kalodata
echo ================================================================
echo A Chrome window will open. Sign into your Kalodata account, then
echo CLOSE THE WINDOW to continue.
echo.
.venv\Scripts\python scripts\login_kalodata.py

echo.
echo ================================================================
echo  Step 2 of 2 - Sign in to Google (Flow)
echo ================================================================
echo A Chrome window will open. Sign into the Google account with your
echo AI subscription, land on labs.google/fx/tools/flow, then CLOSE it.
echo.
.venv\Scripts\python scripts\login_flow.py

echo.
echo Done - both sessions are saved.
pause
