#!/bin/bash
# 마이팜 도매 통합검색 - Mac/Linux 설치 스크립트

set -e

echo ""
echo "========================================"
echo "  마이팜 도매 통합검색 - 설치"
echo "========================================"
echo ""

# Python 확인
if ! command -v python3 &> /dev/null; then
    echo "[오류] Python3이 설치되어 있지 않습니다."
    echo ""
    echo "  Mac:   brew install python3"
    echo "  Linux: sudo apt install python3 python3-pip"
    echo ""
    exit 1
fi

PYTHON_VERSION=$(python3 --version 2>&1)
echo "[1/3] Python 확인 완료: $PYTHON_VERSION"

# pip 업그레이드
echo "[2/3] pip 업그레이드 중..."
python3 -m pip install --upgrade pip --quiet 2>/dev/null

# 패키지 설치
echo "[3/3] 도매 통합검색 설치 중..."
pip3 install . --quiet

echo ""
echo "========================================"
echo "  설치 완료!"
echo "========================================"
echo ""
echo "  실행: python3 -m domae_mcp"
echo "  접속: http://localhost:5900"
echo ""
echo "  처음 사용 시:"
echo "  1. pharmsq.com에서 무료 가입 후 API 키 발급"
echo "  2. 실행 후 설정에서 API 키 입력"
echo ""
