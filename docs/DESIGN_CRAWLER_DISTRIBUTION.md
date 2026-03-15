# 크롤러 서버 배포 시스템 설계

> 패턴: 오픈소스 프레임워크 + 비공개 플러그인 (WordPress, VS Code 모델)
> 작성일: 2026-03-16
> 수정일: 2026-03-16 (검증 결과 반영 — C1~C4, M1~M7 수정)

---

## 1. 전체 아키텍처

```
┌─────────────────────────────────────────────────────────────┐
│  팜스퀘어 서버 (Express.js + Prisma + PostgreSQL)             │
│                                                              │
│  ┌──────────────────────────────────────────────────┐        │
│  │  domae_crawlers 테이블                             │        │
│  │  ┌──────────┬──────────────────────┬───────────┐  │        │
│  │  │ geoweb   │ class GeoCrawler...  │ hash: ab12│  │        │
│  │  │ boksan   │ class BoksanCrawl... │ hash: cd34│  │        │
│  │  │ ...      │ ...                  │ ...       │  │        │
│  │  └──────────┴──────────────────────┴───────────┘  │        │
│  └──────────────────────────────────────────────────┘        │
│                                                              │
│  API:                                                        │
│  GET /api/domae/crawlers ──── API키 검증 → 서명된 번들 응답    │
│  GET /api/domae/crawlers/version ── 해시만 (변경 체크)         │
│  POST /api/domae/verify ──── API키 검증 + 사용자 정보         │
│  POST /api/domae/heartbeat ── 사용 통계 수집                  │
│                                                              │
│  서명: Ed25519 개인키로 번들 서명                              │
└──────────────────────┬──────────────────────────────────────┘
                       │ HTTPS
                       ▼
┌─────────────────────────────────────────────────────────────┐
│  로컬 클라이언트 (Python, 사용자 PC)                           │
│                                                              │
│  ┌──────────────────────────────────────────────────┐        │
│  │  CrawlerLoader (신규)                              │        │
│  │                                                    │        │
│  │  시작 시:                                          │        │
│  │  1. 서버에 크롤러 요청 (API 키 첨부)                │        │
│  │  2. Ed25519 공개키로 서명 검증                      │        │
│  │  3. ~/.maipharm-domae-mcp/crawlers/에 캐시          │        │
│  │  4. importlib로 동적 로드                          │        │
│  │  5. CrawlerRegistry에 등록                         │        │
│  │                                                    │        │
│  │  오프라인: 캐시된 번들로 최대 14일간 동작 (만료7일+유예7일) │
│  └──────────────────────────────────────────────────┘        │
│                                                              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐        │
│  │ SearchService│  │ OrderService │  │  MCP Server  │        │
│  │ (변경 없음)   │  │ (변경 없음)  │  │  (변경 없음)  │        │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘        │
│         └─────────────────┼─────────────────┘                │
│                           ▼                                  │
│                   CrawlerRegistry                            │
│                   (변경: 정적 import → 동적 로드)               │
└─────────────────────────────────────────────────────────────┘
```

**핵심 원칙**: CrawlerRegistry 인터페이스(get/list_all)는 동일 유지.
SearchService, OrderService는 **크롤러 미로드 시 graceful 처리 추가** (is_loaded 체크).
MCP Server는 변경 없음.

---

## 2. 서명 메커니즘

### 왜 Ed25519인가

| | RSA | Ed25519 |
|---|---|---|
| 키 크기 | 2048+ bit | 32 byte |
| 서명 속도 | 느림 | 빠름 |
| 검증 속도 | 빠름 | 빠름 |
| 구현 | Python: cryptography | Python: cryptography (동일) |
| Node.js | crypto 내장 | crypto 내장 (Node 16+) |

Ed25519가 간결하고 양쪽 언어에서 네이티브 지원.

### 키 관리

```
서버 (팜스퀘어):
  - 개인키: 환경변수 DOMAE_SIGNING_PRIVATE_KEY
  - 번들 서명 시 사용
  - 절대 외부 노출 금지

로컬 (클라이언트):
  - 공개키: 소스코드에 하드코딩 (base64 문자열)
  - src/domae_mcp/core/crawlers/loader.py에 상수로 포함
  - 공개키이므로 코드에 노출되어도 안전
```

### 번들 구조 (Major 2 수정: 실제 와이어 포맷 반영)

서버 응답은 **래퍼 구조**이다. `payload`는 JSON 문자열이고 `signature`는 분리되어 있다.
클라이언트는 `payload` 문자열을 재직렬화하지 않고 그대로 SHA256 → 서명 검증한다.

```json
// 서버 HTTP 응답 (실제 와이어 포맷)
{
  "payload": "{\"version\":\"2026.03.16.001\",\"issued_at\":\"...\",\"expires_at\":\"...\",\"api_key_hash\":\"...\",\"crawlers\":{...}}",
  "signature": "base64로 인코딩된 Ed25519 서명"
}
```

`payload` JSON 문자열을 파싱하면:
```json
{
  "version": "2026.03.16.001",
  "issued_at": "2026-03-16T09:00:00Z",
  "expires_at": "2026-03-23T09:00:00Z",
  "api_key_hash": "sha256(사용자의 API 키)",
  "crawlers": {
    "geoweb": "class GeoCrawler(BaseCrawler):\n    ...",
    "boksan": "class BoksanCrawler(BaseCrawler):\n    ...",
    "..."
  }
}
```

### 서명 방식: 응답 바이트 직접 서명 (C1 수정)

