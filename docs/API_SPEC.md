# API 명세: maipharm-domae-mcp

## REST API (웹 UI용)

Base URL: `http://localhost:5900/api`

---

### 검색

#### `GET /api/search?keyword={keyword}&suppliers={suppliers}`

전 도매상 통합 재고 검색.

**Parameters:**
| 이름 | 타입 | 필수 | 설명 |
|------|------|------|------|
| keyword | string | Y | 검색 키워드 (제품명 또는 보험코드) |
| suppliers | string | N | 쉼표 구분 도매상 필터 (예: "지오영,복산") |

**Response 200:**
```json
{
  "keyword": "아모잘탄",
  "results": [
    {
      "maker": "한미약품",
      "product_name": "아모잘탄정 5/100mg",
      "unit": "30T",
      "insurance_code": "655801440",
      "suppliers": [
        { "name": "지오영", "quantity": 25, "price": 12500, "product_id": "GEO-12345" },
        { "name": "복산", "quantity": 10, "price": 12300, "product_id": "BOK-67890" }
      ]
    }
  ]
}
```

---

### 주문

#### `POST /api/orders`

도매상에 주문 실행.

**Request Body:**
```json
{
  "supplier": "지오영",
  "product_id": "GEO-12345",
  "product_name": "아모잘탄정 5/100mg",
  "quantity": 5
}
```

**Response 200:**
```json
{
  "success": true,
  "message": "주문 완료",
  "order_id": "ORD-2024-001"
}
```

---

### 긴급주문

#### `GET /api/urgent-orders`

긴급주문 목록 조회.

**Response 200:**
```json
{
  "orders": [
    {
      "id": 1,
      "product_name": "아모잘탄정 5/100mg",
      "unit": "30T",
      "total_quantity": 10,
      "filled_quantity": 3,
      "active": true,
      "created_at": "2026-03-15T10:00:00",
      "suppliers": [
        { "supplier": "지오영", "product_id": "GEO-12345", "price": 12500 },
        { "supplier": "복산", "product_id": "BOK-67890", "price": 12300 }
      ],
      "logs": [
        { "supplier": "복산", "ordered_quantity": 3, "success": true, "created_at": "..." }
      ]
    }
  ]
}
```

#### `POST /api/urgent-orders`

긴급주문 등록.

**Request Body:**
```json
{
  "product_name": "아모잘탄정 5/100mg",
  "unit": "30T",
  "insurance_code": "655801440",
  "total_quantity": 10,
  "suppliers": [
    { "supplier": "지오영", "product_id": "GEO-12345", "price": 12500 },
    { "supplier": "복산", "product_id": "BOK-67890", "price": 12300 }
  ]
}
```

#### `PUT /api/urgent-orders/{id}/cancel`

긴급주문 취소.

#### `PUT /api/urgent-orders/{id}/reactivate`

완료/취소된 긴급주문 재활성화.

#### `DELETE /api/urgent-orders/{id}`

긴급주문 삭제.

---

### 주문이력

#### `GET /api/orders?limit={limit}&offset={offset}`

주문 이력 조회.

**Parameters:**
| 이름 | 타입 | 필수 | 기본값 | 설명 |
|------|------|------|--------|------|
| limit | int | N | 50 | 조회 개수 |
| offset | int | N | 0 | 시작 위치 |

---

### 설정

#### `GET /api/settings/credentials`

도매별 계정 목록 조회 (비밀번호는 마스킹).

**Response 200:**
```json
{
  "credentials": [
    { "supplier": "지오영", "login_id": "***REMOVED***", "login_pw": "****", "configured": true },
    { "supplier": "복산", "login_id": "", "login_pw": "", "configured": false }
  ]
}
```

#### `PUT /api/settings/credentials`

도매 계정 저장.

**Request Body:**
```json
{
  "supplier": "지오영",
  "login_id": "***REMOVED***",
  "login_pw": "***REMOVED***"
}
```

#### `POST /api/settings/credentials/test`

계정 연결 테스트 (실제 로그인 시도).

**Request Body:**
```json
{ "supplier": "지오영" }
```

**Response 200:**
```json
{ "success": true, "message": "로그인 성공" }
```

#### `GET /api/settings/telegram`

텔레그램 설정 조회.

#### `PUT /api/settings/telegram`

텔레그램 설정 저장.

**Request Body:**
```json
{
  "token": "123456:ABC...",
  "chat_id": "987654321"
}
```

#### `GET /api/settings/schedules`

모니터링 스케줄 조회.

#### `PUT /api/settings/schedules`

모니터링 스케줄 수정.

---

### 모니터링 제품

#### `GET /api/products`

모니터링 대상 제품 목록.

#### `POST /api/products`

제품 추가.

**Request Body:**
```json
{ "name": "655801440", "description": "" }
```

#### `DELETE /api/products/{id}`

제품 삭제.

---

### 모니터링 제어

#### `POST /api/monitor/start`
#### `POST /api/monitor/stop`
#### `GET /api/monitor/status`

**Response 200:**
```json
{ "running": true, "last_run": "2026-03-15T10:30:00" }
```

---

### 신규도매 요청

#### `POST /api/supplier-request`

신규 도매상 크롤러 개발 요청 메일 전송.

