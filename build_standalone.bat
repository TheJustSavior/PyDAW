@echo off
REM Build standalone PyDAW executable with PyInstaller
echo ========================================
echo PyDAW Standalone Build Script
echo ========================================
echo.

REM Check if PyInstaller is installed
python -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo PyInstaller not found. Installing...
    pip install pyinstaller
    if errorlevel 1 (
        echo Failed to install PyInstaller
        pause
        exit /b 1
    )
)

echo Building PyDAW standalone executable...
echo.

python build_exe.py

if errorlevel 1 (
    echo.
    echo Build FAILED!
    pause
    exit /b 1
)

echo.
echo ========================================
echo Build completed successfully!
echo ========================================
echo Executable location: dist_pyinstaller\PyDAW.exe
echo.
pause
