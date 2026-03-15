#!/bin/bash
# 프론트엔드 빌드 → static/ 배치
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
FRONTEND_DIR="$PROJECT_DIR/frontend"
STATIC_DIR="$PROJECT_DIR/src/domae_mcp/static"

echo "프론트엔드 빌드 시작..."
cd "$FRONTEND_DIR"
npm run build

echo "빌드 결과물 복사..."
rm -rf "$STATIC_DIR"/*
cp -r dist/* "$STATIC_DIR"/

echo "완료: $STATIC_DIR"
ls -la "$STATIC_DIR"
