@echo off
chcp 65001 >nul

:: Python 확인
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo [오류] Python이 설치되어 있지 않습니다.
    echo        먼저 install.bat를 실행해주세요.
    echo.
    pause
    exit /b 1
)

:: 패키지 설치 확인
python -c "import domae_mcp" >nul 2>&1
if errorlevel 1 (
    echo.
    echo [오류] 도매 통합검색이 설치되어 있지 않습니다.
    echo        먼저 install.bat를 실행해주세요.
    echo.
    pause
    exit /b 1
)

echo.
echo  마이팜 도매 통합검색
echo  ────────────────────
echo  브라우저에서 http://localhost:5900 접속하세요.
echo  종료하려면 이 창을 닫으세요.
echo.

:: 2초 후 브라우저 자동 열기
start /b cmd /c "timeout /t 2 /nobreak >nul && start http://localhost:5900"

:: 서버 실행
python -m domae_mcp
