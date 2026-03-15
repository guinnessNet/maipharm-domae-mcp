# 팜스퀘어 도매 API 키 발급 시스템 구축 계획

> domae-mcp 로컬 사용자를 위한 API 키 발급/검증을 팜스퀘어에 통합하는 계획

---

## 1. 현재 상태

### 팜스퀘어 기술 스택
```
백엔드:  pharmsquare-server-main (Express.js + Prisma + PostgreSQL + Redis)
프론트:  pharmsquare-next-main (Next.js 15 + MUI)
인증:    세션 기반 (connect.sid + Redis)
구독:    Subscription (plan: basic/pro/enterprise/free)
```

### domae-mcp가 필요로 하는 것
```
1. API 키 발급 — 팜스퀘어 사용자가 도매 API 키를 받음
2. API 키 검증 — 로컬 앱 시작 시 키가 유효한지 확인
3. 하트비트 — 12시간마다 사용 통계 수신
4. 요금제 확인 — free/basic/pro 등급에 따른 기능 제어
```

---

## 2. 전체 아키텍처

```
┌─────────────────────────────┐
│  팜스퀘어 프론트 (Next.js)    │
│                               │
│  마이페이지 → "도매 API 키"   │
│  ┌─────────────────────────┐ │
│  │ API 키: dmk_free_xxxx   │ │
│  │ [복사] [재발급]          │ │
│  │                         │ │
│  │ 상태: 활성               │ │
│  │ 발급일: 2026-03-15      │ │
│  │ 마지막 사용: 2분 전      │ │
│  └─────────────────────────┘ │
└──────────────┬──────────────┘
               │ API 호출
               ▼
┌─────────────────────────────┐
│  팜스퀘어 백엔드 (Express)    │
│                               │
│  POST /api/domae/keys        │ ← 키 발급
│  GET  /api/domae/keys        │ ← 내 키 조회
│  DELETE /api/domae/keys/:id  │ ← 키 비활성화
│  POST /api/domae/keys/:id/   │
│        regenerate            │ ← 키 재발급
│                               │
│  ── 외부 공개 (인증 없음) ──   │
│  GET  /api/domae/verify      │ ← 키 검증 (로컬 앱용)
│  POST /api/domae/heartbeat   │ ← 하트비트 (로컬 앱용)
│                               │
└──────────────┬──────────────┘
               │ Prisma
               ▼
┌─────────────────────────────┐
│  PostgreSQL                  │
│  ┌─────────────────────────┐│
│  │ DomaeApiKey 테이블       ││
│  │ DomaeHeartbeat 테이블    ││
│  └─────────────────────────┘│
└─────────────────────────────┘
               ▲
               │ GET /api/domae/verify
┌──────────────┴──────────────┐
│  로컬 domae-mcp 앱           │
│  (약국 PC)                    │
│  시작 시 키 검증 → 사용       │
│  12시간마다 하트비트           │
└─────────────────────────────┘
```

---

## 3. DB 스키마 (Prisma)

### pharmsquare-server-main의 schema.prisma에 추가

```prisma
// ── 도매 API 키 ──

model DomaeApiKey {
  id          String   @id @default(cuid())
  createdAt   DateTime @default(now())
  updatedAt   DateTime @updatedAt

  // 소유자
  userId      String
  user        User     @relation(fields: [userId], references: [id])
  pharmacyId  String?
  pharmacy    Pharmacy? @relation(fields: [pharmacyId], references: [id])

  // 키 정보
  key         String   @unique              // dmk_free_a1b2c3...
  tier        DomaeTier @default(free)      // free, basic, pro
  active      Boolean  @default(true)

  // 사용 추적
  lastUsedAt  DateTime?
  lastIp      String?
  useCount    Int      @default(0)          // verify 호출 횟수

  // 하트비트 통계
  heartbeats  DomaeHeartbeat[]
}

model DomaeHeartbeat {
  id          String   @id @default(cuid())
  createdAt   DateTime @default(now())

  apiKeyId    String
  apiKey      DomaeApiKey @relation(fields: [apiKeyId], references: [id])

  // 사용 통계
  version     String                        // domae-mcp 버전
  searchCount Int      @default(0)          // 검색 횟수
  orderCount  Int      @default(0)          // 주문 횟수
  activeMonitors Int   @default(0)          // 모니터링 제품 수
  ip          String?
}

enum DomaeTier {
  free        // 무료 (로컬)
  basic       // 베이직 (클라우드 5만)
  pro         // 프로 (클라우드 10만)
}
```

