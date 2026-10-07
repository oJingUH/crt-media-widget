@echo off
rem ===========================================================================
rem  CRT-MEDIA // setup - one-time, from-source install
rem
rem  Double-click this file. It needs no administrator rights, it makes no
rem  registry changes, and it does everything inside the project folder.
rem
rem  What it does:
rem    1. finds a usable Python (prefers the "py" launcher, falls back to
rem       "python" on PATH; needs CPython 3.11 or newer, because pythonnet
rem       3.2.0 publishes wheels only from 3.11 up. 3.14 is what the app is
rem       developed and tested against)
rem    2. creates .venv next to this script - with uv when uv is available,
rem       otherwise with "python -m venv"
rem    3. installs requirements.txt into it
rem    4. verifies that the new venv can import webview, winrt.windows.
rem       media.control, pycaw, pystray and PIL, then prints SUCCESS
rem
rem  On any failure it says exactly which step failed and stops; it never
rem  carries on into a half-installed state.
rem
rem  Optional arguments:
rem    --recreate   delete and rebuild .venv from scratch
rem    --nopause    never wait for a keypress (for scripted runs; the same can
rem                 be done by setting the environment variable CRT_SETUP_NOPAUSE)
rem ===========================================================================
setlocal EnableExtensions
title CRT-MEDIA setup

set "HERE=%~dp0"
set "VENV=%HERE%.venv"
set "VE_PY=%VENV%\Scripts\python.exe"
set "VE_PYW=%VENV%\Scripts\pythonw.exe"
set "REQ=%HERE%requirements.txt"
set "PYLAUNCH="
set "MISSING="

set "NOPAUSE="
set "RECREATE="
if defined CRT_SETUP_NOPAUSE set "NOPAUSE=1"
if "%~1"=="" goto :argsdone
for %%A in (%*) do (
  if /I "%%A"=="--recreate" set "RECREATE=1"
  if /I "%%A"=="recreate" set "RECREATE=1"
  if /I "%%A"=="--nopause" set "NOPAUSE=1"
)
:argsdone

echo ===========================================================================
echo  CRT-MEDIA // setup
echo ===========================================================================
echo  project   %HERE%
echo.
echo  No admin rights needed. Everything happens inside this folder and the
echo  new .venv it creates; nothing is installed system-wide.
echo.

if not exist "%REQ%" (
  echo [FAIL] requirements.txt is missing from
  echo        %REQ%
  echo        Run setup.cmd from inside the CRT-MEDIA project folder.
  goto :failuse
)

rem ---------------------------------------------------------------------------
rem  1. find Python
rem ---------------------------------------------------------------------------
echo [1/4] looking for Python ...

py -3.14 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYLAUNCH=py -3.14"
if defined PYLAUNCH goto :havepython

py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYLAUNCH=py -3"
if defined PYLAUNCH goto :havepython

py -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYLAUNCH=py"
if defined PYLAUNCH goto :havepython

python -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYLAUNCH=python"
if defined PYLAUNCH goto :havepython

echo [FAIL] No Python found on this machine.
echo.
echo        Install Python 3.11 or newer (3.14 recommended) from
echo            https://www.python.org/downloads/windows/
echo        During the install, tick "Add python.exe to PATH", then run
echo        setup.cmd again.
echo.
echo        If you do not want to install Python at all, use the portable
echo        build instead: download CRT-MEDIA-1.0.0-portable.zip from
echo            https://github.com/oJingUH/crt-media-widget/releases/latest
echo        extract it anywhere and double-click CRT-MEDIA.vbs inside it.
goto :failuse

:havepython
for /f "delims=" %%P in ('%PYLAUNCH% -c "import sys;print(sys.executable)"') do set "PYEXE=%%P"
for /f "delims=" %%V in ('%PYLAUNCH% -c "import sys;print(sys.version.split()[0])"') do set "PYVER=%%V"

if not defined PYEXE (
  echo [FAIL] Python responded to -c "import sys" but its path could not be read.
  echo        Detected launcher: %PYLAUNCH%
  goto :failuse
)
echo        found Python %PYVER%
echo        %PYEXE%

%PYLAUNCH% -c "import sys;raise SystemExit(0 if sys.version_info[:2]>=(3,11) else 1)"
if errorlevel 1 (
  echo [FAIL] Python %PYVER% is too old. CRT-MEDIA needs 3.11 or newer;
  echo        3.14 is what it is developed and tested against. Python 3.10
  echo        cannot work: pythonnet publishes no wheel for it.
  goto :failuse
)

