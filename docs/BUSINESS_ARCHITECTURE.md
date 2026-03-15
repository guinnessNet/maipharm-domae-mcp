# 비즈니스 아키텍처: maipharm-domae-mcp

## 1. 설계 철학

**"기능은 모두에게, 편의는 유료로"**

- 모든 핵심 기능(검색, 주문, 자동주문, 모니터링, 알림)은 **무료로 제공**
- 유료 사용자에게는 **편의성과 안정성**을 제공 (24시간 운영, 짧은 간격, 카톡 알림)
- 돈을 버는 것보다 **많은 약국에 퍼뜨리는 것**이 우선 목표

---

## 2. 제공 형태 비교

```
┌──────────────────────────────────────────────────────────────────────┐
│                        제공 형태 2가지                                │
│                                                                      │
│  ┌─────────────────────────────┐  ┌──────────────────────────────┐  │
│  │  A. 로컬 설치형 (무료)       │  │  B. 클라우드형 (유료)         │  │
│  │                              │  │                               │  │
│  │  • git clone + pip install  │  │  • 팜스퀘어 유료회원이면      │  │
│  │  • 내 PC에서 직접 실행       │  │    자동으로 이용 가능         │  │
│  │  • PC 켜져있을 때만 동작     │  │  • 클라우드 서버가 대신 실행   │  │
│  │  • 모든 기능 사용 가능       │  │  • 24시간 365일 운영          │  │
│  │  • 모니터링 간격 60분        │  │  • 모니터링 간격 5~30분       │  │
│  │  • 텔레그램 알림             │  │  • 카카오톡 + 텔레그램 알림   │  │
│  │  • 검색 이력 로컬 보관       │  │  • 검색 이력 클라우드 보관    │  │
│  │                              │  │                               │  │
│  │  설치: GitHub에서 다운로드   │  │  진입: 팜스퀘어 메뉴에서      │  │
│  │  인증: 팜스퀘어 무료가입     │  │        바로 접속              │  │
│  │        → API 키 발급         │  │  인증: 팜스퀘어 SSO           │  │
│  │  결제: 없음                  │  │  결제: 팜스퀘어 구독에 포함   │  │
│  └─────────────────────────────┘  └──────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 3. 로컬 API 키 인증 체계

### 3.1 왜 로컬도 API 키가 필요한가

| 이유 | 설명 |
|------|------|
| **사용자 식별** | 누가 사용하는지 알아야 전환 퍼널 구축 가능 |
| **보안** | 서버 리소스(업데이트 체크, 크롤러 패치 등)에 아무나 접근 불가 |
| **남용 방지** | API 키 없이 크롤러 코드만 빼가서 대량 크롤링하는 것 차단 |
| **전환 유도** | 무료 가입 → 사용 → 자연스럽게 유료 전환 |
| **통계** | 활성 사용자 수, 검색 빈도, 인기 도매상 등 파악 |

### 3.2 로컬 사용자의 API 키 발급 흐름

```
┌──────────┐     ┌──────────────────┐     ┌────────────────────┐
│  사용자   │     │   팜스퀘어        │     │  domae API 서버     │
│  (로컬)  │     │   (회원가입)      │     │  (api.domae.kr)    │
└────┬─────┘     └────────┬─────────┘     └─────────┬──────────┘
     │                     │                         │
     │  1. 팜스퀘어 무료 가입                         │
     │    (약국명, 이메일, 연락처)                     │
     │─────────────────────▶                         │
     │                     │                         │
     │  2. "도매 API 키 발급" 메뉴 클릭               │
     │─────────────────────▶                         │
     │                     │  3. API 키 생성 요청     │
     │                     │────────────────────────▶│
     │                     │  4. API 키 반환          │
     │                     │◀────────────────────────│
     │                     │     dmk_free_a1b2c3...  │
     │  5. API 키 표시      │                         │
     │◀─────────────────────                         │
     │                     │                         │
     │  6. 로컬 설치 후 설정 화면에서 API 키 입력       │
     │     config.json에 저장                         │
     │                     │                         │
     │  7. 이후 로컬 앱 시작 시 API 키 검증            │
     │───────────────────────────────────────────────▶│
     │     GET /api/verify?key=dmk_free_a1b2c3...    │
     │◀───────────────────────────────────────────────│
     │     { valid: true, tier: "free",               │
     │       pharmacy: "마이약국" }                    │
```

### 3.3 API 키 구조

```
키 형식: dmk_{tier}_{random_32chars}

예시:
  dmk_free_a1b2c3d4e5f6g7h8i9j0k1l2    ← 무료 (로컬)
  dmk_basic_m3n4o5p6q7r8s9t0u1v2w3x4   ← 베이직 (클라우드)
  dmk_pro_y5z6a7b8c9d0e1f2g3h4i5j6     ← 프로 (클라우드)
```

### 3.4 API 키로 제어되는 것들

```python
# src/domae_mcp/local/api_key.py (공개 코드)

