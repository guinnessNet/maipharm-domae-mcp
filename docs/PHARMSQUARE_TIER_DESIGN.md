# 팜스퀘어 서비스 등급 및 메뉴 접근 제어 설계

> 팜스퀘어의 기존 Role/Subscription 시스템에 도매 서비스를 통합하고,
> 사용자 등급에 따른 메뉴 접근 제어를 설계합니다.

---

## 1. 현재 시스템 분석

### 1.1 현재 Role (변경 없음)
```
master      → 시스템 관리자 (약국 승인, 데이터 관리)
admin       → 약국 관리자 (약국 설정, 직원 관리)
pharmacist  → 관리약사 (약품/재고 전문)
employee    → 약국 직원
user        → 일반 사용자 (가입 직후, 승인 대기)
```

### 1.2 현재 Plan
```prisma
enum Plan {
  free        // 마스터가 부여하는 무료 체험
  basic       // 5,000원/월
  pro         // 29,000원/월 (standard)
  enterprise  // 99,000원/월 (premium)
}
```

### 1.3 현재 메뉴 구조
```
master      → masterMenu (11개)
admin       → adminMenu (7개) + generalMenu (6개)
pharmacist  → pharmMenu (4개) + generalMenu (6개)
employee    → generalMenu (6개)
user        → 없음 (pharmacy 미연결)
```

### 1.4 문제점
- **Plan에 따른 기능 제한이 구현되어 있지 않음** (구독이 있으면 전부 사용 가능)
- 구독 없는 사용자(미결제)도 로그인하면 모든 메뉴 접근 가능
- 도매 서비스를 추가할 등급 체계가 없음

---

## 2. 설계 방향

### 2.1 핵심 원칙

```
"가입은 무료, 기본 기능 제공. 고급 기능은 구독에 따라."
```

1. **회원가입**: 기존과 동일 (관리자 승인 필요)
2. **구독 없는 사용자**: 카카오톡 알림, AI 챗봇, 도매 API 키 발급 사용 가능
3. **구독 사용자**: 처방전, 조제, 복약지도, 재고관리 등 전체 기능
4. **도매 서비스**: 별도 등급으로 관리 (구독과 독립)

### 2.2 2축 등급 체계

```
축 1: 팜스퀘어 구독 (Plan)
  → 처방전/조제/복약지도/재고 등 약국 핵심 기능 제어

축 2: 도매 등급 (DomaeTier)
  → 도매 검색/주문/모니터링 기능 제어

두 축은 독립적. 도매만 쓰는 사용자도, 팜스퀘어만 쓰는 사용자도 가능.
```

---

## 3. 서비스 등급 상세

### 3.1 팜스퀘어 구독 등급 (기존 Plan 활용)

| 등급 | 월 요금 | 대상 | 사용 가능 기능 |
|------|---------|------|---------------|
| **미구독** | 0원 | 가입만 한 사용자 | 텔레그램 알림, AI 챗봇, 약품 검색, 도매 API 키(free) 발급 |
| **basic** | 5,000원 | 소규모 약국 | 미구독 + 조제, 복약지도, 예약관리 + **도매 basic 무료 제공** |
| **pro** | 29,000원 | 일반 약국 | basic + 재고관리, 사용량 분석, 환자관리 + **카카오톡 알림** |
| **enterprise** | 99,000원 | 대형/체인 약국 | pro + 다중 약국, 통계, 직원관리 고급 |
| **free** | 0원 | 마스터 부여 | enterprise와 동일 (체험용) |

### 3.2 도매 등급 (신규 DomaeTier)

| 등급 | 월 요금 | 제공 형태 | 기능 |
|------|---------|----------|------|
| **free** | 0원 | 로컬 설치 | API 키 발급, 60분 모니터링, 텔레그램 알림 |
| **basic** | **0원** (팜스퀘어 구독자 무료) | 클라우드 | 30분 모니터링, 24시간, 텔레그램 알림 |
| **pro** | **+5만원** (추가 결제) | 클라우드 | 5분 모니터링, 24시간, 텔레그램 + **카카오톡**, 다중 약국 |

> **핵심 전략**: 도매로 돈 버는 것이 아니라, 도매를 미끼로 팜스퀘어 유입 유도.
> 팜스퀘어 구독하면 도매 basic이 공짜이므로, "도매 쓰려고 팜스퀘어 가입"하는 흐름 유도.

