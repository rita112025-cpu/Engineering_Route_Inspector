@echo off
setlocal EnableExtensions
rem ============================================================================
rem  Engineering Route Inspector - Windows launcher. Double-click to start.
rem  - Runs only on this computer (127.0.0.1). Engineering files are never uploaded.
rem  - First run: creates a private Python environment in .\.venv and installs the
rem    packages listed in requirements.txt (this needs internet once).
rem  - Extra arguments are passed on, e.g.:  start-ui.bat --port 9000 --no-browser
rem ============================================================================
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
chcp 65001 >nul 2>&1

set "VENV=%~dp0.venv"
set "VPY=%VENV%\Scripts\python.exe"
if exist "%VPY%" goto have_venv

set "SYSPY="
for %%C in ("py -3" "python" "python3") do (
  if not defined SYSPY (
    %%~C -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "SYSPY=%%~C"
  )
)
if not defined SYSPY goto no_python
echo [1/3] Creating the private Python environment in .venv (first run only)...
%SYSPY% -m venv "%VENV%"
if errorlevel 1 goto venv_failed

:have_venv
set "STAMP=%VENV%\requirements.installed"
fc /b "%~dp0requirements.txt" "%STAMP%" >nul 2>&1
if not errorlevel 1 goto deps_ok
echo [2/3] Installing the required packages (internet needed once; no project data is sent)...
"%VPY%" -m pip install --disable-pip-version-check -r "%~dp0requirements.txt"
if errorlevel 1 goto pip_failed
copy /y "%~dp0requirements.txt" "%STAMP%" >nul

:deps_ok
echo [3/3] Starting. Keep this window open; close it to stop the program.
set "PYTHONPATH=%~dp0src"
"%VPY%" -m app %*
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" goto done
call :has_diagnose %*
if defined IS_DIAGNOSE goto done_rc
echo.
echo The program stopped with error code %RC%. Running the self-check:
echo.
"%VPY%" -m app --diagnose
echo.
echo See README.md, section "Troubleshooting". To get a diagnostics file, start the program
echo again and use the "Diagnostics" button.
pause
exit /b %RC%

:no_python
echo.
echo Python 3.10 or newer was not found.
echo Install Python from https://www.python.org/downloads/ and tick "Add python.exe to PATH",
echo then double-click start-ui.bat again.
pause
exit /b 1

:venv_failed
echo.
echo Could not create the Python environment in .venv. Check that the folder is writable,
echo then delete the .venv folder (if it exists) and try again.
pause
exit /b 1

:pip_failed
echo.
echo Package installation failed. Check the internet connection (or proxy settings) and try again.
echo Nothing from your projects was sent anywhere.
pause
exit /b 1

:done_rc
exit /b %RC%

:done
exit /b 0

rem Sets IS_DIAGNOSE when --diagnose is among the arguments (any position). This is the only place that decides
rem it. The arguments are walked with shift, not "for %%A in (%*)": that form turns * and ? into file names.
:has_diagnose
set "IS_DIAGNOSE="
:has_diagnose_next
if "%~1"=="" exit /b 0
if /i "%~1"=="--diagnose" set "IS_DIAGNOSE=1"
shift
goto has_diagnose_next