class ApiKeyManager:
    """API 키 검증 및 기능 제어"""

    VERIFY_URL = "https://api.domae.kr/api/verify"

    async def verify(self, api_key: str) -> dict:
        """앱 시작 시 API 키 검증 (1회)"""
        resp = await httpx.get(
            self.VERIFY_URL,
            params={"key": api_key},
            timeout=5.0,
        )
        if resp.status_code != 200:
            raise ValueError("유효하지 않은 API 키입니다. 팜스퀘어에서 발급받으세요.")
        return resp.json()
        # {
        #   "valid": true,
        #   "tier": "free",
        #   "pharmacy_name": "마이약국",
        #   "features": {
        #     "min_interval": 60,
        #     "max_crawlers": 8,
        #     "telegram": true,
        #     "kakao": false
        #   }
        # }

    async def heartbeat(self, api_key: str, stats: dict):
        """주기적 하트비트 (12시간마다) — 사용 통계 수집"""
        await httpx.post(
            "https://api.domae.kr/api/heartbeat",
            json={
                "key": api_key,
                "version": __version__,
                "search_count": stats.get("search_count", 0),
                "order_count": stats.get("order_count", 0),
                "active_monitors": stats.get("active_monitors", 0),
            },
            timeout=5.0,
        )
        # 실패해도 무시 (오프라인 동작 가능)
```

### 3.5 로컬 앱 최초 실행 흐름

```
┌────────────────────────────────────────────────────┐
│  첫 실행 → localhost:5900 접속                       │
│                                                      │
│  ┌────────────────────────────────────────────────┐ │
│  │                                                │ │
│  │   마이팜 도매 통합검색 - 초기 설정               │ │
│  │                                                │ │
│  │   1. API 키 입력                               │ │
│  │   ┌──────────────────────────────────────┐     │ │
│  │   │ dmk_free_                            │     │ │
│  │   └──────────────────────────────────────┘     │ │
│  │   API 키가 없으신가요? [팜스퀘어에서 발급받기]   │ │
│  │                                                │ │
│  │   2. 도매상 계정 설정                           │ │
│  │   ┌──────────┬──────────┬──────────┐           │ │
│  │   │ 지오영    │ ID:      │ PW:      │  [테스트] │ │
│  │   │ 복산     │ ID:      │ PW:      │  [테스트] │ │
│  │   │ ...      │          │          │          │ │
│  │   └──────────┴──────────┴──────────┘           │ │
│  │                                                │ │
│  │   3. 텔레그램 설정 (선택)                       │ │
│  │   ┌──────────────────────────────────────┐     │ │
│  │   │ 봇 토큰:                             │     │ │
│  │   │ Chat ID:                             │     │ │
│  │   └──────────────────────────────────────┘     │ │
│  │                                                │ │
│  │                        [설정 완료 →]            │ │
│  │                                                │ │
│  └────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────┘
```

### 3.6 오프라인 동작 정책

```
API 키 검증 실패 시 동작:

1. 최초 실행 → API 키 필수 (온라인 검증)
2. 이후 실행 → 마지막 검증 성공 후 7일간 오프라인 동작 허용
3. 7일 초과 → "API 키 재검증이 필요합니다" 메시지 + 기능 제한
4. 네트워크 오류 → 기존 캐시된 검증 결과 사용 (graceful degradation)

목적: 인터넷 불안정한 약국도 사용 가능하되, 완전 오프라인 무제한은 방지
```

```python
# src/domae_mcp/local/api_key.py

OFFLINE_GRACE_DAYS = 7

class ApiKeyManager:
    def is_valid_cached(self) -> bool:
        """오프라인 시 캐시된 검증 결과 확인"""
        cached = self.config.get("api_key_verified_at")
        if not cached:
            return False
        verified_at = datetime.fromisoformat(cached)
        return (datetime.now() - verified_at).days < OFFLINE_GRACE_DAYS
```

---

## 4. 팜스퀘어 연동 구조

### 4.1 핵심 결정사항

- **회원가입/결제는 팜스퀘어가 담당** → domae-cloud에 인증/결제 시스템 불필요
- **클라우드 프론트엔드는 팜스퀘어에서 서브도메인으로 연결**
- **팜스퀘어 유료회원 = 도매 클라우드 서비스 이용 가능**
- **팜스퀘어 무료회원 = API 키 발급 → 로컬 설치형 사용 가능**

### 4.2 인증 흐름 (2가지)

```
A. 로컬 무료 사용자:
┌──────────┐                    ┌──────────────┐
│ 로컬 앱   │  API 키 검증       │ domae API    │
│ (PC)     │───────────────────▶│ 서버         │
│          │◀───────────────────│              │
│          │  { tier: "free" }  │              │
└──────────┘                    └──────────────┘
  ↕ 크롤러가 직접 도매 사이트 접속 (서버 경유 아님)

B. 클라우드 유료 사용자:
┌──────────┐     ┌──────────────┐     ┌──────────────┐
│ 브라우저  │ JWT │ 팜스퀘어      │     │ domae-cloud  │
│          │◀───│ 로그인        │     │ 서버         │
│          │────┼───────────────┼────▶│              │
│          │    │               │     │ JWT 검증 후   │
│          │◀───┼───────────────┼─────│ 서비스 제공   │
└──────────┘    └──────────────┘     └──────────────┘
  ↕ 클라우드 서버가 도매 사이트에 대신 접속
```

### 4.3 팜스퀘어 연결 방식 (클라우드)

**서브도메인 방식 (권장)**
```
팜스퀘어:      app.pharmsquare.com
도매 클라우드:  domae.pharmsquare.com   ← 별도 서브도메인