JSON 직렬화 순서가 Node.js와 Python에서 다를 수 있으므로,
**JSON 재직렬화 대신 원본 응답 바이트의 SHA256 해시를 서명한다.**

```
서버: payload_bytes = JSON.stringify(bundle_without_signature)
      signature = Ed25519.sign(SHA256(payload_bytes))
      → 응답에 signature 필드 추가

클라이언트: 응답 JSON에서 signature 추출
           나머지 필드를 원본 바이트 그대로 SHA256
           Ed25519.verify(hash, signature)
```

실제 구현에서는 서버가 signature를 제외한 번들을 먼저 JSON 문자열로 만들고,
그 문자열의 SHA256을 서명한다. 클라이언트는 동일한 문자열을 재구성하지 않고,
**서버가 보낸 원문 바이트를 그대로 사용**하여 검증한다.

### 서명 생성 (서버, Node.js)

```typescript
import crypto from 'crypto';

function createSignedBundle(bundleData: object, privateKeyDer: Buffer): object {
  // 1. signature 없는 번들을 JSON 문자열로 (이게 서명 대상)
  const payloadStr = JSON.stringify(bundleData);

  // 2. SHA256 해시 → Ed25519 서명
  const hash = crypto.createHash('sha256').update(payloadStr).digest();
  const signature = crypto.sign(null, hash, {
    key: privateKeyDer,
    format: 'der',
    type: 'pkcs8',
  });

  // 3. 번들에 payload_raw + signature 추가
  return {
    payload: payloadStr,              // 서명 대상 원문 (JSON 문자열)
    signature: signature.toString('base64'),
  };
}
```

### 서명 검증 (로컬, Python)

```python
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.hashes import SHA256
import json, base64, hashlib

PUBLIC_KEY_B64 = "..."  # 32바이트 Ed25519 공개키 (base64)

def verify_and_parse_bundle(response: dict) -> dict | None:
    """서버 응답을 검증하고 번들 데이터를 파싱.

    response 구조: {"payload": "JSON문자열", "signature": "base64"}
    """
    payload_str = response.get("payload")
    signature_b64 = response.get("signature")
    if not payload_str or not signature_b64:
        return None

    # 원문 바이트의 SHA256 해시로 서명 검증
    payload_hash = hashlib.sha256(payload_str.encode()).digest()
    signature = base64.b64decode(signature_b64)
    public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(PUBLIC_KEY_B64))

    try:
        public_key.verify(signature, payload_hash)
    except Exception:
        return None

    # 검증 통과 → JSON 파싱
    return json.loads(payload_str)
```

**핵심**: 서버가 보낸 `payload` 문자열을 클라이언트가 재직렬화하지 않고
그대로 해시하여 검증한다. JSON 키 순서 차이 문제가 원천 제거됨.

### 방어 수준

```
✅ 막을 수 있는 것:
  - 번들을 수정하여 재배포 (서명 불일치)
  - 다른 사람의 번들을 복사 (api_key_hash 불일치)
  - 만료된 번들 영구 사용 (expires_at 검증)

❌ 막을 수 없는 것 (로컬 코드 수정 시):
  - verify_bundle() 함수 자체를 무력화
  - 캐시된 .py 파일을 직접 import
  → "의도적 우회"는 기술적으로 불가피.
    목적은 "쉽게 복사/재배포 방지"이지 "완벽한 DRM"이 아님.
```

---

## 3. 팜스퀘어 서버 측 설계

### 3-1. Prisma 스키마 추가 (C3/C4 수정: 기존 스키마 컨벤션 준수)

팜스퀘어 기존 스키마는 모든 모델이 `String @id @default(cuid())`를 사용하고,
`Pharmacy.id`도 `String` 타입이다. 이에 맞춰 설계한다.

또한 보안을 위해 raw API 키는 DB에 저장하지 않는다 (m3 수정).
발급 시 한 번만 사용자에게 보여주고, DB에는 해시 + prefix만 저장한다.

```prisma
// prisma/schema.prisma에 추가

model DomaeApiKey {
  id            String    @id @default(cuid())
  keyHash       String    @unique                // SHA256(key) — 검증용 (raw 키 저장 안 함)
  keyPrefix     String                           // "dmk_free_a1b2" — 식별/표시용 (앞 16자)
  tier          String    @default("free")       // "free" | "basic" | "pro"
  pharmacyId    String?
  pharmacy      Pharmacy? @relation(fields: [pharmacyId], references: [id])
  pharmacyName  String    @default("")
  isActive      Boolean   @default(true)

  // 사용 통계 (하트비트로 갱신)
  lastHeartbeat DateTime?
  lastVersion   String?                          // 클라이언트 앱 버전
  searchCount   Int       @default(0)
  orderCount    Int       @default(0)

  createdAt     DateTime  @default(now())
  updatedAt     DateTime  @updatedAt

  @@index([pharmacyId])
  @@index([isActive])
}

// Pharmacy 모델에 관계 추가 필요 (M5):
// model Pharmacy {
//   ...
//   domaeApiKeys  DomaeApiKey[]
// }

model DomaeCrawler {
  id            String    @id @default(cuid())
  name          String    @unique                // "geoweb", "boksan", ...
  supplierName  String                           // "지오영", "복산", ...
  code          String    @db.Text               // Python 크롤러 코드 전체
  codeHash      String                           // SHA256(code) — 변경 감지
  isActive      Boolean   @default(true)         // 비활성화 시 배포 제외

  // 메타데이터
  supportsOrder Boolean   @default(false)        // 주문 지원 여부
  notes         String?   @db.Text               // 특이사항 (관리자 메모)

  createdAt     DateTime  @default(now())
  updatedAt     DateTime  @updatedAt
}
```

