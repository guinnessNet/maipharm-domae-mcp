@echo off

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo [ERROR] Python is not installed.
    echo         Please run install.bat first.
    echo.
    pause
    exit /b 1
)

:: Check package
python -c "import domae_mcp" >nul 2>&1
if errorlevel 1 (
    echo.
    echo [ERROR] Maipharm Domae is not installed.
    echo         Please run install.bat first.
    echo.
    pause
    exit /b 1
)

echo.
echo  Maipharm Domae
echo  --------------------
echo  Open http://localhost:5900 in your browser.
echo  Close this window to stop the server.
echo.

:: Open browser after 2 seconds
start /b cmd /c "timeout /t 2 /nobreak >nul && start http://localhost:5900"

:: Run server (errors will show here)
python -m domae_mcp
if errorlevel 1 (
    echo.
    echo [ERROR] Server crashed. See error above.
    echo.
)
pause