팜스퀘어 메뉴에서 "도매 통합검색" 클릭 → domae.pharmsquare.com으로 이동
JWT 토큰을 쿠키(같은 도메인) 또는 URL 파라미터로 전달
```

### 4.4 팜스퀘어 API 연동 (domae-cloud 서버)

```python
# cloud/auth/pharmsquare.py

class PharmSquareAuth:
    """팜스퀘어 JWT 검증 — 자체 인증 시스템 불필요"""

    def __init__(self, pharmsquare_api_url: str, api_key: str):
        self.api_url = pharmsquare_api_url  # 예: https://api.pharmsquare.com
        self.api_key = api_key

    async def verify_token(self, token: str) -> dict | None:
        """팜스퀘어 JWT를 검증하고 사용자 정보를 반환"""
        resp = await httpx.get(
            f"{self.api_url}/api/auth/verify",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Service-Key": self.api_key,  # 서비스 간 인증
            }
        )
        if resp.status_code != 200:
            return None

        return resp.json()
        # {
        #   "user_id": "ps_12345",
        #   "pharmacy_name": "마이약국",
        #   "phone": "010-1234-5678",
        #   "plan": "basic",         ← 팜스퀘어 요금제
        #   "plan_features": {
        #     "domae_enabled": true,
        #     "domae_tier": "basic"  ← basic / pro
        #   }
        # }

    async def get_domae_tier(self, token: str) -> str:
        """사용자의 도매 서비스 등급 확인"""
        user = await self.verify_token(token)
        if not user:
            raise HTTPException(401, "인증 실패")

        tier = user.get("plan_features", {}).get("domae_tier")
        if not tier:
            raise HTTPException(403, "도매 서비스 이용 권한이 없습니다")

        return tier  # "basic" or "pro"
```

```python
# cloud/auth/dependencies.py

from fastapi import Depends, Header

async def get_current_user(
    authorization: str = Header(...),
    ps_auth: PharmSquareAuth = Depends(get_ps_auth),
):
    """FastAPI 의존성 — 모든 API에서 사용"""
    token = authorization.replace("Bearer ", "")
    user = await ps_auth.verify_token(token)
    if not user:
        raise HTTPException(401)
    return user

async def require_plan(min_tier: str = "basic"):
    """요금제 확인 의존성"""
    async def checker(user = Depends(get_current_user)):
        tier = user["plan_features"].get("domae_tier")
        tiers = {"basic": 1, "pro": 2}
        if tiers.get(tier, 0) < tiers.get(min_tier, 0):
            raise HTTPException(403, f"{min_tier} 이상 요금제가 필요합니다")
        return user
    return checker
```

---

## 5. 요금제 상세

> 요금제/결제는 팜스퀘어에서 관리. domae-cloud는 팜스퀘어 API로 등급만 확인.
> **핵심 전략**: 도매로 수익을 내는 것이 아니라, 도매를 통해 팜스퀘어 유입 유도.

| 구분 | 도매 free | 도매 basic | 도매 pro |
|------|:---:|:---:|:---:|
| **비용** | 0원 | **0원** (팜스퀘어 구독자 무료) | **+5만/월** (추가 결제) |
| **조건** | 팜스퀘어 무료가입만 | 팜스퀘어 basic 이상 구독 | 도매 pro 추가 결제 |
| **제공 형태** | 로컬 PC | 클라우드 | 클라우드 |
| **운영 시간** | PC 켜져있을 때 | 24시간 | 24시간 |
| | | | |
| **통합 검색** | O | O | O |
| **자동 주문** | O | O | O |
| **긴급주문** | O | O | O |
| **가격 변동 모니터링** | O | O | O |
| **MCP (Claude Desktop)** | O | O | O |
| | | | |
| **모니터링 간격** | 60분 | 30분 | 5분 |
| **알림 채널** | 텔레그램 | 텔레그램 | 텔레그램 + **카카오톡** |
| **검색 이력 보관** | 로컬 7일 | 클라우드 90일 | 클라우드 1년 |
| **다중 약국 관리** | 1개 | 1개 | **최대 3개** |
| **전용 대시보드** | 로컬 UI | 웹 대시보드 | 웹 대시보드 + 통계 |

> 팜스퀘어 구독 (basic 5천 / pro 2.9만 / enterprise 9.9만) 하면
> 도매 basic이 자동으로 따라옴 → "도매 쓰려고 팜스퀘어 가입"하는 전환 유도

---

## 6. 시스템 아키텍처

### 6.1 전체 구조

```
┌─────────────────────────────────────────────────────────────────────┐
│                          팜스퀘어 플랫폼                              │
│                                                                       │
│  ┌────────────────────┐  ┌──────────────────────────────────────┐   │
│  │ 팜스퀘어 프론트엔드  │  │ 팜스퀘어 백엔드                      │   │
│  │                     │  │                                      │   │
│  │  ┌───────────────┐ │  │  ┌────────────┐  ┌───────────────┐  │   │
│  │  │ 기존 메뉴     │ │  │  │ 인증 서비스 │  │ 결제/구독     │  │   │
│  │  ├───────────────┤ │  │  │ (JWT 발급)  │  │ (토스페이먼츠) │  │   │
│  │  │ 도매 통합검색 │─┼──┼──│             │  │               │  │   │
│  │  │ (서브도메인)  │ │  │  └──────┬──────┘  └───────────────┘  │   │
│  │  └───────────────┘ │  │         │                             │   │
│  └────────────────────┘  └─────────┼─────────────────────────────┘   │
│                                     │ JWT 검증                        │
└─────────────────────────────────────┼────────────────────────────────┘
                                      │
                    ┌─────────────────▼──────────────────────┐
                    │       domae-cloud 서버 (비공개)          │
                    │       domae.pharmsquare.com              │
                    │                                          │
                    │  ┌──────────────┐  ┌────────────────┐  │
                    │  │ FastAPI      │  │ Celery Beat    │  │
                    │  │ (팜스퀘어    │  │ (스케줄러)      │  │
                    │  │  JWT 검증)   │  │ 30분/5분       │  │
                    │  └──────┬───────┘  └───────┬────────┘  │
                    │         │                   │            │
                    │  ┌──────▼───────────────────▼────────┐  │
                    │  │        코어 엔진 (공유)             │  │
                    │  │  domae_mcp.core 패키지 import     │  │
                    │  │  크롤러 8개 + 서비스 로직           │  │
                    │  └──────────────────────────────────┘  │
                    │                                          │
                    │  ┌──────────┐ ┌────────┐ ┌───────────┐ │
                    │  │PostgreSQL│ │ Redis  │ │ 카카오톡  │ │
                    │  │          │ │        │ │ 알림톡   │ │
                    │  └──────────┘ └────────┘ └───────────┘ │
                    └──────────────────────────────────────────┘
                                      │
                    ┌─────────────────┼──────────────────────┐
                    ▼                 ▼                      ▼
                 지오영             복산           ...     새로팜


              ※ 로컬 사용자도 팜스퀘어 무료가입 후 API 키 필요

