# 프로젝트 구조: maipharm-domae-mcp

## 디렉토리 구조

```
maipharm-domae-mcp/
│
├── README.md                    # 설치/사용 가이드
├── LICENSE
├── requirements.txt             # Python 의존성
├── .gitignore
│
├── docs/                        # 설계 문서
│   ├── ARCHITECTURE.md          # 아키텍처 설계
│   ├── API_SPEC.md              # REST API + MCP Tools 명세
│   ├── DATABASE_SCHEMA.md       # DB 스키마
│   └── PROJECT_STRUCTURE.md     # 이 파일
│
├── src/
│   └── domae_mcp/
│       ├── __init__.py          # __version__ = "1.0.0"
│       ├── __main__.py          # CLI 진입점
│       │                        #   (없음) → 웹서버 모드
│       │                        #   --mcp → MCP stdio 모드
│       │                        #   --install-startup → 자동시작 등록
│       │                        #   --uninstall-startup → 자동시작 해제
│       │
│       ├── server.py            # FastAPI 앱 + 정적 파일 서빙
│       ├── mcp_server.py        # MCP 도구 정의 (mcp SDK)
│       ├── config.py            # 설정 관리 (~/.maipharm-domae-mcp/)
│       ├── database.py          # SQLAlchemy + SQLite + 마이그레이션
│       ├── startup.py           # Windows 자동시작 등록/해제
│       │
│       ├── crawlers/            # 도매상 크롤러 (domae-v2에서 이식)
│       │   ├── __init__.py
│       │   ├── base.py          # BaseCrawler, SearchResult, OrderResult
│       │   ├── registry.py      # CrawlerRegistry
│       │   ├── geoweb.py        # 지오영
│       │   ├── boksan.py        # 복산
│       │   ├── inchun.py        # 인천
│       │   ├── tjpharm.py       # 티제이팜
│       │   ├── hmpmall.py       # HMP
│       │   ├── beakje.py        # 백제
│       │   ├── picomall.py      # 피코
│       │   └── saeropharm.py    # 새로팜
│       │
│       ├── models/              # SQLAlchemy 모델
│       │   ├── __init__.py
│       │   ├── product.py       # Product (모니터링 대상)
│       │   ├── order.py         # Order (주문 이력)
│       │   ├── urgent_order.py  # UrgentOrder, UrgentOrderSupplier, UrgentOrderLog
│       │   ├── inventory.py     # InventorySnapshot
│       │   └── schedule.py      # MonitorSchedule
│       │
│       ├── services/            # 비즈니스 로직
│       │   ├── __init__.py
│       │   ├── search_service.py    # 통합 검색 (ThreadPoolExecutor)
│       │   ├── order_service.py     # 주문 실행
│       │   ├── monitor_service.py   # 재고 모니터링 (백그라운드)
│       │   ├── telegram_service.py  # 텔레그램 알림
│       │   ├── mail_service.py      # SendGrid 메일 (신규도매 요청)
│       │   └── update_service.py    # GitHub 버전 체크
│       │
│       ├── routers/             # FastAPI 라우터
│       │   ├── __init__.py
│       │   ├── search.py        # GET /api/search
│       │   ├── order.py         # POST /api/orders, GET /api/orders
│       │   ├── urgent.py        # /api/urgent-orders CRUD
│       │   ├── products.py      # /api/products CRUD
│       │   ├── settings.py      # /api/settings/* (계정, 텔레그램, 스케줄)
│       │   ├── monitor.py       # /api/monitor (start/stop/status)
│       │   ├── supplier_request.py  # POST /api/supplier-request
│       │   └── update.py        # GET /api/update-check
│       │
│       └── static/              # React 빌드 결과물 (git에 포함)
│           ├── index.html
│           └── assets/
│               ├── index-xxx.js
│               └── index-xxx.css
│
├── frontend/                    # React 소스 (개발용)
│   ├── package.json
│   ├── vite.config.js
│   ├── index.html
│   └── src/
│       ├── App.jsx              # 라우팅 + 레이아웃
│       ├── api/
│       │   └── client.js        # axios (상대경로 /api)
│       └── pages/
│           ├── SearchPage.jsx       # 검색 + 주문
│           ├── UrgentPage.jsx       # 긴급주문
│           ├── HistoryPage.jsx      # 주문이력
│           ├── ProductsPage.jsx     # 모니터링 제품 관리
│           ├── SettingsPage.jsx     # 설정 (계정, 텔레그램, 스케줄)
│           └── SupplierRequestPage.jsx  # 신규도매 요청
│
└── scripts/
    └── build_frontend.sh        # npm build → static/ 복사
```

## domae-v2와의 파일 매핑

| domae-v2 | maipharm-domae-mcp | 변경사항 |
|----------|-------------------|---------|
| backend/main.py | src/domae_mcp/server.py | 인증 제거, 정적서빙 추가 |
| backend/config.py | src/domae_mcp/config.py | 환경변수 → config.json |
| backend/database.py | src/domae_mcp/database.py | credential 시딩 제거, 경로 변경 |
| backend/dependencies.py | (삭제) | 로컬 전용, 인증 불필요 |
| backend/crawlers/* | src/domae_mcp/crawlers/* | import 경로만 변경 |
| backend/models/* | src/domae_mcp/models/* | credential.py 삭제 |
| backend/routers/* | src/domae_mcp/routers/* | 인증 데코레이터 제거, 라우터 추가 |
| backend/services/* | src/domae_mcp/services/* | config 소스 변경, 서비스 추가 |
| frontend/src/* | frontend/src/* | LoginPage 삭제, 페이지 추가 |
| (없음) | src/domae_mcp/mcp_server.py | MCP 도구 신규 |
| (없음) | src/domae_mcp/startup.py | Windows 자동시작 신규 |
| (없음) | src/domae_mcp/__main__.py | CLI 진입점 신규 |

## 의존성 (requirements.txt)

```
# 웹서버
fastapi>=0.100.0
uvicorn>=0.23.0

# 크롤러
requests>=2.31.0
beautifulsoup4>=4.12.0
lxml>=4.9.0

# DB
sqlalchemy>=2.0.0

# MCP
mcp>=1.0.0

# 텔레그램
python-telegram-bot>=20.0

# 메일
sendgrid>=6.10.0

# 암호화
cryptography>=41.0.0

# 업데이트 체크 (requests로 충분)
```

## 데이터 저장 경로

```
~/.maipharm-domae-mcp/
├── config.json        # 설정 (도매 계정 암호화, 텔레그램 등)
└── data/
    └── domae.db       # SQLite 데이터베이스
```

Windows: `C:\Users\{사용자}\.maipharm-domae-mcp\`
Mac/Linux: `/Users/{사용자}/.maipharm-domae-mcp/`
