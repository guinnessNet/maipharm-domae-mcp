# CLAUDE.md

## 프로젝트 개요

maipharm-domae-mcp: 로컬 설치형 의약품 도매 통합 검색/주문 MCP 서버.
사용자 PC에서 로컬 웹서버 + MCP 서버로 동작하여, 브라우저 UI와 AI(Claude Desktop) 양쪽에서 활용 가능.

- **기반**: domae-v2 (NAS용 웹앱)에서 코어 로직 이식
- **백엔드**: Python FastAPI + SQLite
- **프론트엔드**: React + Vite (경량 CSS)
- **MCP**: Python mcp SDK (stdio transport)
- **배포**: git clone + pip install (로컬 설치)

## 핵심 제약사항

- **Selenium 사용 금지**. BS4 + requests만 사용.
- SQLite 사용 (로컬 경량 DB).
- 로컬 전용 (127.0.0.1 바인딩), 인증 불필요.
- Windows 우선, Mac/Linux도 동작 가능하도록.

## 빌드 및 실행

```bash
# 설치
git clone https://github.com/xxx/maipharm-domae-mcp
cd maipharm-domae-mcp
pip install -r requirements.txt

# 프론트엔드 빌드
cd frontend && npm install && npm run build
# 빌드 결과물 → src/domae_mcp/static/ 에 복사

# 웹서버 모드
python -m domae_mcp

# MCP 모드
python -m domae_mcp --mcp

# Windows 자동시작 등록
python -m domae_mcp --install-startup
```

## 아키텍처

### 실행 모드
- `python -m domae_mcp` → FastAPI 웹서버 (localhost:5900)
- `python -m domae_mcp --mcp` → MCP stdio 서버

### 디렉토리
```
src/domae_mcp/
├── __main__.py         # CLI 진입점 (모드 분기)
├── server.py           # FastAPI 앱
├── mcp_server.py       # MCP 도구 정의
├── config.py           # ~/.maipharm-domae-mcp/ 설정 관리
├── database.py         # SQLAlchemy + SQLite
├── startup.py          # Windows 자동시작
├── crawlers/           # 8개 도매상 크롤러
├── models/             # SQLAlchemy 모델
├── services/           # 비즈니스 로직
├── routers/            # FastAPI 라우터
└── static/             # React 빌드 결과물
```

### 크롤러
domae-v2와 동일한 패턴. BaseCrawler 상속, login/search/order 구현.
계정 정보는 config.json에서 읽음 (DB 아닌 파일 기반, 암호화).

### 주의사항
- 티제이팜: login_p=2, referer 필수, ItemToken 캐싱
- 인천/복산 (NicePharm): 장바구니 읽기는 Bag.asp
- 백제: JWT Bearer, product_id는 ITEM_CD|ITEM_GB_CD
- HMP: DWR 프로토콜, 주문 미구현
- 피코/새로팜: 검색만 구현

## Git Commit
- 커밋 메시지는 항상 한글로 간략하게 작성한다 (1줄, 50자 이내 권장)

## 설계 문서
- `docs/ARCHITECTURE.md` — 시스템 아키텍처
- `docs/API_SPEC.md` — REST API + MCP Tools 명세
- `docs/DATABASE_SCHEMA.md` — DB 스키마
- `docs/PROJECT_STRUCTURE.md` — 프로젝트 구조 + domae-v2 매핑
