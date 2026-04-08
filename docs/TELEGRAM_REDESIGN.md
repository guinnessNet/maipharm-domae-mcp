# 텔레그램 알림 시스템 리디자인

## 현재 문제 분석

### 문제 1: 하루 2~3번밖에 알림이 안 옴
**원인**: 알림 빈도가 아니라 **알림 필터링 부재**가 문제.
- 모니터링 사이클은 60분 간격으로 돌고 있음 (08~22시 기준 약 14회/일)
- 변동이 감지된 사이클에서만 알림을 보내므로 2~3회 수신은 정상
- **진짜 문제**: 알림이 올 때 쓸모없는 잡변동(재고 1~2개 변동)이 섞여 있고, 정작 중요한 변동은 놓침

### 문제 2: 원하는 알림 유형이 누락됨
현재 감지하는 것:
- ✅ 재고 입고 (0 → N)
- ✅ 가격 변동
- ✅ 모든 재고 변동 (잡변동 포함)
- ✅ 긴급주문 실행 결과

**누락된 것**:
- ❌ 급격한 재고 감소 (30분 전 대비 30% 이상 감소)
- ❌ 긴급주문이 자동 실행되었을 때의 명확한 알림

### 문제 3: 메시지 포맷이 복잡함
현재: 모든 변동을 한 메시지에 줄줄이 나열 → 중요한 것과 사소한 것 구분 불가

### 문제 4: 상호작용 없음
현재: 단방향 알림만 가능. 입고 알림 보고 바로 주문하려면 웹에 접속해야 함

---

## 설계 목표

| # | 목표 | 설명 |
|---|------|------|
| G1 | 의미 있는 알림만 | 잡변동 제거, 3가지 핵심 이벤트에 집중 |
| G2 | 즉시 행동 가능 | 텔레그램 인라인 버튼으로 1-tap 주문 |
| G3 | 한눈에 파악 | 이벤트별 분리 메시지, 깔끔한 포맷 |
| G4 | 주문 피드백 | 버튼 주문 후 결과 메시지 수신 |

---

## 핵심 변경 사항

### 1. 알림 이벤트 3가지로 축소

| 이벤트 | 조건 | 이모지 |
|--------|------|--------|
| **급격한 재고 감소** | 직전 스냅샷 대비 30% 이상 감소 | 🔴 |
| **재입고** | 직전 스냅샷 재고 0 → 현재 > 0 | 🟢 |
| **긴급주문 체결** | 긴급주문 설정된 제품이 자동 주문됨 | ⚡ |

**제거 대상** (더 이상 알림 안 보냄):
- 가격 변동 (가격은 참고 정보로만 표시)
- 소폭 재고 변동 (30% 미만)
- 신규 제품 등장

### 2. 메시지 포맷 리디자인

#### 2-A. 급격한 재고 감소
```
🔴 재고 급감
복산 미녹시딜정(병)
356개 → 50개 (▼86%)
현재가 23,700원
```

#### 2-B. 재입고 (인라인 버튼 포함)
```
🟢 입고
백제 현대미녹시딜정5mg
190개 입고 | 23,700원

[1개 주문] [3개 주문] [5개 주문]
```
→ 버튼 누르면 해당 도매에서 해당 수량 즉시 주문

#### 2-C. 긴급주문 체결
```
⚡ 긴급주문 체결
복산 타이레놀정500mg
3개 주문 완료 | 단가 2,370원
잔여: 7/10개
```

#### 2-D. 주문 결과 (버튼 주문 후)
```
✅ 주문 완료
복산 타이레놀정500mg × 1개
주문금액 2,370원
```
또는
```
❌ 주문 실패
복산 타이레놀정500mg × 1개
사유: 재고 부족
```

### 3. 텔레그램 인라인 버튼 + 콜백 처리

#### 아키텍처

```
[텔레그램 서버]
    │
    ├── sendMessage (reply_markup: InlineKeyboardMarkup)
    │   → 입고 알림 + [1개 주문] [3개 주문] [5개 주문] 버튼
    │
    └── Webhook POST /telegram/callback
        → 버튼 클릭 시 callback_query 수신
        → Redis 긴급주문 큐에 job push
        → 주문 결과를 editMessageText로 업데이트

[CloudWorker]
    ├── domae:jobs:urgent 큐에서 주문 job 처리
    └── 완료 후 텔레그램 결과 메시지 전송
```

#### 콜백 데이터 형식
```
order:{monitor_id}:{supplier}:{product_id}:{quantity}
```
- 텔레그램 callback_data 최대 64바이트 → 축약 필요
- `monitor_id`는 DB에서 조회, `supplier`+`product_id`로 크롤러 특정

#### 구현 위치
- **Webhook 엔드포인트**: `pharmsquare-server-main`에 `POST /api/telegram/callback` 추가
  - 또는 domae-mcp 클라우드에 별도 FastAPI 엔드포인트 추가
- **주문 실행**: 기존 `urgent_order_immediate` job 활용

### 4. 모니터링 간격 조정

현재: 60분 간격 → "30분 전 대비 30% 감소" 감지 불가

**변경**:
- 스냅샷 비교 기준을 **직전 사이클**이 아닌 **최근 N분 이내 스냅샷**으로 변경
- 스냅샷을 **교체(DELETE+INSERT)하지 않고 누적** 저장 → 시계열 비교 가능
- 오래된 스냅샷은 24시간 후 자동 정리 (cron 또는 사이클 시작 시)

---

## 수정 대상 파일

### Cloud 모드 (운영 중인 환경)

