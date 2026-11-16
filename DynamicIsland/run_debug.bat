@echo off
cd /d "%~dp0"
taskkill /f /im DynamicIsland.exe >nul 2>&1
if not exist venv\Scripts\python.exe (
  echo Run build.bat first - it creates the "venv" folder.
  pause
  exit /b 1
)
echo Starting Dynamic Island with a console. Errors will be shown below.
echo Close this window to stop the island.
echo.
venv\Scripts\python.exe island.py
echo.
echo The island stopped. If there is an error above, send me a screenshot of it.
pause
