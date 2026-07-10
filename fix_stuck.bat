@echo off
REM ============================================================
REM  SportyPredict - one-click recovery when the app is stuck
REM  Kills the app + any leftover hidden scraper browsers, then
REM  restarts the app in a visible window.
REM ============================================================
echo [1/3] Stopping the app and leftover scraper processes...
taskkill /IM SportybetPredictor.exe /T /F >nul 2>&1
taskkill /IM python.exe /T /F >nul 2>&1

REM Kill orphaned headless browsers left behind by killed scrapers
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"name='chrome.exe' or name='msedge.exe' or name='node.exe'\" | Where-Object { $_.CommandLine -match 'headless|playwright' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo [2/3] Waiting 5 seconds for ports and sessions to clear...
timeout /t 5 /nobreak >nul

echo [3/3] Starting the app in a new window (leave it open!)...
cd /d "%~dp0"
if exist app.py (
    start "SportybetPredictor" cmd /k python app.py
) else (
    start "" "%~dp0SportybetPredictor.exe"
)
echo Done. The app window is starting - give it 2-3 minutes to warm up.
echo If it STILL does not update after 10 minutes, check TROUBLESHOOTING.md
pause
