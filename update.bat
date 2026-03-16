@echo off

echo.
echo ========================================
echo   Maipharm Domae - Update
echo ========================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed.
    echo         Please run install.bat first.
    pause
    exit /b 1
)

:: Check git
git --version >nul 2>&1
if errorlevel 1 (
    echo [!] git is not installed.
    echo.
    echo Manual update:
    echo 1. Download latest from GitHub Releases
    echo 2. Extract to this folder (overwrite)
    echo 3. Run install.bat
    echo.
    pause
    exit /b 1
)

echo [1/2] Downloading latest version...
git pull --ff-only
if errorlevel 1 (
    echo.
    echo [!] Auto-update failed.
    echo     Try: git stash then run again, or download fresh.
    pause
    exit /b 1
)

echo [2/2] Reinstalling package...
pip install . --quiet
if errorlevel 1 (
    echo [ERROR] Package install failed. Run install.bat again.
    pause
    exit /b 1
)

echo.
echo ========================================
echo   Update Complete!
echo ========================================
echo.
echo   Run start.bat to start.
echo.
pause
