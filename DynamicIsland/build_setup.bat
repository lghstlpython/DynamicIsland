@echo off
cd /d "%~dp0"
if /i "%~1"=="skipbuild" goto have_exe

echo === Step 1/3: building DynamicIsland.exe ===
call build.bat nopause
if errorlevel 1 goto fail

:have_exe
if not exist dist\DynamicIsland.exe (
  echo ERROR: dist\DynamicIsland.exe not found - run build.bat first.
  goto fail
)

echo.
echo === Step 2/3: looking for Inno Setup ===
set "ISCC="
call :find
if defined ISCC goto build

echo Inno Setup not found - downloading it from jrsoftware.org ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -UseBasicParsing -Uri 'https://jrsoftware.org/download.php/is.exe' -OutFile ($env:TEMP + '\innosetup.exe'); exit 0 } catch { Write-Host $_.Exception.Message; exit 1 }"
if errorlevel 1 goto manual
echo Installing Inno Setup (takes a few seconds, no admin rights needed) ...
"%TEMP%\innosetup.exe" /VERYSILENT /SUPPRESSMSGS /NORESTART /CURRENTUSER
call :find
if defined ISCC goto build

:manual
echo.
echo ERROR: Inno Setup could not be installed automatically.
echo   1) Download "Inno Setup 6" from https://jrsoftware.org/isdl.php and install it (Next, Next, ...).
echo   2) Run:  build_setup.bat skipbuild      (the exe is already built, so it is quick)
goto fail

:build
echo.
echo === Step 3/3: creating the installer ===
"%ISCC%" setup.iss
if errorlevel 1 goto fail
echo.
echo Done! Installer: installer\DynamicIsland-Setup.exe
pause
exit /b 0

:find
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
exit /b 0

:fail
echo.
echo Setup build failed. Scroll up to read the first error message.
pause
exit /b 1