┌──────────────────────────────────────────────────────┐
│                     약국 PC (로컬 무료)                 │
│                                                        │
│  ┌──────────┐    ┌──────────────────────┐             │
│  │ 브라우저  │◀──▶│ FastAPI 웹서버        │             │
│  └──────────┘    │ localhost:5900       │             │
│  ┌──────────┐    └──────────┬───────────┘             │
│  │ Claude   │◀──▶│ 코어 엔진 (동일 코드) │             │
│  │ Desktop  │    │ 모니터링 60분        │             │
│  └──────────┘    │ 텔레그램 알림        │             │
│   (MCP stdio)    └─────────┬────────────┘             │
│                            │                           │
│                   API 키 검증 (시작 시 1회 + 12h 하트비트) │
│                            │                           │
└────────────────────────────┼───────────────────────────┘
              │              │
    ┌─────────┼──────────┐   │
    ▼         ▼          ▼   ▼
 지오영    복산     ...  새로팜  domae API 서버
                              (api.domae.kr)
```

### 6.2 핵심: domae-cloud는 인증/결제 없이 가볍게

팜스퀘어가 인증/결제를 담당하므로 domae-cloud에 필요한 것:

| 필요한 것 | 불필요한 것 (팜스퀘어가 담당) |
|---|---|
| 팜스퀘어 JWT 검증 미들웨어 | 회원가입 |
| 사용자별 도매 계정 관리 | 로그인/비밀번호 관리 |
| 스케줄러 (Celery) | 결제/구독 관리 |
| 카카오 알림톡 발송 | 요금제 변경 UI |
| 검색/주문 API | 결제 웹훅 |

---

## 7. 코드 공유 전략

### 7.1 핵심 원칙: 하나의 코어, 두 개의 실행 환경

```
maipharm-domae-mcp/              ← BSL 라이선스, 공개 저장소
├── src/domae_mcp/
│   ├── core/                    ← 공유 코어 (크롤러, 서비스, 모델)
│   │   ├── crawlers/            # 8개 도매상 크롤러
│   │   ├── services/            # 검색, 주문, 모니터링
│   │   └── models/              # 데이터 모델
│   │
│   ├── local/                   ← 로컬 모드 전용
│   │   ├── __main__.py          # CLI 진입점
│   │   ├── server.py            # FastAPI (localhost)
│   │   ├── mcp_server.py        # MCP stdio
│   │   ├── config.py            # config.json 관리
│   │   ├── database.py          # SQLite
│   │   └── scheduler.py         # 로컬 스케줄러 (60분 최소)
│   │
│   └── static/                  # React 빌드

maipharm-domae-cloud/            ← 비공개 저장소 (서버 전용)
├── cloud/
│   ├── server.py                # FastAPI + 팜스퀘어 JWT 검증
│   ├── database.py              # PostgreSQL (멀티 테넌트)
│   ├── scheduler.py             # Celery Beat (5분~30분)
│   ├── auth/
│   │   └── pharmsquare.py       # 팜스퀘어 JWT 검증 (자체 인증 X)
│   ├── notifications/
│   │   └── kakao_service.py     # 카카오 알림톡
│   └── routers/                 # API 라우터
└── requirements.txt
    └── domae-mcp>=1.0.0         # 코어를 pip 패키지로 의존
```

### 7.2 로컬 vs 클라우드 코드 차이점 정리

```
                    로컬 (공개)              클라우드 (비공개)
                    ──────────              ────────────────
크롤러 코드         core/ (공유)             core/ (동일, pip import)
검색/주문 로직      core/ (공유)             core/ (동일, pip import)