### 3-2. API 엔드포인트

```
파일: pharmsquare-server-main/src/routers/domae.ts
등록: apiRouter.use('/domae', domaeRouter)  // app.ts에 추가
```

#### GET /api/domae/crawlers

```typescript
// 크롤러 번들 배포 (핵심 API)
// 인증: API 키 (Authorization: Bearer dmk_free_xxx)

import { Router } from 'express';
import crypto from 'crypto';
import prisma from '../helpers/prismadb';

const router = Router();

// API 키 검증 미들웨어 (해시 기반 조회 — raw 키를 DB에 저장하지 않음)
async function validateDomaeApiKey(req, res, next) {
  const authHeader = req.headers.authorization;
  if (!authHeader?.startsWith('Bearer dmk_')) {
    return res.status(401).json({ error: 'API 키가 필요합니다.' });
  }

  const apiKey = authHeader.slice(7); // "Bearer " 제거
  const keyHash = crypto.createHash('sha256').update(apiKey).digest('hex');
  const record = await prisma.domaeApiKey.findUnique({
    where: { keyHash }
  });

  if (!record || !record.isActive) {
    return res.status(403).json({ error: '유효하지 않은 API 키입니다.' });
  }

  req.domaeApiKey = record;
  req.domaeApiKeyRaw = apiKey; // 번들 서명의 api_key_hash용
  next();
}

// GET /api/domae/crawlers
router.get('/crawlers', validateDomaeApiKey, async (req, res) => {
  const apiKeyRecord = req.domaeApiKey;

  // 활성 크롤러 전체 조회
  const crawlers = await prisma.domaeCrawler.findMany({
    where: { isActive: true },
    select: { name: true, code: true, codeHash: true }
  });

  // 번들 구성
  const crawlerMap = {};
  for (const c of crawlers) {
    crawlerMap[c.name] = c.code;
  }

  const now = new Date();
  const expiresAt = new Date(now.getTime() + 7 * 24 * 60 * 60 * 1000); // 7일 후

  const bundleData = {
    version: generateVersionString(crawlers),
    issued_at: now.toISOString(),
    expires_at: expiresAt.toISOString(),
    api_key_hash: apiKeyRecord.keyHash,
    crawlers: crawlerMap,
  };

  // Ed25519 서명 (payload 문자열의 SHA256 해시를 서명)
  const privateKey = Buffer.from(process.env.DOMAE_SIGNING_PRIVATE_KEY, 'base64');
  const payloadStr = JSON.stringify(bundleData);
  const payloadHash = crypto.createHash('sha256').update(payloadStr).digest();
  const signature = crypto.sign(null, payloadHash, {
    key: privateKey,
    format: 'der',
    type: 'pkcs8',
  });

  // 응답: {payload: "JSON문자열", signature: "base64"}
  res.json({
    payload: payloadStr,
    signature: signature.toString('base64'),
  });
});

function generateVersionString(crawlers) {
  // 모든 크롤러 해시를 합산하여 전체 버전 생성
  const combined = crawlers.map(c => c.codeHash).sort().join('');
  return crypto.createHash('sha256').update(combined).digest('hex').slice(0, 12);
}
```

#### GET /api/domae/crawlers/version

```typescript
// 버전 해시만 반환 (변경 체크용, 가벼움)
router.get('/crawlers/version', validateDomaeApiKey, async (req, res) => {
  const crawlers = await prisma.domaeCrawler.findMany({
    where: { isActive: true },
    select: { codeHash: true }
  });

  const version = generateVersionString(crawlers);
  res.json({ version });
});
```

#### POST /api/domae/verify

```typescript
// API 키 검증 + 사용자 정보 반환 (기존 api_key.py가 호출하던 것)
router.post('/verify', validateDomaeApiKey, async (req, res) => {
  const record = req.domaeApiKey;
  res.json({
    valid: true,
    tier: record.tier,
    pharmacy_name: record.pharmacyName,
    features: {
      min_interval: record.tier === 'free' ? 60 : record.tier === 'basic' ? 30 : 5,
      max_crawlers: 10,
      telegram: true,
      kakao: record.tier !== 'free',
    }
  });
});
```

#### POST /api/domae/heartbeat

```typescript
// 사용 통계 수집
router.post('/heartbeat', validateDomaeApiKey, async (req, res) => {
  const { search_count, order_count, active_monitors, version } = req.body;

  await prisma.domaeApiKey.update({
    where: { id: req.domaeApiKey.id },
    data: {
      lastHeartbeat: new Date(),
      lastVersion: version || null,
      searchCount: { increment: search_count || 0 },
      orderCount: { increment: order_count || 0 },
    }
  });

  res.json({ ok: true });
});
```

#### POST /api/domae/api-keys (어드민 전용)

```typescript
// API 키 발급 (팜스퀘어 어드민 또는 사용자 대시보드에서)
router.post('/api-keys', requireSession, async (req, res) => {
  const user = req.session.user;
  const { tier } = req.body;

  const rawKey = `dmk_${tier || 'free'}_${crypto.randomBytes(16).toString('hex')}`;
  const keyHash = crypto.createHash('sha256').update(rawKey).digest('hex');
  const keyPrefix = rawKey.slice(0, 16); // 식별용 앞 16자

  const record = await prisma.domaeApiKey.create({
    data: {
      keyHash,
      keyPrefix,
      tier: tier || 'free',
      pharmacyId: user.pharmacyId,
      pharmacyName: user.pharmacyName || '',
    }
  });

  // 키는 발급 시 이 응답에서만 전문 노출 (DB에 raw 키 저장 안 함)
  res.json({ api_key: rawKey, tier: record.tier });
});
```