### 3.3 알림 채널 × 등급

```
                      미구독    basic     pro      enterprise
                      ──────   ──────   ──────   ──────────
텔레그램 알림           ✅       ✅       ✅        ✅
카카오톡 알림           ❌       ❌       ✅        ✅
도매 모니터링 알림      텔레그램  텔레그램  텔레+카톡  텔레+카톡
```

### 3.4 도매 등급 자동 연동 규칙

```
팜스퀘어 미구독         → 도매 free (로컬만, 60분)
팜스퀘어 basic 이상     → 도매 basic 자동 부여 (클라우드, 30분)
도매 pro 추가 결제      → 도매 pro (클라우드, 5분, 카카오톡)
```

---

## 4. 메뉴 접근 제어 매트릭스

### 4.1 전체 메뉴 × 등급 매트릭스

```
                          미구독  basic   pro    enterprise
                          ──────  ─────  ─────  ──────────
[공통 - 항상 접근 가능]
  AI 챗봇                   ✅      ✅     ✅      ✅
  약품 검색                  ✅      ✅     ✅      ✅
  텔레그램 알림 설정          ✅      ✅     ✅      ✅
  도매 API 키 관리           ✅      ✅     ✅      ✅
  내 정보 / 설정             ✅      ✅     ✅      ✅

[pro 이상 추가]
  카카오톡 알림              🔒      🔒     ✅      ✅

[basic 이상]
  조제                      🔒      ✅     ✅      ✅
  복약지도                   🔒      ✅     ✅      ✅
  개별제공(GRAP)             🔒      ✅     ✅      ✅
  예약관리                   🔒      ✅     ✅      ✅

[pro 이상]
  재고관리                   🔒      🔒     ✅      ✅
  사용량 분석                🔒      🔒     ✅      ✅
  환자연결                   🔒      🔒     ✅      ✅
  동일성분검색               🔒      🔒     ✅      ✅

[enterprise / admin 전용]
  통계                      🔒      🔒     🔒      ✅
  직원등록                   🔒      🔒     🔒      ✅
  약품관리(고급)              🔒      🔒     🔒      ✅
  예제관리                   🔒      🔒     🔒      ✅

[master 전용 - 변경 없음]
  약국승인, 유저승인, 무료기간제공 등

🔒 = 잠김 (클릭 시 "구독이 필요합니다" + 업그레이드 안내)
```

### 4.2 사이드바 메뉴 재구성

```typescript
// 현재: role 기반으로만 메뉴 분기
// 변경: role + plan 기반으로 메뉴 분기

// ── 모든 사용자 공통 ──
const freeMenu = [
  { label: "AI 챗봇", path: "/chat", icon: "chat" },
  { label: "약품검색", path: "/search/result", icon: "search" },
  { label: "도매 통합검색", path: "/settings/domae", icon: "domae" },  // 신규
];

// ── basic 이상 ──
const basicMenu = [
  { label: "조제", path: "/fill", icon: "fill" },
  { label: "복약지도", path: "/teach", icon: "teach" },
  { label: "개별제공", path: "/grap", icon: "grap" },
  { label: "예약관리", path: "/reserved", icon: "message" },
];

// ── pro 이상 ──
const proMenu = [
  { label: "사용량", path: "/amount", icon: "amount" },
  { label: "재고관리", path: "/pharm/yakstock/inventory", icon: "stock" },
  { label: "동일성분검색", path: "/pharm/yakstock/same-ingredient", icon: "ingredient" },
  { label: "환자연결", path: "/admin/patient-link", icon: "patient" },
];

// ── enterprise (admin role) ──
const enterpriseMenu = [
  { label: "통계", path: "/admin/statistics", icon: "stats" },
  { label: "직원등록", path: "/admin/profile", icon: "profile" },
  { label: "약품관리", path: "/admin/medicines", icon: "medicines" },
  { label: "예제관리", path: "/admin/mistaken", icon: "mistaken" },
];
```

### 4.3 잠긴 메뉴 UX

