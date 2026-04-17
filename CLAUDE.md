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
- 로컬 전용 (127.0.0.1 바인딩), 팜스퀘어 API 키 인증 필요 (무료 가입).
- Windows 우선, Mac/Linux도 동작 가능하도록.

## 크롤러 배포 아키텍처 (중요)

이 레포는 **공개 GitHub** 레포이다. 크롤러 코드는 비공개로 서버에서 배포한다.

### 구조
```
이 레포 (공개):  프레임워크 (BaseCrawler, Registry, Loader, Services, UI, MCP)
팜스퀘어 서버 (비공개):  크롤러 원본 코드 13개 → DB에 저장 → API로 배포
```

### 크롤러 코드 관리 규칙
- 크롤러 .py 파일(geoweb.py, boksan.py 등)은 **이 레포에서 개발/테스트**한다.
- 단, 이 파일들은 `.gitignore`에 등록되어 **커밋되지 않는다**.
- 크롤러 개발 완료 후 `pharmsquare-server-main/prisma/seeds/domae-crawlers/`로 복사하여 배포한다.
- `base.py` (BaseCrawler, SearchResult, OrderResult)는 공개 코드이며 이 레포에 커밋한다.
- `loader.py` (CrawlerLoader)는 공개 코드이며 이 레포에 커밋한다.
- `registry.py` (CrawlerRegistry)는 공개 코드이며 이 레포에 커밋한다.

### 크롤러 개발 → 배포 흐름
```
1. 이 레포에서 크롤러 .py 작성/수정 (src/domae_mcp/core/crawlers/geoweb.py)
2. 로컬에서 테스트 (python -m tests.test_crawlers 지오영)
3. pharmsquare-server-main/prisma/seeds/domae-crawlers/에 복사
4. 시드 스크립트 실행 → DB 반영
5. 사용자에게 자동 배포 (다음 버전 체크 시)
```

### 절대 하지 말 것
- **크롤러 .py 파일을 이 레포에 커밋하지 마라** (.gitignore에 등록됨)
- 크롤러 코드에 사용자 크리덴셜을 하드코딩하지 마라
- 서명 개인키를 코드에 포함하지 마라 (공개키만 loader.py에 포함)

### 커밋 가능한 크롤러 관련 파일
```
src/domae_mcp/core/crawlers/
├── __init__.py      ✅ 커밋
├── base.py          ✅ 커밋 (BaseCrawler, SearchResult, OrderResult)
├── registry.py      ✅ 커밋 (CrawlerRegistry)
├── loader.py        ✅ 커밋 (CrawlerLoader - 서버 다운로드/캐시/서명검증)
├── geoweb.py        ❌ gitignore (개발용, 커밋 금지)
├── boksan.py        ❌ gitignore (개발용, 커밋 금지)
└── ...              ❌ gitignore (개발용, 커밋 금지)
```

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
├── core/
│   ├── crawlers/
│   │   ├── base.py         # BaseCrawler 추상 클래스 (공개)
│   │   ├── registry.py     # CrawlerRegistry (공개)
│   │   ├── loader.py       # CrawlerLoader - 서버 배포 (공개)
│   │   └── *.py            # 크롤러 원본 (gitignore, 개발용)
│   ├── models/             # SQLAlchemy 모델
│   └── services/           # SearchService, OrderService 등
├── local/
│   ├── server.py           # FastAPI 앱
│   ├── mcp_server.py       # MCP 도구 정의
│   ├── config.py           # ~/.maipharm-domae-mcp/ 설정 관리
│   ├── database.py         # SQLAlchemy + SQLite
│   ├── scheduler.py        # 로컬 스케줄러 (60분 최소)
│   ├── startup.py          # Windows 자동시작
│   └── routers/            # FastAPI 라우터
├── static/                 # React 빌드 결과물
└── frontend/               # React 소스
```

### 크롤러 로드 흐름
```
앱 시작 → CrawlerLoader.load()
         → 서버에서 서명된 번들 다운로드 (API 키 필수)
         → Ed25519 서명 검증
         → ~/.maipharm-domae-mcp/crawlers/에 캐시
         → 동적 import → CrawlerRegistry에 등록
         → SearchService/OrderService가 Registry에서 가져다 씀