**Request Body:**
```json
{
  "supplier_name": "새로운도매",
  "login_url": "https://example.com/login",
  "login_id": "user123",
  "login_pw": "pass123",
  "agreed": true
}
```

**Response 200:**
```json
{ "success": true, "message": "요청이 전송되었습니다." }
```

**Response 400** (동의 미체크):
```json
{ "success": false, "message": "개인정보 제공에 동의해주세요." }
```

---

### 업데이트 체크

#### `GET /api/update-check`

**Response 200:**
```json
{
  "current_version": "1.0.0",
  "latest_version": "1.1.0",
  "update_available": true,
  "release_url": "https://github.com/.../releases/tag/v1.1.0"
}
```

---

## MCP Tools

MCP 서버가 제공하는 도구 목록. stdio transport로 JSON-RPC 통신.

### search_inventory

전 도매상 통합 재고 검색.

```json
{
  "name": "search_inventory",
  "description": "의약품 키워드로 8개 도매상(지오영, 복산, 인천, 티제이팜, HMP, 백제, 피코, 새로팜)의 재고를 통합 검색합니다.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "keyword": {
        "type": "string",
        "description": "검색 키워드 (제품명 또는 보험코드)"
      },
      "suppliers": {
        "type": "array",
        "items": { "type": "string" },
        "description": "검색할 도매상 목록 (미지정 시 전체)"
      }
    },
    "required": ["keyword"]
  }
}
```

### place_order

특정 도매상에 주문 실행.

```json
{
  "name": "place_order",
  "description": "특정 도매상에 의약품을 주문합니다. product_id는 search_inventory 결과에서 얻을 수 있습니다.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "supplier": { "type": "string", "description": "도매상명" },
      "product_id": { "type": "string", "description": "도매상 내부 제품코드" },
      "product_name": { "type": "string", "description": "제품명" },
      "quantity": { "type": "integer", "description": "주문 수량" }
    },
    "required": ["supplier", "product_id", "product_name", "quantity"]
  }
}
```

### create_urgent_order

긴급주문 등록 (재고 감지 시 자동주문).

```json
{
  "name": "create_urgent_order",
  "description": "긴급주문을 등록합니다. 모니터링 중 해당 제품의 재고가 감지되면 자동으로 주문합니다.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "product_name": { "type": "string" },
      "unit": { "type": "string" },
      "insurance_code": { "type": "string", "description": "보험코드" },
      "total_quantity": { "type": "integer", "description": "총 필요 수량" },
      "suppliers": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "supplier": { "type": "string" },
            "product_id": { "type": "string" },
            "price": { "type": "integer" }
          }
        },
        "description": "주문 가능한 도매상 목록 (search_inventory 결과에서 선택)"
      }
    },
    "required": ["product_name", "unit", "total_quantity", "suppliers"]
  }
}
```

### list_urgent_orders

```json
{
  "name": "list_urgent_orders",
  "description": "긴급주문 목록과 진행 상태를 조회합니다.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "active_only": { "type": "boolean", "default": false }
    }
  }
}
```

### cancel_urgent_order

```json
{
  "name": "cancel_urgent_order",
  "description": "긴급주문을 취소합니다.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "urgent_order_id": { "type": "integer" }
    },
    "required": ["urgent_order_id"]
  }
}
```

### get_order_history

```json
{
  "name": "get_order_history",
  "description": "주문 이력을 조회합니다.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "limit": { "type": "integer", "default": 20 },
      "offset": { "type": "integer", "default": 0 }
    }
  }
}
```

### start_monitoring

```json
{
  "name": "start_monitoring",
  "description": "재고 모니터링을 시작합니다. 등록된 제품들의 재고를 주기적으로 검색하고, 변동 시 텔레그램으로 알립니다.",
  "inputSchema": { "type": "object", "properties": {} }
}
```

### stop_monitoring

```json
{
  "name": "stop_monitoring",
  "description": "재고 모니터링을 중지합니다.",
  "inputSchema": { "type": "object", "properties": {} }
}
```

### get_monitoring_status

```json
{
  "name": "get_monitoring_status",
  "description": "모니터링 실행 상태와 등록된 감시 제품 목록을 조회합니다.",
  "inputSchema": { "type": "object", "properties": {} }
}
```

### add_monitoring_product

모니터링 대상 제품 추가.

```json
{
  "name": "add_monitoring_product",
  "description": "모니터링 대상 제품을 추가합니다. 보험코드 또는 제품명으로 등록하면 주기적으로 재고를 검색합니다.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "name": { "type": "string", "description": "보험코드 또는 제품명" }
    },
    "required": ["name"]
  }
}
```

### remove_monitoring_product

모니터링 대상 제품 삭제.

```json
{
  "name": "remove_monitoring_product",
  "description": "모니터링 대상 제품을 삭제합니다.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "product_id": { "type": "integer", "description": "제품 ID" }
    },
    "required": ["product_id"]
  }
}
```

### test_credential

도매상 계정 연결 테스트.

```json
{
  "name": "test_credential",
  "description": "특정 도매상의 계정이 올바른지 실제 로그인을 시도하여 확인합니다.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "supplier": { "type": "string", "description": "도매상명 (지오영, 복산, 인천, 티제이팜, HMP, 백제, 피코, 새로팜)" }
    },
    "required": ["supplier"]
  }
}
```