```
사이드바에서 잠긴 메뉴를 클릭하면:

┌─────────────────────────────────────┐
│  🔒 이 기능은 Basic 구독이          │
│     필요합니다.                      │
│                                      │
│  조제, 복약지도, 예약관리 등          │
│  약국 핵심 기능을 사용하려면          │
│  구독을 시작하세요.                   │
│                                      │
│  [요금제 보기]  [나중에]              │
└─────────────────────────────────────┘
```

---

## 5. 구현 계획

### 5.1 백엔드: 구독 상태 확인 미들웨어

```typescript
// pharmsquare-server-main/src/lib/requirePlan.ts (신규)

type PlanLevel = "none" | "basic" | "pro" | "enterprise" | "free";

const PLAN_HIERARCHY: Record<PlanLevel, number> = {
  none: 0,
  basic: 1,
  pro: 2,
  enterprise: 3,
  free: 3,  // free는 enterprise와 동급
};

function requirePlan(minPlan: PlanLevel) {
  return async (req, res, next) => {
    const user = req.session?.user;
    if (!user) return res.status(401).json({ error: "로그인이 필요합니다." });

    // master는 항상 통과
    if (user.role === "master") return next();

    const subscription = await prisma.subscription.findFirst({
      where: { pharmacyId: user.pharmacyId, status: "active" },
    });

    const currentPlan: PlanLevel = subscription?.plan || "none";
    const currentLevel = PLAN_HIERARCHY[currentPlan];
    const requiredLevel = PLAN_HIERARCHY[minPlan];

    if (currentLevel < requiredLevel) {
      return res.status(403).json({
        error: "구독 업그레이드가 필요합니다.",
        currentPlan,
        requiredPlan: minPlan,
      });
    }

    next();
  };
}
```

### 5.2 백엔드: 라우터에 적용

```typescript
// 기존 라우터에 미들웨어 추가

// basic 이상 필요
app.use("/api/fill", requirePlan("basic"), fillRouter);
app.use("/api/teach", requirePlan("basic"), teachRouter);
app.use("/api/grap", requirePlan("basic"), grapRouter);
app.use("/api/reserved", requirePlan("basic"), reservedRouter);

// pro 이상 필요
app.use("/api/stock", requirePlan("pro"), stockRouter);
app.use("/api/amount", requirePlan("pro"), amountRouter);

// enterprise 이상 필요
app.use("/api/admin/statistics", requirePlan("enterprise"), statisticsRouter);

// 구독 불필요 (모든 사용자)
app.use("/api/auth", authRouter);
app.use("/api/chat", chatRouter);        // AI 챗봇
app.use("/api/search", searchRouter);     // 약품 검색
app.use("/api/domae", domaeRouter);       // 도매 API 키
app.use("/api/message", messageRouter);   // 카카오톡
```

### 5.3 프론트엔드: Sidebar.tsx 수정

```typescript
// pharmsquare-next-main/src/components/Sidebar.tsx

// 구독 상태 조회 훅
const { data: subscription } = useQuery({
  queryKey: ["subscription-status"],
  queryFn: () => axios.get("/api/subscription/status"),
});

const currentPlan = subscription?.data?.plan || "none";
const planLevel = { none: 0, basic: 1, pro: 2, enterprise: 3, free: 3 };

function canAccess(requiredPlan: string) {
  return planLevel[currentPlan] >= planLevel[requiredPlan];
}

// 메뉴 렌더링
{freeMenu.map(item => (
  <MenuItem key={item.path} item={item} />              // 항상 활성
))}

{basicMenu.map(item => (
  <MenuItem
    key={item.path}
    item={item}
    locked={!canAccess("basic")}                         // 잠금 여부
    onLockedClick={() => showUpgradeDialog("basic")}     // 업그레이드 안내
  />
))}

{canAccess("pro") && proMenu.map(item => (
  <MenuItem key={item.path} item={item} />
))}
```

### 5.4 프론트엔드: 사용자 정보 API 확장

```typescript
// GET /api/auth/userinfo-v2 응답에 추가

{
  user: { id, name, email, role, ... },
  pharmacy: { id, name, ... },
  subscription: {                          // 추가
    plan: "basic" | "pro" | "enterprise" | "free" | null,
    status: "active" | "expired" | null,
    endDate: "2026-12-31",
  },
  domaeApiKey: {                           // 추가
    key: "dmk_free_xxxx",
    tier: "free" | "basic" | "pro",
    active: true,
  }
}
```