| 파일 | 변경 내용 |
|------|----------|
| `cloud/scheduler.py` | `_detect_changes()` 로직 교체 → 3가지 이벤트만 감지 |
| `cloud/scheduler.py` | 스냅샷 누적 저장 + 24h 정리 로직 추가 |
| `cloud/notifier.py` | `send_telegram()` → `send_telegram_with_buttons()` 추가 (InlineKeyboardMarkup 지원) |
| `cloud/scheduler.py` | 알림 메시지 포맷 리디자인 |
| `cloud/webhook.py` **(신규)** | 텔레그램 콜백 핸들러 (버튼 클릭 → 주문 실행) |
| `cloud/worker.py` | webhook에서 push한 주문 job 처리 (기존 로직 활용) |

### Local 모드 (동일 적용)

| 파일 | 변경 내용 |
|------|----------|
| `core/services/monitor_service.py` | 변동 감지 로직 동일 교체 |
| `core/services/telegram_service.py` | 인라인 버튼 + 콜백 지원 추가 |

### 서버 (pharmsquare-server-main)

| 파일 | 변경 내용 |
|------|----------|
| `src/routes/telegram.ts` **(신규)** | 텔레그램 Webhook 프록시 → Redis job push |

---

## 구현 순서

### Step 1: 알림 로직 정리 (cloud/scheduler.py)
1. `_detect_changes()` → `_detect_alerts()` 교체
   - 30% 이상 감소만 감지
   - 0→N 재입고만 감지
   - 가격/소폭변동 제거
2. 스냅샷 누적 저장으로 변경
3. 24h 이상 오래된 스냅샷 정리 로직

### Step 2: 메시지 포맷 리디자인 (cloud/notifier.py)
1. 이벤트별 개별 메시지 전송 (묶어서 1개 → 이벤트마다 1개)
2. 재입고 메시지에 InlineKeyboardMarkup 첨부
3. `answerCallbackQuery` + `editMessageText` 헬퍼 추가

### Step 3: 텔레그램 Webhook 콜백 처리
1. `cloud/webhook.py` — FastAPI 엔드포인트
2. callback_data 파싱 → Redis `domae:jobs:urgent` 큐에 push
3. 주문 완료 후 결과 메시지 전송

### Step 4: 로컬 모드 동기화 (core/services/)
1. monitor_service.py 알림 로직 동일 적용
2. telegram_service.py 버튼 지원 추가

---

## 기술 참고

### 텔레그램 InlineKeyboardMarkup
```python
reply_markup = {
    "inline_keyboard": [
        [
            {"text": "1개 주문", "callback_data": "order:mon123:boksan:PD001:1"},
            {"text": "3개 주문", "callback_data": "order:mon123:boksan:PD001:3"},
            {"text": "5개 주문", "callback_data": "order:mon123:boksan:PD001:5"},
        ]
    ]
}

requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
    "chat_id": chat_id,
    "text": message,
    "parse_mode": "HTML",
    "reply_markup": reply_markup,
})
```

### 콜백 처리
```python
# Webhook으로 수신되는 callback_query
{
    "callback_query": {
        "id": "...",
        "data": "order:mon123:boksan:PD001:1",
        "message": {"chat": {"id": 12345}, "message_id": 678}
    }
}

# 1. 즉시 응답 (로딩 표시)
requests.post(f".../answerCallbackQuery", json={
    "callback_query_id": query_id,
    "text": "주문 처리 중..."
})

# 2. 메시지 업데이트 (버튼 제거 + 상태 표시)
requests.post(f".../editMessageText", json={
    "chat_id": chat_id,
    "message_id": message_id,
    "text": "🟢 입고\n백제 현대미녹시딜정5mg\n190개 입고 | 23,700원\n\n⏳ 1개 주문 처리 중...",
    "parse_mode": "HTML",
})

# 3. 주문 완료 후 최종 업데이트
requests.post(f".../editMessageText", json={
    "chat_id": chat_id,
    "message_id": message_id,
    "text": "🟢 입고\n백제 현대미녹시딜정5mg\n190개 입고 | 23,700원\n\n✅ 1개 주문 완료",
    "parse_mode": "HTML",
})
```

### Webhook 등록 (1회)
```bash
curl "https://api.telegram.org/bot{TOKEN}/setWebhook?url=https://your-server.com/api/telegram/callback"
```

---

## 스냅샷 스키마 변경

### 현재 (교체 방식)
매 사이클마다 DELETE ALL → INSERT → 직전 1개만 비교 가능

### 변경 후 (누적 방식)
```sql
-- 기존 테이블에 인덱스 추가
CREATE INDEX idx_snapshots_monitor_scanned
ON domae_inventory_snapshots ("monitorId", "scannedAt" DESC);

-- 24시간 초과 스냅샷 정리 (매 사이클 시작 시)
DELETE FROM domae_inventory_snapshots
WHERE "scannedAt" < NOW() - INTERVAL '24 hours';
```

비교 쿼리:
```sql
-- 30분 전 스냅샷과 비교
SELECT DISTINCT ON (supplier, "productName")
       supplier, "productName", quantity, price
FROM domae_inventory_snapshots
WHERE "monitorId" = %s
  AND "scannedAt" BETWEEN NOW() - INTERVAL '35 minutes' AND NOW() - INTERVAL '25 minutes'
ORDER BY supplier, "productName", "scannedAt" DESC
```

---

## 요약

| Before | After |
|--------|-------|
| 모든 변동 알림 (잡음 많음) | 3가지 핵심 이벤트만 |
| 1개 메시지에 모든 변동 나열 | 이벤트별 개별 메시지 |
| 단방향 (읽기만) | 인라인 버튼 → 1-tap 주문 |
| 주문 결과 별도 확인 필요 | 같은 메시지에 결과 업데이트 |
| 스냅샷 교체 → 직전만 비교 | 스냅샷 누적 → 시계열 비교 |
