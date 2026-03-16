@echo off

echo.
echo ========================================
echo   Maipharm Domae - Install
echo ========================================
echo.

:: --- Step 1: Check Python ---
python --version >nul 2>&1
if errorlevel 1 (
    echo [!] Python is not installed.
    echo.
    echo Installing Python automatically...
    echo.
    goto :install_python
) else (
    echo [1/3] Python OK
    python --version
    goto :install_package
)

:: --- Install Python via winget ---
:install_python

winget --version >nul 2>&1
if errorlevel 1 (
    goto :install_python_download
)

echo [1/3] Installing Python via winget...
echo      (Please wait...)
echo.
winget install Python.Python.3.12 --accept-package-agreements --accept-source-agreements --silent
if errorlevel 1 (
    echo [!] winget install failed. Trying direct download...
    goto :install_python_download
)

set "PATH=%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%PATH%"

python --version >nul 2>&1
if errorlevel 1 (
    echo [!] Python installed but PATH not updated.
    echo     Please close this window and run install.bat again.
    pause
    exit /b 1
)

echo      Python installed!
python --version
echo.
goto :install_package

:: --- Install Python via direct download ---
:install_python_download

echo [1/3] Downloading Python...

set "PYTHON_INSTALLER=%TEMP%\python-installer.exe"
set "PYTHON_URL=https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe"

powershell -Command "& { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%PYTHON_URL%' -OutFile '%PYTHON_INSTALLER%' }" 2>nul
if errorlevel 1 (
    curl -L -o "%PYTHON_INSTALLER%" "%PYTHON_URL%" 2>nul
)

if not exist "%PYTHON_INSTALLER%" (
    echo.
    echo [ERROR] Python download failed.
    echo.
    echo Please download Python manually:
    echo https://www.python.org/downloads/
    echo.
    echo IMPORTANT: Check "Add Python to PATH" during install!
    echo.
    pause
    exit /b 1
)

echo      Download complete. Installing...
echo.

"%PYTHON_INSTALLER%" /quiet InstallAllUsers=0 PrependPath=1 Include_test=0 Include_launcher=1
if errorlevel 1 (
    echo [!] Silent install failed. Opening manual installer...
    echo     Check "Add Python to PATH" then click Install Now.
    "%PYTHON_INSTALLER%"
)

del "%PYTHON_INSTALLER%" 2>nul

set "PATH=%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%PATH%"

python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo [!] Python installed but PATH not updated.
    echo     Please close this window and run install.bat again.
    echo.
    pause
    exit /b 1
)

echo [1/3] Python installed!
python --version
echo.

:: --- Step 2: pip upgrade ---
:install_package

echo [2/3] Upgrading pip...
python -m pip install --upgrade pip --quiet 2>nul

echo [3/3] Installing Maipharm Domae...
pip install . --quiet
if errorlevel 1 (
    echo.
    echo [ERROR] Package install failed.
    echo Please try again.
    pause
    exit /b 1
)

echo.
echo ========================================
echo   Install Complete!
echo ========================================
echo.
echo   Run:  start.bat (double-click)
echo   Open: http://localhost:5900
echo.
echo   First time setup:
echo   1. Sign up at pharmsq.com (free)
echo   2. Settings - Domae - Get API key
echo   3. Run start.bat - Enter API key
echo.
pause