---

## 6. 미구독 사용자 경험 흐름

```
1. 팜스퀘어 가입 (이메일, 약국 정보)
        ↓
2. 관리자(master) 승인 대기
        ↓
3. 승인 완료 → 로그인 가능
        ↓
4. 미구독 상태로 접속
        ↓
   ┌─────────────────────────────────────────┐
   │  사용 가능:                              │
   │  • AI 챗봇 (약물 상담)                   │
   │  • 약품 검색                             │
   │  • 카카오톡 알림 설정                     │
   │  • 도매 API 키 발급 (로컬 설치용)         │
   │                                          │
   │  잠긴 메뉴:                              │
   │  • 🔒 조제 → "Basic 구독 필요"           │
   │  • 🔒 복약지도 → "Basic 구독 필요"       │
   │  • 🔒 재고관리 → "Pro 구독 필요"         │
   └─────────────────────────────────────────┘
        ↓
5. 구독 결제 시 → 해당 메뉴 잠금 해제
        ↓
6. 도매 유료 결제 시 → 클라우드 도매 서비스 이용 가능
```

---

## 7. DB 변경 사항

### 7.1 기존 테이블 변경: 없음
기존 User, Subscription, Plan 등은 그대로 유지.

### 7.2 신규 테이블 추가

```prisma
// DomaeApiKey, DomaeHeartbeat
// (PHARMSQUARE_API_KEY_PLAN.md에 이미 설계됨)

model DomaeApiKey {
  id          String      @id @default(cuid())
  createdAt   DateTime    @default(now())
  updatedAt   DateTime    @updatedAt
  userId      String
  user        User        @relation(fields: [userId], references: [id])
  pharmacyId  String?
  pharmacy    Pharmacy?   @relation(fields: [pharmacyId], references: [id])
  key         String      @unique
  tier        DomaeTier   @default(free)
  active      Boolean     @default(true)
  lastUsedAt  DateTime?
  lastIp      String?
  useCount    Int         @default(0)
  heartbeats  DomaeHeartbeat[]
}

model DomaeHeartbeat {
  id             String       @id @default(cuid())
  createdAt      DateTime     @default(now())
  apiKeyId       String
  apiKey         DomaeApiKey  @relation(fields: [apiKeyId], references: [id])
  version        String
  searchCount    Int          @default(0)
  orderCount     Int          @default(0)
  activeMonitors Int          @default(0)
  ip             String?
}

enum DomaeTier {
  free
  basic
  pro
}
```

---

## 8. 구현 순서

```
Phase A: 구독 기반 접근 제어 (1~2일)
├─ requirePlan 미들웨어 작성 (백엔드)
├─ 기존 라우터에 미들웨어 적용
├─ userinfo-v2 API에 subscription 정보 추가
├─ Sidebar.tsx에 plan 기반 메뉴 분기
└─ 잠긴 메뉴 클릭 시 업그레이드 다이얼로그

Phase B: 도매 API 키 시스템 (반나절)
├─ DomaeApiKey, DomaeHeartbeat 모델 추가
├─ /api/domae/* 라우터 작성
├─ settings/domae/page.tsx 프론트 페이지
└─ 사이드바에 "도매 통합검색" 메뉴 추가

Phase C: domae-mcp 연동 테스트 (30분)
├─ api_key.py URL을 pharmsquare로 변경
├─ 팜스퀘어 로그인 → 키 발급 → domae-mcp에 입력 → 검증
└─ 하트비트 수신 확인
```

---

## 9. 요약

### 변경 전 vs 변경 후

```
변경 전:
  로그인하면 모든 메뉴 사용 가능
  구독은 있지만 기능 제한 미적용
  도매 서비스 연결 없음

변경 후:
  미구독: AI 챗봇 + 약품검색 + 카톡 + 도매 API 키
  basic:  + 조제, 복약지도, 예약관리
  pro:    + 재고관리, 사용량 분석, 환자연결
  enterprise: + 통계, 직원관리 고급

  도매 서비스는 독립적으로:
  free:   로컬 설치 (API 키만 발급)
  basic:  클라우드 30분 모니터링 (5만/월)
  pro:    클라우드 5분 모니터링 (10만/월)
```
