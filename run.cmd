@echo off
setlocal
set "HERE=%~dp0"
set "PY=%HERE%.venv\Scripts\python.exe"
set "PYW=%HERE%.venv\Scripts\pythonw.exe"
set "APP=%HERE%app.py"

if not exist "%APP%" goto :noapp
if not exist "%PY%" goto :novenv
if not exist "%PYW%" goto :novenv

if /I "%~1"=="--debug" goto debug
if /I "%~1"=="-d" goto debug

rem normal launch: no console window, returns immediately
start "" "%PYW%" "%APP%"
endlocal
exit /b 0

:debug
rem troubleshooting: keep a visible console carrying the widget's log
"%PY%" "%APP%" --debug
endlocal
exit /b %errorlevel%

:novenv
echo.
echo   RETRO-CONTROLLER is not set up yet on this machine.
echo.
echo   The project's virtual environment is missing:
echo       %PYW%
echo.
echo   Run setup.cmd once, from this folder, to create it and install the
echo   dependencies:
echo.
echo       setup.cmd
echo.
echo   When it reports SUCCESS, start the widget again - run.cmd for the
echo   normal silent launch, or run.cmd --debug to keep a log on screen.
echo.
echo   (Alternatively, use the portable build, which needs no Python at all:
echo    download RETRO-CONTROLLER-1.2.0-portable.zip from
echo    https://github.com/oJingUH/retro-controller/releases/latest
echo    extract it anywhere and double-click RETRO-CONTROLLER.vbs inside it.)
echo.
timeout /t 30 >nul 2>nul
endlocal
exit /b 1

:noapp
echo.
echo   RETRO-CONTROLLER cannot start: app.py is missing from
echo       %HERE%
echo.
echo   This folder does not look like a complete RETRO-CONTROLLER checkout.
echo.
timeout /t 30 >nul 2>nul
endlocal
exit /b 2