### 3-3. 크롤러 관리 어드민

```
파일: pharmsquare-server-main/src/routers/domae-admin.ts
등록: apiRouter.use('/admin/domae', requireAdmin, domaeAdminRouter)

// 크롤러 관리 (마이팜 운영팀 전용)
GET    /api/admin/domae/crawlers          → 크롤러 목록
GET    /api/admin/domae/crawlers/:name    → 크롤러 코드 조회
PUT    /api/admin/domae/crawlers/:name    → 크롤러 코드 수정 (자동 해시 갱신)
POST   /api/admin/domae/crawlers         → 새 크롤러 추가
DELETE /api/admin/domae/crawlers/:name    → 크롤러 비활성화

// API 키 관리
GET    /api/admin/domae/api-keys          → 키 목록 + 통계
PUT    /api/admin/domae/api-keys/:id/revoke → 키 비활성화

// 대시보드 통계
GET    /api/admin/domae/stats             → 활성 사용자, 검색/주문 합계
```

**크롤러 수정 흐름**:

```
1. 어드민이 PUT /api/admin/domae/crawlers/geoweb 호출
   body: { code: "class GeoCrawler(BaseCrawler):..." }

2. 서버:
   - codeHash = SHA256(code) 계산
   - DB에 code + codeHash 업데이트
   - updatedAt 자동 갱신

3. 다음 번 로컬 클라이언트가 버전 체크 시:
   - GET /api/domae/crawlers/version → 새 해시 반환
   - 로컬 캐시 해시와 다름 → GET /api/domae/crawlers로 전체 다운로드
   - 새 크롤러 로드 → 즉시 반영

→ 어드민이 코드 수정하면 모든 사용자에게 자동 배포 (다음 체크 시)
```

---

## 4. 로컬 클라이언트 측 설계

### 4-1. 디렉토리 구조 변경

```
src/domae_mcp/core/crawlers/
├── __init__.py              # 변경: _register_all() 제거
├── base.py                  # 변경 없음 (BaseCrawler, SearchResult, OrderResult)
├── registry.py              # 변경: 동적 로드 방식으로 전환
└── loader.py                # 신규: 서버에서 크롤러 다운로드 + 캐시 + 서명검증

# 기존 크롤러 파일들은 Git에서 제거 (서버로 이동):
# ❌ geoweb.py, boksan.py, inchun.py, ...
# (개발/테스트용으로 로컬에 남겨둘 수 있지만 배포 패키지에서는 제외)

캐시 디렉토리 (런타임 생성):
~/.maipharm-domae-mcp/
├── config.json              # 기존 (API 키, 크리덴셜 등)
├── .key                     # 기존 (Fernet 키)
├── data/domae.db            # 기존 (SQLite)
└── crawlers/                # 신규 (서버에서 받은 크롤러 캐시)
    ├── bundle.json           # 서명된 번들 원본 (검증용 보관)
    ├── geoweb.py             # 번들에서 추출한 크롤러 코드
    ├── boksan.py
    └── ...
```

### 4-2. CrawlerLoader (신규 핵심 모듈)

검증 반영 사항:
- M1: `config.base_dir` 사용 (`config_dir` 아님)
- M2: 동기 httpx 사용 (MCP 모드 호환). 서버 lifespan에서는 `asyncio.to_thread()` 래핑
- M6: 크롤러 0개 로드 시 경고 로그
- M7: 캐시 파일 atomic write (temp → rename)
- C1: 서명 검증 시 payload 원문 바이트 그대로 사용 (재직렬화 안 함)

