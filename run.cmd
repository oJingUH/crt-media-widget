@echo off
setlocal
set "HERE=%~dp0"
set "PY=%HERE%.venv\Scripts\python.exe"
set "PYW=%HERE%.venv\Scripts\pythonw.exe"
set "APP=%HERE%app.py"

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
