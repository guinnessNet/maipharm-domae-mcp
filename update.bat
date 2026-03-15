@echo off
chcp 65001 >nul
echo.
echo ========================================
echo   마이팜 도매 통합검색 - 업데이트
echo ========================================
echo.

:: Python 확인
python --version >nul 2>&1
if errorlevel 1 (
    echo [오류] Python이 설치되어 있지 않습니다.
    echo        먼저 install.bat를 실행해주세요.
    pause
    exit /b 1
)

:: git 확인
git --version >nul 2>&1
if errorlevel 1 (
    echo [!] git이 없습니다. 수동 업데이트가 필요합니다.
    echo.
    echo 1. https://github.com/guinnessNet/maipharm-domae-mcp 에서 최신 버전 다운로드
    echo 2. 기존 폴더에 덮어쓰기
    echo 3. install.bat 실행
    echo.
    pause
    exit /b 1
)

echo [1/2] 최신 버전 다운로드 중...
git pull --ff-only
if errorlevel 1 (
    echo.
    echo [!] 자동 업데이트 실패. 수동 업데이트를 시도하세요.
    echo     git stash 후 다시 시도하거나, 새로 다운로드하세요.
    pause
    exit /b 1
)

echo [2/2] 패키지 재설치 중...
pip install . --quiet
if errorlevel 1 (
    echo [오류] 패키지 설치 실패. install.bat를 다시 실행해주세요.
    pause
    exit /b 1
)

echo.
echo ========================================
echo   업데이트 완료!
echo ========================================
echo.
echo   start.bat로 실행하세요.
echo.
pause