```python
# src/domae_mcp/core/crawlers/loader.py

"""크롤러 서버 배포 로더: 서버에서 크롤러 번들을 다운로드하고 검증/캐시/로드"""

import base64
import hashlib
import importlib.util
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from domae_mcp.core.crawlers.base import BaseCrawler

logger = logging.getLogger(__name__)

# ─── 상수 ──────────────────────────────────────────
SERVER_URL = "https://api.pharmsq.com/api/domae"
CACHE_DIR_NAME = "crawlers"
BUNDLE_FILE = "bundle.json"
OFFLINE_GRACE_DAYS = 7
# 버전 체크는 앱 시작 시마다 수행 (별도 간격 제어 없음)
# 앱 실행 중 주기적 체크가 필요하면 추후 스케줄러에서 loader.check_update() 호출

# Ed25519 공개키 (소스코드에 하드코딩 — 공개키이므로 안전)
PUBLIC_KEY_B64 = "TO_BE_GENERATED"  # 키 생성 후 교체


class CrawlerLoader:
    """서버에서 크롤러 코드를 받아 로컬에서 실행.

    흐름:
    1. load() 호출 (동기 — MCP 모드 호환)
    2. 캐시에 유효한 번들이 있으면 캐시에서 로드
    3. 없거나 만료되었으면 서버에서 다운로드
    4. 서명 검증 → 캐시 저장 → 동적 import → 크롤러 클래스 반환

    async 컨텍스트(FastAPI lifespan)에서는 asyncio.to_thread(loader.load)로 호출.
    """

    def __init__(self, base_dir: Path, api_key: str):
        """
        Args:
            base_dir: ~/.maipharm-domae-mcp/ 경로 (ConfigManager.base_dir)
            api_key: "dmk_free_xxx" 형태의 API 키
        """
        self._base_dir = base_dir
        self._api_key = api_key
        self._cache_dir = base_dir / CACHE_DIR_NAME
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._bundle_path = self._cache_dir / BUNDLE_FILE
        self._api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()

    def load(self) -> dict[str, type[BaseCrawler]]:
        """크롤러 클래스들을 로드하여 반환.

        Returns:
            {"지오영": GeoCrawler, "복산": BoksanCrawler, ...}

        Raises:
            RuntimeError: 서버 연결 실패 + 캐시도 만료된 경우
        """
        # 1. 서버에서 최신 번들 시도
        bundle = self._fetch_from_server()

        if bundle is None:
            # 2. 서버 실패 → 캐시 사용
            bundle = self._load_from_cache()

        if bundle is None:
            raise RuntimeError(
                "크롤러를 로드할 수 없습니다. "
                "인터넷 연결을 확인하고 API 키가 유효한지 확인하세요."
            )

        # 3. 크롤러 코드를 파일로 저장 + 동적 import
        result = self._import_crawlers(bundle)

        # M6: 번들에 크롤러가 있는데 0개 로드 시 경고
        expected = len(bundle.get("crawlers", {}))
        if expected > 0 and len(result) == 0:
            logger.error("번들에 %d개 크롤러가 있지만 모두 import 실패!", expected)

        return result

    def check_update(self) -> bool:
        """서버에 버전 변경 여부만 확인 (가벼운 체크)."""
        try:
            resp = httpx.get(
                f"{SERVER_URL}/crawlers/version",
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=5.0,
            )
            if resp.status_code == 200:
                server_version = resp.json().get("version")
                cached = self._load_cached_bundle()
                if cached and cached.get("version") == server_version:
                    return False
                return True
        except Exception:
            pass
        return False

    # ─── 서버 통신 ──────────────────────────────

    def _fetch_from_server(self) -> Optional[dict]:
        """서버에서 크롤러 번들 다운로드 + 서명 검증."""
        try:
            # N7 수정: 캐시된 response를 파싱한 뒤 만료 체크
            cached_response = self._load_cached_bundle()
            if cached_response:
                cached_bundle = self._verify_and_parse(cached_response)
                if cached_bundle and not self._is_expired(cached_bundle):
                    if not self.check_update():
                        logger.debug("크롤러 캐시 유효, 서버 동일 버전")
                        return cached_bundle

            # 전체 번들 다운로드
            resp = httpx.get(
                f"{SERVER_URL}/crawlers",
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=30.0,
            )

            if resp.status_code == 401:
                logger.error("API 키가 유효하지 않습니다.")
                return None
            if resp.status_code == 403:
                logger.error("API 키가 비활성화되었습니다.")
                return None
            if resp.status_code != 200:
                logger.warning("서버 응답 오류: %d", resp.status_code)
                return None

            response = resp.json()

            # C1 수정: 서명 검증 (payload 원문 바이트 사용)
            bundle = self._verify_and_parse(response)
            if bundle is None:
                logger.error("번들 서명 검증 실패 — 변조 가능성")
                return None

            # API 키 해시 검증
            if bundle.get("api_key_hash") != self._api_key_hash:
                logger.error("번들이 이 API 키용이 아닙니다")
                return None

            # M7: 캐시에 atomic write
            self._save_to_cache(response)  # 원본 response 저장 (signature 포함)
            logger.info("크롤러 번들 다운로드 완료 (v%s)", bundle.get("version"))
            return bundle

        except httpx.HTTPError as e:
            logger.warning("서버 연결 실패: %s", e)
            return None

    # ─── 서명 검증 (C1 수정) ──────────────────────

    def _verify_and_parse(self, response: dict) -> Optional[dict]:
        """서버 응답의 서명을 검증하고 번들 데이터를 파싱.

        response 구조: {"payload": "JSON문자열", "signature": "base64"}
        payload 원문 바이트를 그대로 SHA256하여 검증 (재직렬화 안 함).
        """
        try:
            payload_str = response.get("payload")
            signature_b64 = response.get("signature")
            if not payload_str or not signature_b64:
                return None

            # 원문 바이트의 SHA256 해시
            payload_hash = hashlib.sha256(payload_str.encode()).digest()
            signature = base64.b64decode(signature_b64)
            public_key = Ed25519PublicKey.from_public_bytes(
                base64.b64decode(PUBLIC_KEY_B64)
            )

            public_key.verify(signature, payload_hash)

            # 검증 통과 → JSON 파싱
            return json.loads(payload_str)

        except Exception as e:
            logger.error("서명 검증 에러: %s", e)
            return None

    # ─── 캐시 관리 (M7: atomic write) ─────────────

    def _save_to_cache(self, response: dict) -> None:
        """번들을 로컬 캐시에 atomic write (temp → rename)."""
        content = json.dumps(response, ensure_ascii=False)
        fd, tmp_path = tempfile.mkstemp(dir=str(self._cache_dir), suffix=".tmp")
        try:
            os.write(fd, content.encode("utf-8"))
        finally:
            os.close(fd)
        try:
            os.replace(tmp_path, str(self._bundle_path))
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    def _load_from_cache(self) -> Optional[dict]:
        """캐시에서 유효한 번들 로드. 만료 시 None."""
        cached_response = self._load_cached_bundle()
        if cached_response is None:
            return None

        # 캐시된 response에서 서명 재검증 + 파싱
        bundle = self._verify_and_parse(cached_response)
        if bundle is None:
            logger.warning("캐시된 번들 서명 검증 실패")
            return None

        if self._is_expired(bundle):
            logger.warning("캐시된 크롤러 번들이 만료되었습니다.")
            return None

        logger.info("오프라인 모드: 캐시된 크롤러 사용 (v%s)", bundle.get("version"))
        return bundle

    def _load_cached_bundle(self) -> Optional[dict]:
        """캐시 파일 읽기 (만료/서명 체크 안 함)."""
        if not self._bundle_path.exists():
            return None
        try:
            return json.loads(self._bundle_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _is_expired(self, bundle: dict) -> bool:
        """번들 만료 여부. expires_at + OFFLINE_GRACE_DAYS 적용."""
        expires_at_str = bundle.get("expires_at")
        if not expires_at_str:
            return True
        try:
            expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
            grace_deadline = expires_at + timedelta(days=OFFLINE_GRACE_DAYS)
            return datetime.now(timezone.utc) > grace_deadline
        except (ValueError, TypeError):
            return True

    # ─── 동적 import ──────────────────────────────

    def _import_crawlers(self, bundle: dict) -> dict[str, type[BaseCrawler]]:
        """번들의 크롤러 코드를 .py 파일로 저장하고 동적 import.

        크롤러 코드의 하드 요구사항 (Info 반영):
        1. 반드시 `from domae_mcp.core.crawlers.base import BaseCrawler, SearchResult, OrderResult` 사용
        2. domae_mcp 패키지가 pip install 되어 있어야 import 성공
        3. 상대 import 사용 금지 (캐시 디렉토리에서 로드되므로)
        4. requirements.txt에 포함된 패키지만 사용 가능 (새 의존성 추가 시 requirements.txt 먼저 업데이트)

        Returns:
            {"지오영": GeoCrawler, ...} 형태의 dict
        """
        crawlers_code = bundle.get("crawlers", {})
        loaded = {}

        for module_name, code in crawlers_code.items():
            try:
                # 1. M7: 캐시 디렉토리에 atomic write (N3 패턴: try/finally)
                file_path = self._cache_dir / f"{module_name}.py"
                fd, tmp = tempfile.mkstemp(dir=str(self._cache_dir), suffix=".tmp")
                try:
                    os.write(fd, code.encode("utf-8"))
                finally:
                    os.close(fd)
                os.replace(tmp, str(file_path))

                # 2. importlib로 동적 로드
                spec = importlib.util.spec_from_file_location(
                    f"domae_crawlers.{module_name}", str(file_path)
                )
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                spec.loader.exec_module(module)

                # 3. BaseCrawler 서브클래스 찾기
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if (
                        isinstance(attr, type)
                        and issubclass(attr, BaseCrawler)
                        and attr is not BaseCrawler
                    ):
                        supplier_name = getattr(attr, "SUPPLIER_NAME", module_name)
                        loaded[supplier_name] = attr
                        logger.debug("크롤러 로드: %s (%s)", supplier_name, module_name)
                        break

            except Exception as e:
                logger.error("크롤러 로드 실패 [%s]: %s", module_name, e)

        logger.info("크롤러 %d/%d개 로드 완료", len(loaded), len(crawlers_code))
        return loaded
```

