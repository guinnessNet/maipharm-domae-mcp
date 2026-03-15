# 구현 워크플로우: maipharm-domae-mcp

> 현재 상태: 설계 문서 완료, 코드 0%. 처음부터 구현 시작.
> 참고 문서: ARCHITECTURE.md, API_SPEC.md, DATABASE_SCHEMA.md, BUSINESS_ARCHITECTURE.md

---

## 전체 로드맵

```
Phase 0: 프로젝트 골격          ██░░░░░░░░  (0.5일)
Phase 1: 코어 엔진              ████░░░░░░  (3~4일)
Phase 2: 로컬 백엔드            ██████░░░░  (2~3일)
Phase 3: MCP 서버               ███████░░░  (1일)
Phase 4: 프론트엔드             ████████░░  (3~4일)
Phase 5: 통합/배포              █████████░  (1~2일)
────────────────────────────────────────────
합계:                                       약 2~3주 (로컬 버전 완성)

Phase 6: 클라우드 MVP           (이후 별도)
Phase 7: 프리미엄 기능          (이후 별도)
```

> Phase 0~5 = 로컬 설치형 (무료 버전) 완성
> Phase 6~7 = 클라우드형 (유료 버전) → 별도 저장소 (maipharm-domae-cloud)

---

## Phase 0: 프로젝트 골격 (0.5일)

> 목표: 코드가 실행 가능한 최소 골격 구성

### Step 0-1. 디렉토리 구조 생성

```
src/domae_mcp/
├── __init__.py              # __version__ = "1.0.0"
├── __main__.py              # CLI 진입점 (빈 골격)
├── core/
│   ├── __init__.py
│   ├── crawlers/
│   │   ├── __init__.py
│   │   └── base.py          # BaseCrawler, SearchResult, OrderResult
│   ├── services/
│   │   └── __init__.py
│   └── models/
│       └── __init__.py
├── local/
│   ├── __init__.py
│   ├── server.py            # FastAPI 앱 (빈 골격)
│   ├── config.py            # 설정 관리 (빈 골격)
│   └── database.py          # DB 초기화 (빈 골격)
└── static/                  # (빈 디렉토리, 프론트 빌드용)
```

### Step 0-2. __main__.py 진입점

```python
# 최소 동작 확인:
# python -m domae_mcp       → "서버 시작" 출력
# python -m domae_mcp --mcp → "MCP 모드" 출력
```

### Step 0-3. 동작 확인

```bash
pip install -e .
python -m domae_mcp --version   # → 1.0.0
python -m domae_mcp             # → FastAPI 서버 기동 (빈 페이지)
```

**체크포인트**: `python -m domae_mcp`로 localhost:5900에서 빈 FastAPI 서버 확인

---

## Phase 1: 코어 엔진 (3~4일)

> 목표: 크롤러 8개 + 핵심 서비스 구현 (domae-v2에서 이식)
> 의존성: Phase 0 완료

### Step 1-1. BaseCrawler & 데이터 모델 (0.5일)

**파일**: `core/crawlers/base.py`

```python
# 구현할 것:
# - BaseCrawler (ABC): login(), search(), order(), get_cart()
# - SearchResult (dataclass): maker, product_name, unit, insurance_code,
#                             quantity, price, supplier, product_id
# - OrderResult (dataclass): success, message, order_id
# - CrawlerError (exception)
```

**파일**: `core/crawlers/registry.py`

```python
# CrawlerRegistry:
# - register(name, crawler_class)
# - get(name) -> BaseCrawler
# - list_all() -> list[str]
# - 8개 크롤러 자동 등록
```

**체크포인트**: BaseCrawler 상속하여 DummyCrawler 작성, 테스트 통과

### Step 1-2. 크롤러 이식 — 지오영, 복산, 인천 (1일)

**참고**: domae-v2 코드에서 이식. BS4 + requests 사용.

| 파일 | 도매상 | 특이사항 |
|------|--------|----------|
| `core/crawlers/geoweb.py` | 지오영 | 기본 패턴 |
| `core/crawlers/boksan.py` | 복산 | NicePharm 패턴, Bag.asp |
| `core/crawlers/inchun.py` | 인천 | NicePharm 패턴, Bag.asp |

**체크포인트**: 각 크롤러 login() + search("아모잘탄") 동작 확인

### Step 1-3. 크롤러 이식 — 나머지 5개 (1일)

