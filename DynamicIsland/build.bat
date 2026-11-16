@echo off
cd /d "%~dp0"
echo [1/6] Closing a running DynamicIsland.exe, if any...
taskkill /f /im DynamicIsland.exe >nul 2>&1
echo.
echo Python versions found:
py -0
echo.
echo [2/6] Checking files...
if not exist island.py (echo ERROR: island.py not found next to build.bat & goto fail)
if not exist sounds\hello.mp3 (echo ERROR: sounds\hello.mp3 is missing next to build.bat & goto fail)
if not exist sounds\goodbye.mp3 (echo ERROR: sounds\goodbye.mp3 is missing next to build.bat & goto fail)
if not exist character\meta.json (echo ERROR: folder "character" is missing next to build.bat & goto fail)
if not exist icon.ico (echo ERROR: icon.ico is missing next to build.bat & goto fail)
if exist venv rmdir /s /q venv
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist dist (echo ERROR: cannot delete the dist folder - close DynamicIsland.exe and any Explorer window showing it & goto fail)
echo [3/6] Creating environment (trying Python 3.12 first)...
py -3.12 -m venv venv 2>nul || py -3.11 -m venv venv 2>nul || py -m venv venv
if errorlevel 1 (echo ERROR: could not create the environment - is Python installed? & goto fail)
echo [4/6] Installing dependencies...
venv\Scripts\python.exe -m pip install --upgrade pip
venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (echo ERROR: pip install failed - check the messages above & goto fail)
echo Checking winsdk...
venv\Scripts\python.exe -c "import winsdk.windows.media.control; print(\"winsdk OK\")"
if errorlevel 1 (echo ERROR: winsdk does not work with this Python - install Python 3.12 & goto fail)
echo [5/6] Building exe...
venv\Scripts\python.exe -m PyInstaller --noconsole --onefile --name DynamicIsland --collect-all winsdk --add-data "sounds;sounds" --add-data "character;character" --icon icon.ico --add-data "icon.ico;." island.py
if errorlevel 1 (echo ERROR: PyInstaller failed - check the messages above & goto fail)
echo.
echo [6/6] Done! File: dist\DynamicIsland.exe
if /i not "%~1"=="nopause" pause
exit /b 0
:fail
echo.
echo Build failed. Scroll up to read the first error message.
if /i not "%~1"=="nopause" pause
exit /b 1