### 4-3. CrawlerRegistry 변경

```python
# src/domae_mcp/core/crawlers/registry.py (변경)

"""크롤러 레지스트리: 동적 로드 방식"""

import logging
from typing import Optional

from domae_mcp.core.crawlers.base import BaseCrawler

logger = logging.getLogger(__name__)


class CrawlerRegistry:
    """크롤러 레지스트리. CrawlerLoader에서 로드한 크롤러를 관리.

    기존: _register_all()로 정적 import
    변경: load_from_server()로 서버에서 동적 로드

    SearchService, OrderService는 이전과 동일하게
    CrawlerRegistry.get(name)으로 크롤러를 가져옴 → 변경 불필요.
    """

    _crawlers: dict[str, type[BaseCrawler]] = {}
    _loaded: bool = False

    @classmethod
    def register(cls, name: str, crawler_class: type[BaseCrawler]) -> None:
        """크롤러 등록."""
        cls._crawlers[name] = crawler_class
        logger.debug("크롤러 등록: %s", name)

    @classmethod
    def register_all(cls, crawlers: dict[str, type[BaseCrawler]]) -> None:
        """CrawlerLoader에서 로드한 크롤러를 일괄 등록."""
        cls._crawlers = crawlers
        cls._loaded = True
        logger.info("크롤러 %d개 일괄 등록", len(crawlers))

    @classmethod
    def get(cls, name: str) -> BaseCrawler:
        """크롤러 인스턴스 생성. 없으면 KeyError."""
        if name not in cls._crawlers:
            raise KeyError(f"등록되지 않은 크롤러: {name}")
        return cls._crawlers[name]()

    @classmethod
    def list_all(cls) -> list[str]:
        """등록된 크롤러 이름 목록."""
        return list(cls._crawlers.keys())

    @classmethod
    def get_all(cls) -> dict[str, BaseCrawler]:
        """모든 크롤러 인스턴스 생성."""
        return {name: cls._crawlers[name]() for name in cls._crawlers}

    @classmethod
    def is_loaded(cls) -> bool:
        """크롤러가 로드되었는지 여부."""
        return cls._loaded
```

### 4-4. 앱 시작 흐름 변경 (M2 수정: asyncio.to_thread 사용)