FastAPI 서버        local/server.py          cloud/server.py
                    - 127.0.0.1 바인딩       - 0.0.0.0 + Nginx
                    - 인증 없음              - 팜스퀘어 JWT 검증

DB                  local/database.py        cloud/database.py
                    - SQLite                 - PostgreSQL
                    - 단일 사용자            - user_id로 격리

스케줄러            local/scheduler.py       cloud/scheduler.py
                    - threading (60분↑)      - Celery Beat (5분↑)
                    - PC 실행 중만 동작       - 24시간 동작

설정 관리           local/config.py          cloud/ (PostgreSQL)
                    - ~/.maipharm/ 파일      - user_credentials 테이블
                    - Fernet 암호화          - AES-256 서버측 암호화

알림                core/telegram_service    core/telegram_service (동일)
                    - 사용자 직접 설정        + cloud/kakao_service (추가)
                    - 텔레그램만              - 카카오톡 + 텔레그램

MCP 서버            local/mcp_server.py      (해당 없음)
                    - stdio transport        - 클라우드는 웹 UI만

프론트엔드          static/ (React 빌드)     팜스퀘어에서 연결
                    - localhost 접속         - domae.pharmsquare.com
                    - 자체 완결형            - 팜스퀘어 UI 통합

인증                local/api_key.py         auth/pharmsquare.py
                    - 팜스퀘어 API 키 검증   - 팜스퀘어 JWT 검증
                    - 시작 시 1회 + 하트비트  - 매 요청마다
                    - 오프라인 7일 유예       - 자체 가입/로그인 없음

결제                없음 (무료)              없음 (팜스퀘어가 관리)
```

---

## 8. 클라우드 프론트엔드 — 팜스퀘어 연결 방식

### 8.1 서브도메인 방식 (권장)

```
팜스퀘어 사이드바:
┌─────────────────────┐
│  팜스퀘어            │
│  ─────────────────  │
│  > 대시보드          │
│  > 재고 관리         │
│  > 처방 분석         │
│  > ──────────────── │
│  > 도매 통합검색  ←──┼── 클릭 시 domae.pharmsquare.com 이동
│  > ──────────────── │       (팜스퀘어 JWT 쿠키로 자동 인증)
│  > 설정              │
└─────────────────────┘
```

```
domae.pharmsquare.com 페이지 구성:

┌──────────────────────────────────────────────┐
│  팜스퀘어 상단 네비게이션 (공통)               │
├──────────────────────────────────────────────┤
│                                               │
│  ┌─────────┬───────────┬──────────┬────────┐ │
│  │ 통합검색 │ 긴급주문  │ 주문이력 │ 설정   │ │
│  └─────────┴───────────┴──────────┴────────┘ │
│                                               │
│  ┌───────────────────────────────────────┐    │
│  │                                       │    │
│  │   (기존 로컬 React UI와 동일한 화면)    │    │
│  │   검색, 주문, 모니터링 등               │    │
│  │                                       │    │
│  └───────────────────────────────────────┘    │
│                                               │
│  ┌───────────────────────────────────────┐    │
│  │  현재 요금제: 베이직  |  모니터링 30분  │    │
│  │  [프로 업그레이드] → 팜스퀘어 결제로    │    │
│  └───────────────────────────────────────┘    │
└──────────────────────────────────────────────┘
```

### 8.2 프론트엔드 코드 재사용

```
로컬 frontend/src/           클라우드 frontend-cloud/src/
├── pages/                   ├── pages/
│   ├── SearchPage.jsx       │   ├── SearchPage.jsx      ← 동일
│   ├── UrgentPage.jsx       │   ├── UrgentPage.jsx      ← 동일
│   ├── HistoryPage.jsx      │   ├── HistoryPage.jsx     ← 동일
│   ├── ProductsPage.jsx     │   ├── ProductsPage.jsx    ← 동일
│   └── SettingsPage.jsx     │   ├── SettingsPage.jsx    ← 동일 (계정 관리)
│                            │   └── DashboardPage.jsx   ← 프로 전용 (신규)
├── api/                     ├── api/
│   └── client.js            │   └── client.js           ← API URL + JWT 헤더 추가
│       baseURL: "/api"      │       baseURL: "https://domae.pharmsquare.com/api"
│       headers: (없음)       │       headers: { Authorization: Bearer {jwt} }
│                            │
└── App.jsx                  └── App.jsx                  ← 팜스퀘어 네비 + 요금제 표시
```

> 핵심 페이지 컴포넌트는 **npm 패키지** 또는 **git submodule**로 공유 가능.
> 차이점은 API client 설정과 최상위 레이아웃뿐.

---

## 9. 데이터베이스

### 9.1 로컬 (변경 없음)

기존 SQLite 스키마 그대로 유지. 단일 사용자, `user_id` 불필요.

### 9.2 클라우드 (PostgreSQL)

```
기존 테이블 + user_id 추가              신규 테이블
────────────────────────              ──────────────

┌────────────────────────┐     ┌─────────────────────────┐
│  domae_users            │     │  user_credentials        │
├────────────────────────┤     ├─────────────────────────┤
│ id (PK)                 │◀───│ id (PK)                  │
│ pharmsquare_user_id (UQ)│     │ user_id (FK)             │
│ pharmacy_name           │     │ supplier                 │
│ phone                   │     │ login_id                 │
│ domae_tier (basic/pro)  │     │ login_pw_encrypted       │
│ kakao_enabled           │     │ last_tested_at           │
│ created_at              │     │ is_valid                 │
│ last_active_at          │     └─────────────────────────┘
└────────────────────────┘