```
API 키 없으면 크롤러 0개 → 검색/주문 불가.
오프라인 시 캐시된 번들로 7일간 동작.

### 크롤러 작성 규칙
- domae-v2와 동일한 패턴. BaseCrawler 상속, login/search/order 구현.
- 계정 정보는 config.json에서 읽음 (DB 아닌 파일 기반, Fernet 암호화).
- 크롤러 코드 첫 줄은 반드시 `from domae_mcp.core.crawlers.base import BaseCrawler, SearchResult, OrderResult`
- **상대 import 사용 금지** — 캐시 디렉토리에서 동적 로드되므로 절대 import만 가능
- requirements.txt에 포함된 패키지만 사용 가능 (새 의존성 추가 시 requirements.txt 먼저 업데이트)

### 도매상별 주의사항
- 티제이팜: login_p=2, referer 필수, ItemToken 캐싱
- 인천/복산 (NicePharm): 장바구니 읽기는 Bag.asp
- 백제: JWT Bearer, product_id는 ITEM_CD|ITEM_GB_CD
- HMP: DWR 프로토콜, 주문 미구현
- 피코/새로팜: 검색만 구현
- 도현팜 (NicePharm): 인천/복산과 동일 패턴, vendor_code 로그인 시 자동 추출
- 삼성팜: PHP 기반, 검색은 iframe(sc_item_list_iframe.php), 주문은 order_temp 방식(주문대기→주문하기), product_id는 "item_code|supplier_code" 형태, 로그인 시 order.php 방문 필요
- 경동사(ndrug): 별도 패턴

## Git Commit
- 커밋 메시지는 항상 한글로 간략하게 작성한다 (1줄, 50자 이내 권장)

## 릴리즈

### 버전 동기화 규칙 (중요)
릴리즈 전 반드시 **두 파일의 버전을 함께** 수정해야 한다:
- `src/domae_mcp/__init__.py` — `__version__ = "X.Y.Z"`
- `pyproject.toml` — `version = "X.Y.Z"`

`__init__.py`의 `__version__`은 `api_key.py` / `desktop/updater.py` / `__main__.py --version`에서 참조된다.
mismatch 시 exe가 스스로를 구버전으로 인식 → auto-updater 무한 루프.

### 릴리즈 절차
1. 두 파일 버전 수정 → 한글 커밋 (예: `v1.3.1 — 버전 동기화`)
2. 브랜치 push → `git tag vX.Y.Z` → `git push origin vX.Y.Z`
3. `.github/workflows/release.yml`이 자동 실행:
   - 프론트 빌드 → `src/domae_mcp/static/`에 복사
   - PyInstaller (`domae.spec`)로 `MaipharmDomae.exe` 빌드 (windows-latest)
   - sdist/wheel 빌드 (ubuntu-latest)
   - `MaipharmDomae.exe` + `maipharm-domae-mcp-vX.Y.Z.zip` + `*.whl`을 GitHub Release에 업로드

### 절대 하지 말 것
- **이미 배포된 태그를 force-push로 교체하지 마라** — 해당 태그로 이미 업데이트된 사용자 exe의 updater가 꼬인다. 수정이 필요하면 patch release (예: v1.3.0 버그 → v1.3.1)
- `__init__.py`와 `pyproject.toml` 중 한쪽만 올리지 마라

### 로컬 exe 빌드 (개발/디버깅용)
```bash
pip install pyinstaller
cd frontend && npm run build && cp -r dist/* ../src/domae_mcp/static/
cd .. && python -m PyInstaller domae.spec --clean --noconfirm
# → dist/MaipharmDomae.exe (약 54MB)
```

## 설계 문서
- `docs/ARCHITECTURE.md` — 시스템 아키텍처 (로컬 모드)
- `docs/API_SPEC.md` — REST API + MCP Tools 명세
- `docs/DATABASE_SCHEMA.md` — DB 스키마
- `docs/USER_GUIDE.md` — 사용자 가이드 (약국 대상, 비개발자용)
- `docs/TELEGRAM_REDESIGN.md` — 텔레그램 알림 설계