```python
# src/domae_mcp/local/server.py (변경)

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from domae_mcp.local.config import ConfigManager
from domae_mcp.local.database import init_db
from domae_mcp.core.crawlers.loader import CrawlerLoader
from domae_mcp.core.crawlers.registry import CrawlerRegistry

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """앱 시작/종료 이벤트"""
    config = ConfigManager()
    init_db(config)

    # M2: CrawlerLoader는 동기 httpx 사용 → asyncio.to_thread로 래핑
    api_key = config.get_api_key()
    loader = None
    if api_key:
        try:
            loader = CrawlerLoader(config.base_dir, api_key)  # M1: base_dir
            crawlers = await asyncio.to_thread(loader.load)
            CrawlerRegistry.register_all(crawlers)
        except RuntimeError as e:
            logger.warning("크롤러 로드 실패: %s", e)
    else:
        logger.warning("API 키 미설정 — 크롤러 없이 시작")

    app.state.config = config
    app.state.crawler_loader = loader

    yield


app = FastAPI(
    title="maipharm-domae-mcp",
    description="의약품 도매 통합 검색/주문 로컬 서버",
    version="1.0.0",
    lifespan=lifespan,
)

# ... 라우터 마운트 (기존과 동일)
```

### 4-5. MCP 서버 시작 흐름 변경

```python
# src/domae_mcp/local/mcp_server.py — _init_services() 수정
# MCP 모드는 동기 컨텍스트이므로 loader.load() 직접 호출 가능

def _init_services():
    global _config, _session_factory, _search_service, _order_service, _monitor_service

    _config = ConfigManager()

    # 크롤러 로드 (MCP 모드 — 동기)
    api_key = _config.get_api_key()
    if api_key:
        from domae_mcp.core.crawlers.loader import CrawlerLoader
        from domae_mcp.core.crawlers.registry import CrawlerRegistry
        loader = CrawlerLoader(_config.base_dir, api_key)  # M1: base_dir
        try:
            crawlers = loader.load()
            CrawlerRegistry.register_all(crawlers)
        except RuntimeError:
            pass  # 오프라인 + 캐시 만료 시

    # 이하 기존과 동일...
```

---

## 5. 캐시 정책

```
┌─────────────────────────────────────────────────────────────┐
│  캐시 생명주기                                                │
│                                                              │
│  서버 다운로드 ──► 로컬 캐시 저장                              │
│       │              │                                       │
│       │         expires_at까지                                │
│       │         정상 사용                                     │
│       │              │                                       │
│       │         expires_at 경과                               │
│       │              │                                       │
│       │         + 7일 유예 (OFFLINE_GRACE_DAYS)               │
│       │              │                                       │
│       │         유예 만료 → 크롤러 로드 불가                   │
│       │                    "인터넷 연결 필요" 안내              │
│       │                                                      │
│       └── 앱 시작 시마다 버전 체크 (check_update)              │
│           변경 있으면 → 전체 번들 재다운로드                    │
│           변경 없으면 → 캐시 사용                              │
└─────────────────────────────────────────────────────────────┘
```

| 상황 | 동작 |
|------|------|
| 첫 실행 (캐시 없음) | 서버 다운로드 필수. 실패 시 크롤러 0개 |
| 정상 (온라인) | 앱 시작 시 버전 체크 → 변경 시 다운로드 |
| 네트워크 끊김 | 캐시 사용 (expires_at(+7일) + 유예 7일 = 최대 14일) |
| 캐시 만료 | 서버 다운로드 필수. 실패 시 크롤러 0개 |
| API 키 해지 | 서버 403 → 캐시 만료까지만 사용 가능 |
| 크롤러 업데이트 | 어드민이 서버 수정 → 다음 버전 체크 시 자동 반영 |

---

## 6. 기존 코드 영향 범위

```
변경 필요:
  src/domae_mcp/core/crawlers/
    registry.py     — register_all() 추가, _register_all() 제거
    __init__.py     — 정적 import 제거
    loader.py       — 신규 파일

  src/domae_mcp/local/
    server.py       — lifespan에 CrawlerLoader 추가
    mcp_server.py   — _init_services()에 CrawlerLoader 추가
    api_key.py      — CrawlerLoader로 역할 이관, 단순화 또는 제거

최소 변경:
  src/domae_mcp/core/crawlers/base.py        — 변경 없음
  src/domae_mcp/core/services/search_service.py — CrawlerRegistry.is_loaded() 체크 추가
  src/domae_mcp/core/services/order_service.py  — CrawlerRegistry.is_loaded() 체크 추가

변경 없음:
  src/domae_mcp/local/routers/*              — 전부 변경 없음
  frontend/*                                 — 전부 변경 없음

삭제 (서버로 이동):
  src/domae_mcp/core/crawlers/geoweb.py
  src/domae_mcp/core/crawlers/boksan.py
  src/domae_mcp/core/crawlers/inchun.py
  src/domae_mcp/core/crawlers/tjpharm.py
  src/domae_mcp/core/crawlers/hmpmall.py
  src/domae_mcp/core/crawlers/beakje.py
  src/domae_mcp/core/crawlers/picomall.py
  src/domae_mcp/core/crawlers/saeropharm.py
  src/domae_mcp/core/crawlers/sdpharm.py
  src/domae_mcp/core/crawlers/upharmmall.py
```

---

## 7. 키 생성 (1회 작업)

### Ed25519 키페어 생성

```bash
# Python으로 생성
python -c "
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
import base64

private_key = Ed25519PrivateKey.generate()

# 개인키 (서버 환경변수용)
private_bytes = private_key.private_bytes(
    encoding=serialization.Encoding.DER,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption()
)
print('PRIVATE (서버 .env):')
print(base64.b64encode(private_bytes).decode())

# 공개키 (클라이언트 소스코드용)
public_bytes = private_key.public_key().public_bytes(
    encoding=serialization.Encoding.Raw,
    format=serialization.PublicFormat.Raw
)
print()
print('PUBLIC (loader.py PUBLIC_KEY_B64):')
print(base64.b64encode(public_bytes).decode())
"
```