기존 테이블에 user_id FK 추가:
  products.user_id         → domae_users.id
  orders.user_id           → domae_users.id
  urgent_orders.user_id    → domae_users.id
  inventory_snapshots.user_id → domae_users.id
  monitor_schedules.user_id   → domae_users.id
```

> `domae_users`는 팜스퀘어 사용자의 **캐시 테이블**.
> 매 요청 시 팜스퀘어 API를 호출하지 않고, JWT 검증 후 로컬 캐시 참조.
> 요금제 변경은 팜스퀘어 웹훅 또는 주기적 동기화로 반영.

---

## 10. 알림 채널 설계

### 10.1 채널별 구현

```
┌──────────────────────────────────────────────────┐
│                 알림 서비스                         │
│                                                    │
│  ┌──────────────────┐   ┌──────────────────────┐  │
│  │ TelegramService  │   │ KakaoService         │  │
│  │ (로컬 + 클라우드) │   │ (클라우드 전용)       │  │
│  │                  │   │                      │  │
│  │ • 사용자 직접     │   │ • 카카오 알림톡 API  │  │
│  │   봇 토큰 설정    │   │ • 마이팜 비즈채널    │  │
│  │ • 무료           │   │ • 서버에서 발송      │  │
│  │ • 설정 약간 번거로움│  │ • 유료 사용자만      │  │
│  └──────────────────┘   └──────────────────────┘  │
│           │                        │               │
│           ▼                        ▼               │
│  ┌──────────────────────────────────────────────┐ │
│  │            NotificationRouter                 │ │
│  │                                                │ │
│  │  가격 변동    → 텔레그램 (무료) / 카톡 (유료)  │ │
│  │  긴급주문 체결 → 텔레그램 + 카톡 동시          │ │
│  │  재고 감지    → 텔레그램 (무료) / 카톡 (유료)  │ │
│  │  시스템 알림  → 텔레그램만                     │ │
│  └──────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────┘
```

### 10.2 카카오 알림톡이 클라우드 전용인 이유

카카오 알림톡은 **비즈니스 채널 + 템플릿 심사 + API 인증**이 필요하여
개인이 로컬에서 직접 설정하는 것이 사실상 불가능.
→ 클라우드 서비스의 자연스러운 부가가치이자, 기술적으로 우회 불가한 차별점.

---

## 11. 로컬 모드 간격 제한 & 우회 방지

### 11.1 로컬 스케줄러 (공개 코드)

```python
# src/domae_mcp/local/scheduler.py

LOCAL_MIN_INTERVAL = 60  # 분

class LocalScheduler:
    """로컬 모드 스케줄러. 최소 간격 60분."""

    def set_interval(self, interval_minutes: int):
        if interval_minutes < LOCAL_MIN_INTERVAL:
            raise ValueError(
                f"로컬 모드 최소 모니터링 간격은 {LOCAL_MIN_INTERVAL}분입니다. "
                f"더 짧은 간격은 팜스퀘어 클라우드 서비스를 이용해주세요."
            )
        self._interval = interval_minutes