### User 모델에 관계 추가

```prisma
model User {
  // ... 기존 필드 ...
  domaeApiKeys DomaeApiKey[]
}

model Pharmacy {
  // ... 기존 필드 ...
  domaeApiKeys DomaeApiKey[]
}
```

---

## 4. 백엔드 API 구현

### 4.1 파일 구조

```
pharmsquare-server-main/src/
├── routers/
│   └── domae.ts                  # 도매 API 키 라우터 (신규)
├── lib/
│   └── domae/
│       ├── keyGenerator.ts       # API 키 생성 유틸
│       └── tierResolver.ts       # 구독 → 도매 등급 변환
```

### 4.2 라우터: src/routers/domae.ts

```typescript
import { Router } from "express";
import { PrismaClient } from "@prisma/client";
import crypto from "crypto";

const router = Router();
const prisma = new PrismaClient();

// ── 키 생성 유틸 ──

function generateApiKey(tier: string): string {
  const random = crypto.randomBytes(16).toString("hex");
  return `dmk_${tier}_${random}`;
}

function resolveTier(subscription: any): "free" | "basic" | "pro" {
  if (!subscription || subscription.status !== "active") return "free";
  if (subscription.plan === "pro" || subscription.plan === "enterprise") return "pro";
  if (subscription.plan === "basic") return "basic";
  return "free";
}

// ══════════════════════════════════════════
//  인증 필요 (팜스퀘어 로그인 사용자용)
// ══════════════════════════════════════════

// POST /api/domae/keys — API 키 발급
router.post("/keys", async (req, res) => {
  const user = req.session?.user;
  if (!user) return res.status(401).json({ error: "로그인이 필요합니다." });

  // 이미 활성 키가 있는지 확인
  const existing = await prisma.domaeApiKey.findFirst({
    where: { userId: user.id, active: true },
  });
  if (existing) {
    return res.status(400).json({
      error: "이미 발급된 API 키가 있습니다.",
      key: existing.key,
    });
  }

  // 구독 상태로 tier 결정
  const subscription = await prisma.subscription.findFirst({
    where: { pharmacyId: user.pharmacyId, status: "active" },
    orderBy: { createdAt: "desc" },
  });
  const tier = resolveTier(subscription);

  const apiKey = await prisma.domaeApiKey.create({
    data: {
      userId: user.id,
      pharmacyId: user.pharmacyId,
      key: generateApiKey(tier),
      tier,
    },
  });

  res.json({ key: apiKey.key, tier: apiKey.tier });
});

// GET /api/domae/keys — 내 API 키 조회
router.get("/keys", async (req, res) => {
  const user = req.session?.user;
  if (!user) return res.status(401).json({ error: "로그인이 필요합니다." });

  const keys = await prisma.domaeApiKey.findMany({
    where: { userId: user.id },
    orderBy: { createdAt: "desc" },
    include: {
      _count: { select: { heartbeats: true } },
    },
  });

  res.json({
    keys: keys.map((k) => ({
      id: k.id,
      key: k.key,
      tier: k.tier,
      active: k.active,
      createdAt: k.createdAt,
      lastUsedAt: k.lastUsedAt,
      useCount: k.useCount,
    })),
  });
});

// DELETE /api/domae/keys/:id — 키 비활성화
router.delete("/keys/:id", async (req, res) => {
  const user = req.session?.user;
  if (!user) return res.status(401).json({ error: "로그인이 필요합니다." });

  await prisma.domaeApiKey.updateMany({
    where: { id: req.params.id, userId: user.id },
    data: { active: false },
  });

  res.json({ success: true });
});

// POST /api/domae/keys/:id/regenerate — 키 재발급
router.post("/keys/:id/regenerate", async (req, res) => {
  const user = req.session?.user;
  if (!user) return res.status(401).json({ error: "로그인이 필요합니다." });

  const existing = await prisma.domaeApiKey.findFirst({
    where: { id: req.params.id, userId: user.id },
  });
  if (!existing) return res.status(404).json({ error: "키를 찾을 수 없습니다." });

  // 기존 키 비활성화 + 새 키 생성
  await prisma.domaeApiKey.update({
    where: { id: existing.id },
    data: { active: false },
  });

  const newKey = await prisma.domaeApiKey.create({
    data: {
      userId: user.id,
      pharmacyId: user.pharmacyId,
      key: generateApiKey(existing.tier),
      tier: existing.tier,
    },
  });

  res.json({ key: newKey.key, tier: newKey.tier });
});

// ══════════════════════════════════════════
//  인증 불필요 (로컬 domae-mcp 앱이 호출)
// ══════════════════════════════════════════

// GET /api/domae/verify?key=dmk_free_xxxx — API 키 검증
router.get("/verify", async (req, res) => {
  const { key } = req.query;
  if (!key || typeof key !== "string") {
    return res.status(400).json({ valid: false, error: "API 키가 필요합니다." });
  }

  const apiKey = await prisma.domaeApiKey.findUnique({
    where: { key },
    include: {
      user: { select: { name: true, email: true } },
      pharmacy: { select: { name: true } },
    },
  });

  if (!apiKey || !apiKey.active) {
    return res.status(401).json({ valid: false, error: "유효하지 않은 API 키입니다." });
  }

  // 사용 기록 업데이트
  await prisma.domaeApiKey.update({
    where: { id: apiKey.id },
    data: {
      lastUsedAt: new Date(),
      lastIp: req.ip,
      useCount: { increment: 1 },
    },
  });

  res.json({
    valid: true,
    tier: apiKey.tier,
    pharmacy_name: apiKey.pharmacy?.name || "",
    user_name: apiKey.user?.name || "",
    features: {
      min_interval: apiKey.tier === "pro" ? 5 : apiKey.tier === "basic" ? 30 : 60,
      max_crawlers: 8,
      telegram: true,
      kakao: apiKey.tier !== "free",
    },
  });
});

// POST /api/domae/heartbeat — 사용 통계 수신
router.post("/heartbeat", async (req, res) => {
  const { key, version, search_count, order_count, active_monitors } = req.body;

  if (!key) {
    return res.status(400).json({ error: "API 키가 필요합니다." });
  }

  const apiKey = await prisma.domaeApiKey.findUnique({ where: { key } });
  if (!apiKey || !apiKey.active) {
    return res.status(401).json({ error: "유효하지 않은 API 키입니다." });
  }

  await prisma.domaeHeartbeat.create({
    data: {
      apiKeyId: apiKey.id,
      version: version || "unknown",
      searchCount: search_count || 0,
      orderCount: order_count || 0,
      activeMonitors: active_monitors || 0,
      ip: req.ip,
    },
  });

  // lastUsedAt 갱신
  await prisma.domaeApiKey.update({
    where: { id: apiKey.id },
    data: { lastUsedAt: new Date() },
  });

  res.json({ success: true });
});

export default router;
```