| 파일 | 도매상 | 특이사항 |
|------|--------|----------|
| `core/crawlers/tjpharm.py` | 티제이팜 | login_p=2, referer 필수, ItemToken 캐싱 |
| `core/crawlers/hmpmall.py` | HMP | DWR 프로토콜, 주문 미구현 |
| `core/crawlers/beakje.py` | 백제 | JWT Bearer, ITEM_CD\|ITEM_GB_CD |
| `core/crawlers/picomall.py` | 피코 | 검색만 구현 |
| `core/crawlers/saeropharm.py` | 새로팜 | 검색만 구현 |

**체크포인트**: CrawlerRegistry에 8개 등록, 전체 search 동작 확인

### Step 1-4. SQLAlchemy 모델 (0.5일)

**파일**: `core/models/`

| 파일 | 모델 | 주요 컬럼 |
|------|------|-----------|
| `product.py` | Product | id, name, description, created_at |
| `order.py` | Order | id, supplier, product_name, unit, quantity, price, success, message, is_urgent, ordered_at |
| `urgent_order.py` | UrgentOrder, UrgentOrderSupplier, UrgentOrderLog | (DATABASE_SCHEMA.md 참고) |
| `inventory.py` | InventorySnapshot | maker, product_name, unit, insurance_code, quantity, supplier, price, product_id, scanned_at |
| `schedule.py` | MonitorSchedule | start_hour, end_hour, interval_minutes |

**체크포인트**: `create_all()`로 SQLite에 테이블 생성 확인

### Step 1-5. 핵심 서비스 (1일)

**파일**: `core/services/`

| 파일 | 클래스 | 핵심 메서드 |
|------|--------|-------------|
| `search_service.py` | SearchService | `search(keyword, suppliers?)` → ThreadPoolExecutor로 8개 동시 검색, 결과 취합 |
| `order_service.py` | OrderService | `place_order(supplier, product_id, product_name, quantity)` → 크롤러 호출 + DB 저장 |
| `monitor_service.py` | MonitorService | `start()`, `stop()`, `run_cycle()` → 백그라운드 스레드, 스냅샷 비교, 변동 감지 |
| `telegram_service.py` | TelegramService | `send_message(text)`, `send_alert(product, changes)` |

**체크포인트**: SearchService.search("아모잘탄") → 8개 도매 결과 반환

---

## Phase 2: 로컬 백엔드 (2~3일)

> 목표: FastAPI 서버 + REST API 완성
> 의존성: Phase 1 완료

### Step 2-1. 설정 관리 (0.5일)

**파일**: `local/config.py`

```python
# ConfigManager:
# - 경로: ~/.maipharm-domae-mcp/config.json
# - Fernet 암호화 (.key 파일 자동 생성)
# - get_credentials(supplier) → {login_id, login_pw}
# - set_credentials(supplier, login_id, login_pw)
# - get_telegram() → {token, chat_id}
# - set_telegram(token, chat_id)
# - get_api_key() → str
# - set_api_key(key)
```

**파일**: `local/api_key.py`

```python
# ApiKeyManager:
# - verify(api_key) → 서버 검증 (https://api.domae.kr/api/verify)
# - is_valid_cached() → 오프라인 7일 유예
# - heartbeat(api_key, stats) → 12시간마다 통계 전송
```

**체크포인트**: config.json 생성, 계정 암호화/복호화 확인

### Step 2-2. 데이터베이스 초기화 (0.5일)

**파일**: `local/database.py`

```python
# - SQLite (WAL 모드)
# - 경로: ~/.maipharm-domae-mcp/data/domae.db
# - create_engine + SessionLocal
# - init_db(): create_all + 기본 스케줄 시딩
#   (00-08: 120분, 08-22: 60분, 22-24: 120분)
# - get_db(): FastAPI Depends용 세션 제너레이터
```

### Step 2-3. FastAPI 서버 + 라우터 (1~1.5일)

**파일**: `local/server.py`

```python
# - FastAPI 앱 생성
# - 127.0.0.1:5900 바인딩
# - CORS 불필요 (동일 origin)
# - 시작 시 API 키 검증
# - 정적 파일 서빙 (static/)
# - 라우터 마운트
```

**파일**: `local/routers/` — API_SPEC.md 기준으로 구현

| 파일 | 엔드포인트 | 우선순위 |
|------|------------|----------|
| `search.py` | `GET /api/search` | 1순위 |
| `order.py` | `POST /api/orders`, `GET /api/orders` | 1순위 |
| `urgent.py` | `/api/urgent-orders` CRUD | 2순위 |
| `products.py` | `/api/products` CRUD | 2순위 |
| `settings.py` | `/api/settings/*` (계정, 텔레그램, 스케줄) | 1순위 |
| `monitor.py` | `/api/monitor` (start/stop/status) | 2순위 |
| `supplier_request.py` | `POST /api/supplier-request` | 3순위 |
| `update.py` | `GET /api/update-check` | 3순위 |