rem ---------------------------------------------------------------------------
rem  2. create the virtual environment
rem ---------------------------------------------------------------------------
echo.
echo [2/4] preparing %VENV%

if /I "%RECREATE%"=="1" if exist "%VENV%" (
  echo        --recreate: removing the existing .venv
  rmdir /s /q "%VENV%"
)

if exist "%VE_PY%" goto :havevenv

where uv >nul 2>nul
if errorlevel 1 goto :mkvenv_venv
echo        creating .venv with uv
uv venv --python "%PYEXE%" "%VENV%"
if errorlevel 1 (
  echo [FAIL] "uv venv" failed - see its output above.
  goto :failuse
)
goto :havevenv

:mkvenv_venv
echo        uv was not found; creating .venv with "%PYEXE%" -m venv
"%PYEXE%" -m venv "%VENV%"
if errorlevel 1 (
  echo [FAIL] "python -m venv" failed. A common cause is a Python install
  echo        without the venv module; re-run the Python installer and enable
  echo        "pip" and "venv".
  goto :failuse
)

:havevenv
if not exist "%VE_PY%" (
  echo [FAIL] %VE_PY%
  echo        does not exist after creating the environment.
  goto :failuse
)
if not exist "%VE_PYW%" (
  echo [FAIL] %VE_PYW%
  echo        is missing - pythonw.exe should come with the environment.
  goto :failuse
)
echo        .venv is ready

rem ---------------------------------------------------------------------------
rem  3. install the dependencies
rem ---------------------------------------------------------------------------
echo.
echo [3/4] installing requirements.txt
where uv >nul 2>nul
if errorlevel 1 goto :install_pip

uv pip install --python "%VE_PY%" -r "%REQ%"
if errorlevel 1 (
  echo [FAIL] "uv pip install" failed - see its output above.
  goto :failuse
)
goto :verify

:install_pip
"%VE_PY%" -m pip install --upgrade pip
if errorlevel 1 (
  echo [FAIL] "python -m pip install --upgrade pip" failed - see above.
  goto :failuse
)
"%VE_PY%" -m pip install -r "%REQ%"
if errorlevel 1 (
  echo [FAIL] "pip install -r requirements.txt" failed - see its output above.
  goto :failuse
)

rem ---------------------------------------------------------------------------
rem  4. verify the environment can actually import the widget's dependencies
rem ---------------------------------------------------------------------------
:verify
echo.
echo [4/4] verifying the new environment
set "MISSING="
"%VE_PY%" -c "import webview"                      >nul 2>nul || set "MISSING=%MISSING% pywebview"
"%VE_PY%" -c "import winrt.windows.media.control"  >nul 2>nul || set "MISSING=%MISSING% winrt-Windows.Media.Control"
"%VE_PY%" -c "import pycaw"                        >nul 2>nul || set "MISSING=%MISSING% pycaw"
"%VE_PY%" -c "import pystray"                      >nul 2>nul || set "MISSING=%MISSING% pystray"
"%VE_PY%" -c "import PIL"                          >nul 2>nul || set "MISSING=%MISSING% pillow"

if defined MISSING goto :failverify

echo        imports OK: webview, winrt.windows.media.control, pycaw, pystray, PIL
echo.
echo ===========================================================================
echo  SUCCESS - CRT-MEDIA is set up and ready to run.
echo ===========================================================================
echo.
echo  Start the widget now with either of these, from this folder:
echo.
echo      run.cmd        normal start, no console window
echo      run.vbs        the same, silent - use this one for the Startup folder
echo.
echo  If something misbehaves, this keeps a log on screen:
echo.
echo      run.cmd --debug
echo.
echo ===========================================================================
call :waitkey
endlocal
exit /b 0

rem ---------------------------------------------------------------------------
:failverify
echo [FAIL] the new environment was built, but these packages do not import:
echo            %MISSING%
echo.
echo        Nothing else was changed. Try:
echo            setup.cmd --recreate
echo        to build .venv again from scratch, and read the pip/uv output above
echo        if it fails again.
goto :failuse

:failuse
echo.
echo Setup stopped. Nothing was left in a half-installed state.
call :waitkey
endlocal
exit /b 1

rem ---------------------------------------------------------------------------
:waitkey
if defined NOPAUSE exit /b 0
echo.
echo   This window closes by itself in 45 seconds.
timeout /t 45 >nul 2>nul
exit /b 0