```

### 11.2 우회 방지 전략 (이중 보호)

| 계층 | 방식 | 설명 |
|------|------|------|
| **기술적** | API 키 필수 | 팜스퀘어 가입 없이는 사용 불가, 사용자 추적 가능 |
| **기술적** | 코드 분리 | 클라우드 스케줄러는 비공개 저장소에만 존재 |
| **기술적** | 간격 하드코딩 | 로컬 코드에서 60분 미만 설정 불가 |
| **기술적** | 카카오 알림 | 마이팜 비즈채널이어야 발송 가능 |
| **법적** | BSL 라이선스 | 간격 우회/호스팅 서비스화 금지 |
| **실용적** | 가치 차별화 | 기능 제한이 아닌 편의성 차별화 → 우회 동기 낮음 |

> **현실 인정**: 누군가 포크해서 API 키 검증 + 60분 제한을 풀 수 있음.
> 그러나 (1) BSL 위반, (2) 24시간 운영/카톡 알림/클라우드 이력은 재현 불가,
> (3) API 키로 사용자를 파악하고 있으므로 남용 시 키 비활성화 가능.
> 월 5~10만원 가치가 있다면 대부분 유료를 선택함.

---

## 12. 디렉토리 구조

### 12.1 공개 저장소 (BSL 라이선스)

```
maipharm-domae-mcp/
├── src/domae_mcp/
│   ├── __init__.py                 # __version__
│   │
│   ├── core/                       # ★ 공유 코어
│   │   ├── __init__.py
│   │   ├── crawlers/               # 8개 도매상 크롤러
│   │   │   ├── __init__.py
│   │   │   ├── base.py             # BaseCrawler, SearchResult, OrderResult
│   │   │   ├── registry.py         # CrawlerRegistry
│   │   │   ├── geoweb.py           # 지오영
│   │   │   ├── boksan.py           # 복산
│   │   │   ├── inchun.py           # 인천
│   │   │   ├── tjpharm.py          # 티제이팜
│   │   │   ├── hmpmall.py          # HMP
│   │   │   ├── beakje.py           # 백제
│   │   │   ├── picomall.py         # 피코
│   │   │   └── saeropharm.py       # 새로팜
│   │   ├── services/               # 비즈니스 로직
│   │   │   ├── __init__.py
│   │   │   ├── search_service.py   # 통합 검색
│   │   │   ├── order_service.py    # 주문 실행
│   │   │   ├── monitor_service.py  # 재고 모니터링
│   │   │   └── telegram_service.py # 텔레그램 알림
│   │   └── models/                 # SQLAlchemy 모델
│   │       ├── __init__.py
│   │       ├── product.py
│   │       ├── order.py
│   │       ├── urgent_order.py
│   │       ├── inventory.py
│   │       └── schedule.py
│   │
│   ├── local/                      # ★ 로컬 모드 전용
│   │   ├── __init__.py
│   │   ├── __main__.py             # CLI 진입점
│   │   ├── server.py               # FastAPI (localhost)
│   │   ├── mcp_server.py           # MCP stdio
│   │   ├── api_key.py              # ★ API 키 검증 + 하트비트
│   │   ├── config.py               # config.json 관리
│   │   ├── database.py             # SQLite
│   │   ├── scheduler.py            # 로컬 스케줄러 (60분 최소)
│   │   ├── startup.py              # Windows 자동시작
│   │   └── routers/                # FastAPI 라우터
│   │       ├── __init__.py
│   │       ├── search.py
│   │       ├── order.py
│   │       ├── urgent.py
│   │       ├── products.py
│   │       ├── settings.py
│   │       ├── monitor.py
│   │       ├── supplier_request.py
│   │       └── update.py
│   │
│   └── static/                     # React 빌드 결과물
│
├── frontend/                       # React 소스 (로컬 UI)
├── docs/
├── LICENSE                         # BSL 1.1
└── requirements.txt
```

### 12.2 비공개 저장소

```
maipharm-domae-cloud/
├── cloud/
│   ├── __init__.py
│   ├── server.py                   # FastAPI (팜스퀘어 JWT 검증)
│   ├── database.py                 # PostgreSQL
│   ├── scheduler.py                # Celery Beat (5분~30분)
│   ├── config.py                   # 환경변수 기반 설정
│   │
│   ├── auth/
│   │   ├── pharmsquare.py          # 팜스퀘어 JWT 검증 (핵심)
│   │   └── dependencies.py         # FastAPI 의존성
│   │
│   ├── notifications/
│   │   └── kakao_service.py        # 카카오 알림톡
│   │
│   ├── routers/
│   │   ├── search.py               # 코어 호출 래퍼
│   │   ├── order.py
│   │   ├── urgent.py
│   │   ├── products.py
│   │   ├── settings.py             # 도매 계정 관리
│   │   ├── monitor.py
│   │   └── dashboard.py            # 통계 (프로 전용)
│   │
│   └── models/
│       ├── domae_user.py           # 팜스퀘어 사용자 캐시
│       └── user_credential.py      # 도매 계정 (서버측 암호화)
│
├── frontend-cloud/                 # 클라우드 프론트엔드
│   └── src/
│       ├── App.jsx                 # 팜스퀘어 네비 통합
│       ├── api/client.js           # JWT 헤더 포함
│       └── pages/                  # 로컬 페이지 재사용 + 대시보드 추가
│
├── docker-compose.yml
├── Dockerfile
└── requirements.txt
    └── domae-mcp>=1.0.0            # 공개 코어 패키지 의존
```

---

## 13. 배포 구조

### 13.1 로컬 (기존과 동일)

```bash
git clone https://github.com/maipharm/maipharm-domae-mcp
cd maipharm-domae-mcp
pip install -r requirements.txt
cd frontend && npm install && npm run build
python -m domae_mcp           # 웹서버
python -m domae_mcp --mcp     # MCP
```

### 13.2 클라우드

```yaml
# docker-compose.yml
services:
  api:
    build: .
    ports: ["8000:8000"]
    environment:
      - DATABASE_URL=postgresql://...
      - REDIS_URL=redis://redis:6379
      - PHARMSQUARE_API_URL=https://api.pharmsquare.com
      - PHARMSQUARE_SERVICE_KEY=...
      - KAKAO_API_KEY=...
      - ENCRYPTION_MASTER_KEY=...

  worker:
    build: .
    command: celery -A cloud.scheduler worker

  beat:
    build: .
    command: celery -A cloud.scheduler beat

  redis:
    image: redis:7-alpine

  db:
    image: postgres:16-alpine
    volumes: ["pgdata:/var/lib/postgresql/data"]
```

Nginx 설정:
```nginx
# domae.pharmsquare.com
server {
    listen 443 ssl;
    server_name domae.pharmsquare.com;

    # SSL 인증서 (팜스퀘어 와일드카드)
    ssl_certificate     /etc/ssl/pharmsquare.com.crt;
    ssl_certificate_key /etc/ssl/pharmsquare.com.key;

    location /api/ {
        proxy_pass http://localhost:8000;
    }

    location / {
        root /var/www/domae-cloud/static;
        try_files $uri /index.html;
    }
}
```

---

## 14. 팜스퀘어 요금제 동기화

### 14.1 웹훅 방식 (실시간)

```python
# cloud/routers/webhook.py