구현 순서:
```
1순위: search → settings → order   (핵심 동작 확인)
2순위: products → monitor → urgent  (모니터링 기능)
3순위: supplier_request → update    (부가 기능)
```

**체크포인트**: curl로 `/api/search?keyword=아모잘탄` 호출 → JSON 응답

### Step 2-4. 스케줄러 + 모니터링 통합 (0.5일)

**파일**: `local/scheduler.py`

```python
# LocalScheduler:
# - 최소 간격 60분 하드코딩
# - MonitorService 래핑
# - 시간대별 간격 적용 (monitor_schedules 참조)
# - 백그라운드 스레드로 실행
# - 서버 시작 시 자동 시작 (설정되어 있으면)
```

### Step 2-5. Windows 자동시작 (0.5일)

**파일**: `local/startup.py`

```python
# --install-startup: VBS 스크립트 생성
# --uninstall-startup: VBS 스크립트 삭제
# Mac/Linux: 안내 메시지만 출력
```

### Step 2-6. __main__.py 완성

```python
# 모드 분기:
# (없음)              → run_web_server()
# --mcp               → run_mcp_server()
# --install-startup   → install_startup()
# --uninstall-startup → uninstall_startup()
# --port PORT         → run_web_server(port)
# --version           → print version
```

**체크포인트**: `python -m domae_mcp` → 전체 API 동작 확인

---

## Phase 3: MCP 서버 (1일)

> 목표: Claude Desktop에서 12개 도구 사용 가능
> 의존성: Phase 1 완료 (Phase 2와 병렬 가능)

### Step 3-1. MCP 서버 구현

**파일**: `local/mcp_server.py`

```python
# mcp SDK 사용, stdio transport
# 12개 도구 정의 (API_SPEC.md의 MCP Tools 섹션 그대로):
#
# 1. search_inventory     → SearchService.search()
# 2. place_order          → OrderService.place_order()
# 3. create_urgent_order  → DB 저장
# 4. list_urgent_orders   → DB 조회
# 5. cancel_urgent_order  → DB 업데이트
# 6. get_order_history    → DB 조회
# 7. start_monitoring     → MonitorService.start()
# 8. stop_monitoring      → MonitorService.stop()
# 9. get_monitoring_status → MonitorService.status()
# 10. add_monitoring_product → DB 저장
# 11. remove_monitoring_product → DB 삭제
# 12. test_credential     → Crawler.login() 테스트
```

### Step 3-2. Claude Desktop 설정 안내

```json
// claude_desktop_config.json에 추가
{
  "mcpServers": {
    "domae": {
      "command": "python",
      "args": ["-m", "domae_mcp", "--mcp"]
    }
  }
}
```

**체크포인트**: Claude Desktop에서 "아모잘탄 검색해줘" → 도구 호출 → 결과 반환

---

## Phase 4: 프론트엔드 (3~4일)

> 목표: React 웹 UI 완성
> 의존성: Phase 2 API 1순위 완료 후 시작 가능

### Step 4-1. 프로젝트 셋업 (0.5일)

```bash
cd frontend
npm create vite@latest . -- --template react
npm install axios
```

```
frontend/
├── package.json
├── vite.config.js          # proxy: /api → localhost:5900
├── index.html
└── src/
    ├── main.jsx
    ├── App.jsx              # 라우팅 + 레이아웃
    ├── api/
    │   └── client.js        # axios, baseURL: "/api"
    ├── components/          # 공통 컴포넌트
    │   ├── Layout.jsx
    │   ├── Header.jsx
    │   └── UpdateBanner.jsx
    └── pages/
```

### Step 4-2. 검색 페이지 (1일) — 가장 중요

**파일**: `pages/SearchPage.jsx`

```
┌─────────────────────────────────────────┐
│  검색: [__아모잘탄________________] [검색] │
│                                          │
│  ┌────────────────────────────────────┐  │
│  │ 제품명        | 단위 | 도매  | 재고 | 가격   | 주문 │
│  │ 아모잘탄 5/100 | 30T  | 지오영 | 25  | 12,500 | [5▼][주문] │
│  │ 아모잘탄 5/100 | 30T  | 복산   | 10  | 12,300 | [5▼][주문] │
│  │ ...                                        │
│  └────────────────────────────────────┘  │
└─────────────────────────────────────────┘
```

