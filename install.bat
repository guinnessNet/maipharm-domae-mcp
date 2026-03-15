@echo off
chcp 65001 >nul
echo.
echo ========================================
echo   마이팜 도매 통합검색 - 설치
echo ========================================
echo.

:: ─── 1단계: Python 확인 ───
python --version >nul 2>&1
if errorlevel 1 (
    echo [!] Python이 설치되어 있지 않습니다.
    echo.
    echo Python을 자동으로 설치합니다...
    echo.
    goto :install_python
) else (
    echo [1/3] Python 확인 완료
    python --version
    goto :install_package
)

:: ─── Python 자동 설치 ───
:install_python

:: winget 사용 가능 여부 확인 (Windows 10 1709+, Windows 11)
winget --version >nul 2>&1
if errorlevel 1 (
    goto :install_python_download
)

echo [1/3] winget으로 Python 설치 중...
echo      (설치 중 화면이 잠시 멈출 수 있습니다)
echo.
winget install Python.Python.3.12 --accept-package-agreements --accept-source-agreements --silent
if errorlevel 1 (
    echo [!] winget 설치 실패. 직접 다운로드를 시도합니다...
    goto :install_python_download
)

:: PATH 갱신 (새 cmd 세션 필요 없이 현재 세션에 반영)
set "PATH=%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%PATH%"

python --version >nul 2>&1
if errorlevel 1 (
    echo [!] Python PATH 등록 대기 중...
    echo     설치 완료 후 이 창을 닫고 install.bat를 다시 실행해주세요.
    pause
    exit /b 1
)

echo      Python 설치 완료!
python --version
echo.
goto :install_package

:: ─── Python 직접 다운로드 설치 ───
:install_python_download

echo [1/3] Python 다운로드 중...

:: 임시 폴더에 설치파일 다운로드
set "PYTHON_INSTALLER=%TEMP%\python-installer.exe"
set "PYTHON_URL=https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe"

:: PowerShell로 다운로드
powershell -Command "& { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%PYTHON_URL%' -OutFile '%PYTHON_INSTALLER%' }" 2>nul
if errorlevel 1 (
    :: curl 시도 (Windows 10 1803+)
    curl -L -o "%PYTHON_INSTALLER%" "%PYTHON_URL%" 2>nul
)

if not exist "%PYTHON_INSTALLER%" (
    echo.
    echo [오류] Python 다운로드에 실패했습니다.
    echo.
    echo 아래 링크에서 직접 다운로드해주세요:
    echo https://www.python.org/downloads/
    echo.
    echo 설치 시 "Add Python to PATH"를 반드시 체크하세요!
    echo.
    pause
    exit /b 1
)

echo      다운로드 완료. 설치 중...
echo.
echo      ┌─────────────────────────────────────────┐
echo      │  Python 설치 창이 열립니다.               │
echo      │  그냥 기다리시면 자동으로 설치됩니다.      │
echo      └─────────────────────────────────────────┘
echo.

:: /quiet: 무인 설치, PrependPath=1: PATH 자동 등록
"%PYTHON_INSTALLER%" /quiet InstallAllUsers=0 PrependPath=1 Include_test=0 Include_launcher=1
if errorlevel 1 (
    :: 무인 설치 실패 시 UI 모드로 재시도
    echo [!] 자동 설치 실패. 수동 설치 화면을 엽니다...
    echo     "Add Python to PATH" 체크 후 Install Now를 클릭하세요.
    "%PYTHON_INSTALLER%"
)

:: 설치파일 정리
del "%PYTHON_INSTALLER%" 2>nul

:: PATH 갱신
set "PATH=%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%PATH%"

python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo [!] Python 설치는 완료되었지만 PATH 등록이 필요합니다.
    echo     이 창을 닫고 install.bat를 다시 실행해주세요.
    echo.
    pause
    exit /b 1
)

echo [1/3] Python 설치 완료!
python --version
echo.

:: ─── 2단계: pip 업그레이드 ───
:install_package

echo [2/3] pip 업그레이드 중...
python -m pip install --upgrade pip --quiet 2>nul

echo [3/3] 도매 통합검색 설치 중...
pip install . --quiet
if errorlevel 1 (
    echo.
    echo [오류] 패키지 설치에 실패했습니다.
    echo 다시 시도해주세요.
    pause
    exit /b 1
)

echo.
echo ========================================
echo   설치 완료!
echo ========================================
echo.
echo   실행: start.bat 더블클릭
echo   접속: http://localhost:5900
echo.
echo   처음 사용 시:
echo   1. 팜스퀘어(pharmsq.com)에서 무료 가입
echo   2. 설정 ^> 도매 통합검색 ^> API 키 발급
echo   3. start.bat 실행 ^> 설정 ^> API 키 입력
echo.
pause