### 4.3 라우터 등록

```typescript
// pharmsquare-server-main/src/app.ts (또는 index.ts)에 추가:

import domaeRouter from "./routers/domae";
app.use("/api/domae", domaeRouter);
```

---

## 5. 프론트엔드 구현

### 5.1 파일 구조

```
pharmsquare-next-main/src/app/(afterLogin)/
└── settings/
    └── domae/
        └── page.tsx              # 도매 API 키 관리 페이지
```

### 5.2 페이지: settings/domae/page.tsx

```
┌──────────────────────────────────────────────────┐
│  설정 > 도매 통합검색 API 키                       │
│                                                    │
│  ┌────────────────────────────────────────────┐   │
│  │                                            │   │
│  │  내 API 키                                 │   │
│  │  ┌──────────────────────────────────────┐  │   │
│  │  │ dmk_free_a1b2c3d4e5f6g7h8i9j0k1l2  │  │   │
│  │  └──────────────────────────────────────┘  │   │
│  │  [복사]  [재발급]                          │   │
│  │                                            │   │
│  │  등급: 무료 (로컬 전용)                     │   │
│  │  발급일: 2026-03-15                        │   │
│  │  마지막 사용: 2분 전                        │   │
│  │  총 사용 횟수: 142회                        │   │
│  │                                            │   │
│  └────────────────────────────────────────────┘   │
│                                                    │
│  ┌────────────────────────────────────────────┐   │
│  │  도매 통합검색이란?                         │   │
│  │                                            │   │
│  │  8개 도매상의 재고를 한 번에 검색하고,       │   │
│  │  최저가로 주문할 수 있는 프로그램입니다.      │   │
│  │                                            │   │
│  │  [설치 가이드 보기]  [GitHub에서 다운로드]    │   │
│  └────────────────────────────────────────────┘   │
│                                                    │
│  ┌────────────────────────────────────────────┐   │
│  │  클라우드 서비스로 업그레이드                  │   │
│  │                                            │   │
│  │  24시간 자동 모니터링, 카카오톡 알림 등       │   │
│  │  [베이직 5만/월]  [프로 10만/월]             │   │
│  └────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────┘
```