기능:
- 키워드 입력 → GET /api/search → 결과 테이블 렌더링
- 도매상별 가격 정렬, 재고 표시
- 주문 버튼 → POST /api/orders → 결과 알림

### Step 4-3. 설정 페이지 (1일) — 초기 설정에 필수

**파일**: `pages/SettingsPage.jsx`

```
┌──────────────────────────────────────────┐
│  설정                                      │
│                                            │
│  [API 키]  [도매 계정]  [텔레그램]  [스케줄] │
│                                            │
│  ── API 키 ──                              │
│  ┌────────────────────────────────┐        │
│  │ dmk_free_xxxx                  │ [검증]  │
│  └────────────────────────────────┘        │
│  API 키가 없으신가요? [팜스퀘어에서 발급]    │
│                                            │
│  ── 도매 계정 ──                            │
│  ┌──────────┬──────────┬──────────┐        │
│  │ 지오영    │ ID: ___  │ PW: ___ │ [테스트]│
│  │ 복산     │ ID: ___  │ PW: ___ │ [테스트]│
│  │ ...                                     │
│  └──────────┴──────────┴──────────┘        │
│                                            │
│  ── 텔레그램 ──                              │
│  봇 토큰: [______________]                  │
│  Chat ID: [______________] [테스트 발송]    │
└──────────────────────────────────────────┘
```

### Step 4-4. 나머지 페이지 (1~1.5일)

| 파일 | 페이지 | 설명 |
|------|--------|------|
| `UrgentPage.jsx` | 긴급주문 | 등록/목록/취소/재활성화 |
| `HistoryPage.jsx` | 주문이력 | 테이블 + 페이지네이션 |
| `ProductsPage.jsx` | 모니터링 제품 | 추가/삭제 + 모니터링 시작/중지 |
| `SupplierRequestPage.jsx` | 신규도매 요청 | 폼 + 동의 체크 + 메일 발송 |

### Step 4-5. 빌드 & 정적 파일 배치 (0.5일)

```bash
# frontend/
npm run build
# → dist/ 생성

# 빌드 결과물을 src/domae_mcp/static/에 복사
cp -r dist/* ../src/domae_mcp/static/
```

**파일**: `scripts/build_frontend.sh`

```bash
#!/bin/bash
cd frontend && npm run build
rm -rf ../src/domae_mcp/static/*
cp -r dist/* ../src/domae_mcp/static/
```

**체크포인트**: `python -m domae_mcp` → localhost:5900에서 React UI 동작

---

## Phase 5: 통합 & 배포 준비 (1~2일)

> 목표: 로컬 설치형 완성, 배포 가능 상태
> 의존성: Phase 2, 3, 4 모두 완료

### Step 5-1. 통합 테스트

```
테스트 시나리오:
1. 최초 설치 → API 키 입력 → 계정 설정 → 검색 → 주문
2. 모니터링 등록 → 60분 후 재검색 → 변동 감지 → 텔레그램 알림
3. 긴급주문 등록 → 재고 감지 시 자동 주문
4. MCP 모드: Claude Desktop에서 검색/주문
5. Windows 자동시작 등록 → 리부팅 → 자동 실행 확인
```

### Step 5-2. BSL 라이선스 적용

**파일**: `LICENSE` (BSL 1.1 텍스트)

### Step 5-3. README.md 업데이트

```
설치 가이드, 사용법, 스크린샷, 도매상 지원 현황, FAQ
```

### Step 5-4. .gitignore 검증

```
~/.maipharm-domae-mcp/      # 사용자 데이터 (커밋 방지)
*.pyc
__pycache__
node_modules/
frontend/dist/
.env
```

### Step 5-5. GitHub 릴리스

```bash
git tag v1.0.0
git push origin main --tags
# GitHub Releases에 설치 가이드 작성
```

**체크포인트**: 새 PC에서 README 따라 설치 → 전체 동작 확인

---

## Phase 6: 클라우드 MVP (별도 저장소, 2~3주)

> 저장소: maipharm-domae-cloud (비공개)
> 의존성: Phase 5 완료 (로컬 버전이 pip 패키지로 배포된 후)

### Step 6-1. 프로젝트 셋업

```bash
mkdir maipharm-domae-cloud && cd maipharm-domae-cloud
pip install maipharm-domae-mcp   # 공개 코어 패키지 의존
```

### Step 6-2. 팜스퀘어 JWT 검증

