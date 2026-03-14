# 아키텍처 설계: maipharm-domae-mcp

## 1. 시스템 개요

로컬 설치형 의약품 도매 통합 검색/주문 도구.
사용자 PC에서 로컬 웹서버 + MCP 서버로 동작.

```
┌─────────────────────────────────────────────────────┐
│                   사용자 PC (Windows)                  │
│                                                       │
│  ┌──────────┐    ┌──────────────────────────────┐    │
│  │ 브라우저  │───▶│  FastAPI 웹서버 (localhost)    │    │
│  │          │◀───│  - 정적 파일 서빙 (React 빌드) │    │
│  └──────────┘    │  - REST API                   │    │
│                  └──────────┬───────────────────┘    │
│                             │                         │
│  ┌──────────┐    ┌──────────▼───────────────────┐    │
│  │ Claude   │───▶│  MCP 서버 (stdio)             │    │
│  │ Desktop  │◀───│  - 9개 Tools                  │    │
│  └──────────┘    └──────────┬───────────────────┘    │
│                             │                         │
│                  ┌──────────▼───────────────────┐    │
│                  │  코어 엔진                     │    │
│                  │  ├─ CrawlerRegistry           │    │
│                  │  ├─ SearchService              │    │
│                  │  ├─ OrderService               │    │
│                  │  ├─ MonitorService             │    │
│                  │  ├─ TelegramService            │    │
│                  │  └─ MailService (SendGrid)     │    │
│                  └──────────┬───────────────────┘    │
│                             │                         │
│                  ┌──────────▼───────────────────┐    │
│                  │  SQLite (~/.maipharm-domae-mcp/) │  │
│                  └─────────────────────────────────┘  │
└─────────────────────────────────────────────────────┘
                              │
              ┌───────────────┼───────────────────┐
              ▼               ▼                   ▼
        ┌──────────┐  ┌──────────┐        ┌──────────┐
        │ 지오영    │  │ 복산     │  ...   │ 새로팜    │
        │ 웹사이트  │  │ 웹사이트  │        │ 웹사이트   │
        └──────────┘  └──────────┘        └──────────┘
```

## 2. 실행 모드

### 모드 A: 웹서버 (기본)
```bash
python -m domae_mcp
# → FastAPI 서버 기동 (127.0.0.1:5900)
# → 모니터링 자동시작
# → 브라우저에서 http://localhost:5900 접속
```

### 모드 B: MCP (Claude Desktop 연동)
```bash
python -m domae_mcp --mcp
# → stdio MCP 프로토콜 모드
# → Claude Desktop에서 tool 호출 시 응답
```

두 모드 모두 같은 코어 엔진과 DB를 공유.
웹서버 모드에서 MCP를 동시에 사용하려면 별도 프로세스로 --mcp 실행.

## 3. 컴포넌트 설계

### 3.1 진입점 (__main__.py)

```
python -m domae_mcp [옵션]

옵션:
  (없음)              웹서버 모드 (기본)
  --mcp               MCP stdio 모드
  --install-startup   Windows 시작프로그램 등록
  --uninstall-startup Windows 시작프로그램 해제
  --port PORT         웹서버 포트 (기본: 5900)
  --version           버전 출력
```

### 3.2 FastAPI 웹서버 (server.py)

기존 domae-v2의 main.py 기반. 변경사항:
- CORS 불필요 (같은 origin)
- 인증 미들웨어 제거 (로컬 전용, 127.0.0.1 바인딩)
- React 빌드 결과물 정적 서빙 추가
- 업데이트 체크 API 추가

### 3.3 MCP 서버 (mcp_server.py)

Python `mcp` SDK 사용. stdio transport.
코어 서비스를 MCP tool로 래핑.

### 3.4 크롤러 (crawlers/)

domae-v2에서 그대로 이식. 변경사항:
- import 경로만 조정
- credentials를 DB 대신 로컬 config에서 읽도록 변경

### 3.5 서비스 (services/)

domae-v2에서 이식. 변경사항:
- TelegramService: 환경변수 → 로컬 config
- MailService: 신규 추가 (SendGrid API)
- MonitorService: 로직 동일, config 소스만 변경

### 3.6 설정 관리 (config.py)

```
~/.maipharm-domae-mcp/
├── config.json        # 도매 계정, 텔레그램, 설정
└── data/
    └── domae.db       # SQLite 데이터베이스
```

config.json 구조:
```json
{
  "version": "1.0.0",
  "port": 5900,
  "credentials": {
    "지오영": { "login_id": "...", "login_pw": "..." },
    "복산":   { "login_id": "...", "login_pw": "..." }
  },
  "telegram": {
    "token": "",
    "chat_id": ""
  }
}
```

- 비밀번호는 `cryptography.Fernet`으로 암호화 저장
- 키는 머신별 고유값에서 파생 (uuid.getnode 등)

## 4. 데이터 흐름

### 4.1 검색 흐름
```
사용자 → [검색 키워드] → SearchService
  → ThreadPoolExecutor (max_workers=8)
    → Crawler.login() + Crawler.search()  ×8개 도매
  → 결과 취합 → 응답
```

### 4.2 주문 흐름
```
사용자 → [도매, product_id, 수량] → OrderService
  → Crawler.login() → Crawler.order()
  → 결과 → DB 저장 → 텔레그램 알림
```

### 4.3 모니터링 흐름
```
MonitorService (백그라운드 스레드)
  → 주기적으로 등록 제품 검색
  → 이전 스냅샷과 비교
  → 변동 감지 시 텔레그램 알림
  → 긴급주문 조건 충족 시 자동 주문
```

### 4.4 신규도매 요청 흐름
```
사용자 → [도매상명, URL, 계정, 동의체크] → SupplierRequestRouter
  → MailService (SendGrid API)
  → kjh@maipharm.com 으로 전송
```

## 5. Windows 자동시작

`--install-startup` 실행 시:
1. `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\` 경로에 VBS 스크립트 생성
2. VBS가 `pythonw -m domae_mcp`를 콘솔 창 없이 실행
3. PC 부팅 시 자동으로 백그라운드 실행

```vbs
' maipharm-domae-mcp.vbs
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run "pythonw -m domae_mcp", 0, False
```

`--uninstall-startup`으로 제거 가능.

## 6. 자동 업데이트 체크

- 서버 시작 시 GitHub Releases API 호출:
  `GET https://api.github.com/repos/{owner}/maipharm-domae-mcp/releases/latest`
- 응답의 `tag_name`과 현재 `__version__` 비교
- 새 버전 있으면 `/api/update-check` 응답에 포함
- 프론트엔드가 페이지 로드 시 호출 → 상단 배너 표시
- 네트워크 에러 시 무시 (오프라인 동작 가능)

## 7. 보안 고려사항

| 항목 | 대응 |
|------|------|
| 계정 정보 유출 | 로컬 암호화 저장, 네트워크 전송 없음 |
| 외부 접근 | 127.0.0.1만 바인딩, 외부 접속 불가 |
| 신규도매 메일 | SendGrid API 키는 서버사이드, HTTPS 전송 |
| 크롤러 세션 | 메모리에서만 유지, 디스크 저장 안 함 |
| SQLite | 로컬 파일, 사용자 PC 권한으로만 접근 |
