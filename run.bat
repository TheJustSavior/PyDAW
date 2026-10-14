@echo off
setlocal

rem PyDAW launcher: double-click this file from the project folder.
cd /d "%~dp0"

set "PYTHON_CMD="
where py >nul 2>nul
if not errorlevel 1 (
    set "PYTHON_CMD=py -3"
) else (
    where python >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=python"
)

if not defined PYTHON_CMD (
    echo Python 3.10+ was not found.
    echo Install Python, then run:
    echo   pip install -e . --no-build-isolation
    echo.
    pause
    exit /b 1
)

%PYTHON_CMD% -c "import PySide6" >nul 2>nul
if errorlevel 1 (
    echo PyDAW dependencies are not installed for this Python.
    echo From this folder run:
    echo   %PYTHON_CMD% -m pip install -e . --no-build-isolation
    echo.
    pause
    exit /b 1
)

rem Keep native binaries fresh for dev/double-click runs. This copies the
rem latest CMake Release pydaw_engine/pydaw_vst_host into the workspace package
rem and any installed pydaw package before Python imports pydaw_engine.
%PYTHON_CMD% "%CD%\pydaw\_sync_native.py"
if errorlevel 1 (
    echo Native binary sync failed.
    pause
    exit /b 1
)
if exist "%CD%\pydaw\pydaw_vst_host.exe" (
    set "PYDAW_VST_HOST_EXE=%CD%\pydaw\pydaw_vst_host.exe"
)

%PYTHON_CMD% -m pydaw %*
set "EXIT_CODE=%errorlevel%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo PyDAW exited with an error ^(code %EXIT_CODE%^).
    pause
)
exit /b %EXIT_CODE%
