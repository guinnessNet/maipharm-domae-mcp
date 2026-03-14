# 데이터베이스 스키마: maipharm-domae-mcp

SQLite, 저장 경로: `~/.maipharm-domae-mcp/data/domae.db`

## ERD

```
┌─────────────────────┐
│      products        │
├─────────────────────┤
│ id (PK)             │
│ name                │
│ description         │
│ created_at          │
└─────────────────────┘

┌─────────────────────┐     ┌─────────────────────────┐
│ monitor_schedules   │     │   inventory_snapshots    │
├─────────────────────┤     ├─────────────────────────┤
│ id (PK)             │     │ id (PK)                 │
│ start_hour          │     │ maker                   │
│ end_hour            │     │ product_name            │
│ interval_minutes    │     │ unit                    │
└─────────────────────┘     │ insurance_code          │
                            │ quantity                │
                            │ supplier                │
                            │ price                   │
                            │ product_id              │
                            │ scanned_at              │
                            └─────────────────────────┘

┌──────────────────────┐
│       orders         │
├──────────────────────┤
│ id (PK)              │
│ supplier             │
│ product_name         │
│ unit                 │
│ quantity             │
│ price                │
│ success              │
│ message              │
│ is_urgent            │
│ ordered_at           │
└──────────────────────┘

┌──────────────────────────┐    ┌────────────────────────────┐
│     urgent_orders        │    │  urgent_order_suppliers     │
├──────────────────────────┤    ├────────────────────────────┤
│ id (PK)                  │◀──│ id (PK)                    │
│ product_name             │    │ urgent_order_id (FK)       │
│ unit                     │    │ supplier                   │
│ insurance_code           │    │ product_id                 │
│ total_quantity           │    │ price                      │
│ filled_quantity          │    └────────────────────────────┘
│ active                   │
│ created_at               │
│ completed_at             │    ┌────────────────────────────┐
│                          │    │   urgent_order_logs        │
│                          │    ├────────────────────────────┤
│                          │◀──│ id (PK)                    │
│                          │    │ urgent_order_id (FK)       │
└──────────────────────────┘    │ supplier                   │
                                │ ordered_quantity           │
                                │ success                    │
                                │ message                    │
                                │ ordered_at                 │
                                └────────────────────────────┘
```

## 테이블 상세

> **참고**: credentials(도매상 계정)는 DB가 아닌 `config.json`에 암호화 저장합니다.

### products
모니터링 대상 제품. 보험코드(name)로 등록, 첫 검색 시 description 자동 채움.

| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | INTEGER PK | 자동증가 |
| name | TEXT NOT NULL | 보험코드 또는 제품명 |
| description | TEXT | 실제 제품명 (자동 채움) |
| created_at | DATETIME | 등록 시각 |

### orders
주문 이력.

| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | INTEGER PK | 자동증가 |
| supplier | TEXT NOT NULL | 도매상명 |
| product_name | TEXT NOT NULL | 제품명 |
| unit | TEXT | 단위 |
| quantity | INTEGER | 주문 수량 |
| price | INTEGER | 단가 |
| success | BOOLEAN | 주문 성공 여부 |
| message | TEXT | 결과 메시지 |
| is_urgent | BOOLEAN | 긴급주문 여부 |
| ordered_at | DATETIME | 주문 시각 |

### urgent_orders
긴급주문 (재고 감지 시 자동주문).

| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | INTEGER PK | 자동증가 |
| product_name | TEXT NOT NULL | 제품명 |
| unit | TEXT | 단위 |
| insurance_code | TEXT | 보험코드 |
| total_quantity | INTEGER | 총 필요 수량 |
| filled_quantity | INTEGER | 채워진 수량 (기본 0) |
| active | BOOLEAN | 활성 여부 (기본 true) |
| created_at | DATETIME | 등록 시각 |
| completed_at | DATETIME | 완료 시각 (nullable) |

### urgent_order_suppliers
긴급주문과 도매상 매핑.

| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | INTEGER PK | 자동증가 |
| urgent_order_id | INTEGER FK | urgent_orders.id |
| supplier | TEXT NOT NULL | 도매상명 |
| product_id | TEXT NOT NULL | 도매상 내부 제품코드 |
| price | INTEGER | 단가 |

### urgent_order_logs
긴급주문 자동실행 로그.

| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | INTEGER PK | 자동증가 |
| urgent_order_id | INTEGER FK | urgent_orders.id |
| supplier | TEXT | 도매상명 |
| ordered_quantity | INTEGER | 주문 수량 |
| success | BOOLEAN | 성공 여부 |
| message | TEXT | 결과 메시지 |
| ordered_at | DATETIME | 실행 시각 |

### inventory_snapshots
재고 스냅샷 (모니터링 비교용).

| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | INTEGER PK | 자동증가 |
| maker | TEXT | 제약사 |
| product_name | TEXT | 제품명 |
| unit | TEXT | 단위 |
| insurance_code | TEXT | 보험코드 |
| quantity | INTEGER | 재고 수량 |
| supplier | TEXT | 도매상명 |
| price | INTEGER | 단가 |
| product_id | TEXT | 도매상 내부 제품코드 |
| scanned_at | DATETIME | 스캔 시각 |

### monitor_schedules
시간대별 모니터링 주기.

| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | INTEGER PK | 자동증가 |
| start_hour | INTEGER | 시작 시 (0-23) |
| end_hour | INTEGER | 종료 시 (1-24) |
| interval_minutes | INTEGER | 모니터링 주기 (분) |

기본값:
| 시간대 | 주기 |
|--------|------|
| 00-08시 | 60분 |
| 08-22시 | 30분 |
| 22-24시 | 60분 |

## domae-v2 대비 변경사항

1. **credentials 테이블 삭제** → `config.json`으로 대체 (암호화 저장)
2. 나머지 테이블은 동일한 스키마 유지
3. `database.py`의 `_seed_credentials()` 삭제
4. `_migrate_columns()` 로직 유지 (향후 스키마 변경 대비)