### 서버 환경변수 추가

```env
# pharmsquare-server-main/.env
DOMAE_SIGNING_PRIVATE_KEY=MC4CAQAwBQYDK2VwBCIEI...  # 위에서 생성한 개인키
```

---

## 8. 시퀀스 다이어그램

### 첫 실행 (캐시 없음)

```
사용자                  로컬 앱                    팜스퀘어 서버
  │                       │                           │
  │  python -m domae_mcp  │                           │
  │──────────────────────▶│                           │
  │                       │                           │
  │                       │  GET /api/domae/crawlers  │
  │                       │  Authorization: Bearer dmk_free_xxx
  │                       │──────────────────────────▶│
  │                       │                           │ API키 검증
  │                       │                           │ 크롤러 코드 조회
  │                       │                           │ Ed25519 서명
  │                       │      서명된 번들 응답       │
  │                       │◀──────────────────────────│
  │                       │                           │
  │                       │ 서명 검증 ✅               │
  │                       │ api_key_hash 검증 ✅       │
  │                       │ 캐시 저장                  │
  │                       │ 동적 import                │
  │                       │ Registry에 등록            │
  │                       │                           │
  │  localhost:5900 준비    │                           │
  │◀──────────────────────│                           │
```

### 크롤러 업데이트 (어드민 → 사용자)

```
어드민                  팜스퀘어 서버              사용자 로컬 앱
  │                       │                           │
  │  PUT /admin/domae/    │                           │
  │  crawlers/geoweb      │                           │
  │  {code: "새 코드..."}  │                           │
  │──────────────────────▶│                           │
  │                       │ codeHash 갱신              │
  │     완료               │                           │
  │◀──────────────────────│                           │
  │                       │                           │
  │                       │      (6시간 내)            │
  │                       │                           │
  │                       │  GET /crawlers/version    │
  │                       │◀──────────────────────────│
  │                       │                           │
  │                       │  {version: "새 해시"}      │
  │                       │──────────────────────────▶│
  │                       │                           │ 로컬 캐시와 다름!
  │                       │  GET /crawlers            │
  │                       │◀──────────────────────────│
  │                       │                           │
  │                       │  새 번들 응답              │
  │                       │──────────────────────────▶│
  │                       │                           │ 검증 + 캐시 + 리로드
  │                       │                           │ → 새 크롤러 적용 ✅
```

---

## 9. API 키 발급 UI (팜스퀘어 프론트엔드)

```
팜스퀘어 대시보드 > 도매 통합검색 메뉴:

┌──────────────────────────────────────────────────┐
│  도매 통합검색 API 키                              │
│                                                    │
│  현재 등급: Free                                   │
│  API 키: dmk_free_a1b2c3d4e5f6g7h8               │
│          [복사] [재발급]                           │
│                                                    │
│  사용법:                                          │
│  1. 마이팜 도매 앱 설치                            │
│  2. 설정 > API 키에 위 키를 입력                   │
│  3. 도매 계정을 등록하면 검색/주문 가능             │
│                                                    │
│  사용 통계:                                       │
│  마지막 활동: 2시간 전                             │
│  총 검색: 142회 | 총 주문: 23회                    │
│  앱 버전: 1.0.0                                   │
│                                                    │
│  ──────────────────────────────────                │
│  클라우드 버전으로 업그레이드하면                    │
│  24/7 자동 검색 + 자동 주문이 가능합니다            │
│  [클라우드 알아보기 →]                             │
└──────────────────────────────────────────────────┘
```

---

## 10. 구현 순서

```
Step 1 (서버): Prisma 스키마 + 마이그레이션              (0.5일)
Step 2 (서버): Ed25519 키 생성 + 환경변수 설정            (0.5시간)
Step 3 (서버): /api/domae/* 라우터 구현                  (1일)
Step 4 (서버): 기존 크롤러 10개 코드를 DB에 시딩           (0.5일)
Step 5 (로컬): loader.py 구현                           (1일)
Step 6 (로컬): registry.py 변경 + 기존 크롤러 파일 제거    (0.5일)
Step 7 (로컬): server.py, mcp_server.py 시작 흐름 변경   (0.5일)
Step 8 (통합): 전체 흐름 테스트                          (0.5일)
Step 9 (서버): 어드민 API + 크롤러 관리 페이지             (1일)
Step 10(서버): API 키 발급 UI (팜스퀘어 프론트)           (1일)
─────────────────────────────────────────────────────
합계: 약 6~7일
```

---

## 11. 위험 요소

| 위험 | 대응 |
|------|------|
| 팜스퀘어 서버 장애 시 전체 사용자 영향 | 캐시 최대 14일 유예 (만료7일+유예7일) + 서버 모니터링 |
| 크롤러 코드에 BaseCrawler import 경로 문제 | 크롤러 코드 첫 줄에 `from domae_mcp.core.crawlers.base import ...` 유지 |
| 번들 크기가 커서 다운로드 느림 | 10개 크롤러 × ~200줄 = ~40KB 수준. 문제 없음 |
| 공개키 교체 시 기존 캐시 무효화 | 공개키 버전 관리 + 키 교체 시 번들 재서명 |
| exec/import 보안 우려 | 서명 검증으로 신뢰 확보. 서버가 보낸 코드만 실행 |