### 5.3 사이드바 메뉴에 추가

```
팜스퀘어 사이드바:
  > 대시보드
  > 처방전
  > 환자관리
  > 재고관리
  > ──────────
  > 설정
      > 약국 정보
      > 직원 관리
      > 도매 통합검색 ← 신규 추가
      > 구독/결제
```

---

## 6. domae-mcp 쪽 URL 변경

현재 domae-mcp의 api_key.py:

```python
VERIFY_URL = "https://api.domae.kr/api/verify"
HEARTBEAT_URL = "https://api.domae.kr/api/heartbeat"
```

팜스퀘어에 통합하면:

```python
VERIFY_URL = "https://api.pharmsquare.com/api/domae/verify"
HEARTBEAT_URL = "https://api.pharmsquare.com/api/domae/heartbeat"
```

> 또는 `api.domae.kr` → `api.pharmsquare.com/api/domae`로 리다이렉트 설정

---

## 7. 구현 순서

```
Step 1: DB 스키마 추가 (30분)
├─ schema.prisma에 DomaeApiKey, DomaeHeartbeat 모델 추가
├─ DomaeTier enum 추가
├─ User, Pharmacy에 relation 추가
└─ npx prisma migrate dev --name add-domae-api-keys

Step 2: 백엔드 라우터 (1~2시간)
├─ src/routers/domae.ts 작성
├─ app.ts에 라우터 등록
└─ Postman/curl로 테스트

Step 3: 프론트엔드 페이지 (2~3시간)
├─ settings/domae/page.tsx 작성
├─ 사이드바 메뉴에 추가
└─ 키 발급 → 복사 → 재발급 플로우 테스트

Step 4: domae-mcp URL 변경 (10분)
├─ api_key.py의 URL을 팜스퀘어로 변경
└─ 커밋 + 푸시

Step 5: 통합 테스트 (30분)
├─ 팜스퀘어 로그인 → 키 발급
├─ domae-mcp 설정에 키 입력 → 검증 성공
└─ 하트비트 수신 확인
```

---

## 8. 구독 연동 (나중에)

사용자가 팜스퀘어에서 유료 구독하면 도매 API 키 등급도 자동 변경:

```typescript
// 구독 변경 시 (subscription 웹훅 또는 cron)
async function syncDomaeTier(userId: string) {
  const subscription = await prisma.subscription.findFirst({
    where: { pharmacy: { users: { some: { id: userId } } }, status: "active" },
  });

  const tier = resolveTier(subscription);

  await prisma.domaeApiKey.updateMany({
    where: { userId, active: true },
    data: { tier },
  });
}
```

기존 구독 cron (`0 21 * * 1-7`)에 이 로직 추가하면 됨.

---

## 9. 보안 고려사항

| 항목 | 대응 |
|------|------|
| verify/heartbeat는 인증 없이 호출 | API 키 자체가 인증 수단 |
| 키 유출 시 | 팜스퀘어에서 재발급 (기존 키 비활성화) |
| Rate limiting | verify: 분당 10회, heartbeat: 시간당 5회 |
| IP 추적 | verify/heartbeat에서 IP 기록 |
| 키 형식 검증 | `dmk_{tier}_{32hex}` 패턴 매칭 |