@router.post("/webhook/pharmsquare")
async def pharmsquare_webhook(request: Request):
    """팜스퀘어가 요금제 변경 시 호출"""
    payload = await request.json()
    # 서명 검증
    verify_pharmsquare_signature(request.headers, payload)

    event = payload["event"]
    user_id = payload["user_id"]

    if event == "subscription.upgraded":
        tier = payload["domae_tier"]  # "basic" or "pro"
        await update_user_tier(user_id, tier)
        await adjust_scheduler(user_id, tier)  # 스케줄러 간격 변경

    elif event == "subscription.canceled":
        await update_user_tier(user_id, None)
        await stop_scheduler(user_id)  # 스케줄러 중지
        # 데이터는 보관 (30일 후 삭제)
```

### 14.2 폴링 방식 (백업)

```python
# cloud/scheduler.py

@celery.task
def sync_pharmsquare_plans():
    """매 시간 팜스퀘어 API로 요금제 동기화 (웹훅 누락 대비)"""
    users = db.query(DomaeUser).all()
    for user in users:
        ps_user = pharmsquare_api.get_user(user.pharmsquare_user_id)
        if ps_user["domae_tier"] != user.domae_tier:
            user.domae_tier = ps_user["domae_tier"]
            adjust_scheduler(user.id, user.domae_tier)
```

---

## 15. BSL 라이선스 조항

```
Business Source License 1.1

Licensed Work:    maipharm-domae-mcp
Licensor:         MaiPharm (마이팜)

Additional Use Grant:
  1. 개인 또는 단일 약국이 자신의 업무를 위해 로컬에서 사용
  2. 교육 및 학습 목적의 사용

Limitation:
  1. 본 소프트웨어를 이용한 호스팅/SaaS 서비스 제공
  2. 모니터링 간격 제한(60분)을 우회하여 재배포
  3. 10개 이상 약국을 대상으로 한 서비스 운영
  4. 상업적 목적의 포크 및 재배포

Change Date:      2029-03-15
Change License:   Apache License 2.0
```

---

## 16. 구현 순서

```
═══ 로컬 버전 (공개 저장소, ~2주) ═══

Phase 0: 프로젝트 골격 (0.5일)
├─ 디렉토리 구조 생성 (core/ + local/)
├─ __main__.py 진입점
└─ pip install -e . 동작 확인

Phase 1: 코어 엔진 (3~4일)
├─ BaseCrawler + 데이터 모델
├─ 크롤러 8개 이식 (domae-v2에서)
├─ SQLAlchemy 모델 5개
└─ 핵심 서비스 (검색, 주문, 모니터링, 텔레그램)

Phase 2: 로컬 백엔드 (2~3일)
├─ 설정 관리 (config.json + 암호화)
├─ API 키 검증 (팜스퀘어 연동)
├─ FastAPI 서버 + REST API (8개 라우터)
├─ 스케줄러 (60분 최소 제한)
└─ Windows 자동시작

Phase 3: MCP 서버 (1일) ← Phase 2와 병렬 가능
├─ 12개 도구 정의 (stdio transport)
└─ Claude Desktop 연동

Phase 4: 프론트엔드 (3~4일) ← Phase 2 1순위 완료 후 병렬 가능
├─ React + Vite 셋업
├─ 검색 페이지, 설정 페이지
├─ 긴급주문, 주문이력, 모니터링 페이지
└─ 빌드 → static/ 배치

Phase 5: 통합 & 배포 (1~2일)
├─ 통합 테스트
├─ BSL 라이선스 적용
├─ README 업데이트
└─ GitHub 릴리스 v1.0.0

═══ 클라우드 버전 (비공개 저장소, ~3주) ═══

Phase 6: 클라우드 MVP (2~3주)
├─ 팜스퀘어 JWT 검증 미들웨어
├─ PostgreSQL + 멀티 테넌트
├─ 도매 계정 관리 (서버측 AES-256 암호화)
├─ Celery 스케줄러 (30분/5분)
├─ 프론트엔드 (팜스퀘어 연결)
└─ Docker + Nginx 배포

Phase 7: 프리미엄 기능 (1~2주)
├─ 카카오 알림톡 연동
├─ 프로 전용 대시보드 (통계, 가격 추이)
├─ 팜스퀘어 요금제 동기화 웹훅
└─ 다중 약국 관리 (프로)
```

> 상세 구현 계획은 `docs/WORKFLOW.md` 참고.
> Phase 1 완료 후 Phase 2, 3 병렬 진행 가능.
> Phase 2 1순위 API 완료 후 Phase 4 병렬 진행 가능.

---

## 17. 수익 시뮬레이션

```
목표: 우선 확산, 수익은 부차적

로컬 무료 사용자 200개 약국 → 인지도/입소문 확보
팜스퀘어 유료회원 중 도매 서비스 이용:

  베이직 (5만) × 30개 = 150만/월
  프로 (10만)  × 10개 = 100만/월
  ──────────────────────
  월 매출:           250만원
  서버 비용 (예상):   30~50만원
  카카오 알림 비용:   ~10만원
  ──────────────────────
  월 순이익:         ~200만원

※ 팜스퀘어 기존 유료회원에게 번들 제공 시
   전환율이 더 높아질 수 있음 (기존 신뢰 관계 활용)
```