```
cloud/auth/pharmsquare.py    — JWT 검증
cloud/auth/dependencies.py   — FastAPI 의존성
```

### Step 6-3. PostgreSQL + 멀티 테넌트

```
cloud/database.py            — PostgreSQL 연결
cloud/models/domae_user.py   — 사용자 캐시 테이블
cloud/models/user_credential.py — 도매 계정 (AES-256)
+ 기존 테이블에 user_id FK 추가
```

### Step 6-4. Celery 스케줄러

```
cloud/scheduler.py           — Celery Beat
  베이직: 30분 주기
  프로: 5분 주기
```

### Step 6-5. 클라우드 라우터

```
cloud/routers/               — 코어 호출 래퍼 + JWT 인증
```

### Step 6-6. 프론트엔드 (팜스퀘어 연결)

```
frontend-cloud/              — 로컬 페이지 재사용 + JWT 헤더 + 팜스퀘어 네비
```

### Step 6-7. Docker + 배포

```
docker-compose.yml           — API + Worker + Beat + Redis + PostgreSQL
Dockerfile
Nginx 설정 (domae.pharmsquare.com)
```

---

## Phase 7: 프리미엄 기능 (1~2주)

### Step 7-1. 카카오 알림톡

```
cloud/notifications/kakao_service.py
- 비즈니스 채널 등록
- 템플릿 심사 (가격변동, 주문완료, 재고감지)
- 알림톡 발송 API 연동
```

### Step 7-2. 프로 대시보드

```
cloud/routers/dashboard.py   — 통계, 가격 추이
frontend-cloud/pages/DashboardPage.jsx
```

### Step 7-3. 팜스퀘어 요금제 동기화

```
cloud/routers/webhook.py     — 요금제 변경 웹훅
cloud/scheduler.py           — 폴링 백업 (매 시간)
```

---

## 의존성 맵

```
Phase 0 ──▶ Phase 1 ──▶ Phase 2 ──▶ Phase 5
                │           │
                │           └──▶ Phase 4 ──▶ Phase 5
                │
                └──▶ Phase 3 ──▶ Phase 5
                                    │
                                    ▼
                              Phase 6 ──▶ Phase 7
```

- Phase 1 완료 후 → Phase 2, 3 **병렬 진행 가능**
- Phase 2 1순위 API 완료 후 → Phase 4 **병렬 진행 가능**
- Phase 2, 3, 4 모두 완료 → Phase 5 통합
- Phase 5 완료 후 → Phase 6 (별도 저장소)

---

## 병렬 작업 최적화 시나리오

```
Day 1:     Phase 0 (골격)
Day 2~3:   Phase 1 (크롤러 + 모델)
Day 4~5:   Phase 2 (백엔드 API)  ║  Phase 3 (MCP 서버)
Day 6~8:   Phase 2 나머지        ║  Phase 4 (프론트엔드)
Day 9~10:  Phase 5 (통합 + 배포)
────────────────────────────────────────
총 ~10일 (2주)로 로컬 버전 완성 가능
```

---

## 각 Phase 완료 기준

| Phase | 완료 기준 |
|-------|----------|
| 0 | `python -m domae_mcp`로 빈 FastAPI 서버 기동 |
| 1 | SearchService.search("아모잘탄")이 8개 도매 결과 반환 |
| 2 | curl로 전체 REST API 동작 확인 |
| 3 | Claude Desktop에서 search_inventory 도구 호출 성공 |
| 4 | 브라우저에서 검색→주문 전체 플로우 동작 |
| 5 | 새 PC에서 README 따라 설치→동작 확인, GitHub 릴리스 |
| 6 | 팜스퀘어 로그인 → 클라우드 검색/주문 동작 |
| 7 | 카카오 알림 수신, 대시보드 통계 확인 |

---

## 위험 요소 & 대응

| 위험 | 확률 | 대응 |
|------|------|------|
| domae-v2 크롤러 코드 이식 시 도매 사이트 변경 | 중 | 이식 전 각 사이트 로그인/검색 수동 확인 |
| 특정 도매 계정 없어서 테스트 불가 | 고 | 계정 있는 도매부터 구현, 없는 건 스켈레톤만 |
| MCP SDK 버전 호환 | 저 | mcp>=1.0.0 고정, 공식 예제 참고 |
| 프론트엔드 디자인 품질 | 중 | 기능 우선, 디자인은 최소 CSS로 시작 |
| API 키 검증 서버 미구축 | 고 | Phase 5까지는 검증 건너뛰기 옵션 제공 (--no-verify) |
