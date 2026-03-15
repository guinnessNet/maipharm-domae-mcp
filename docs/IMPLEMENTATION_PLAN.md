# 구현 작업 계획서

> 작성일: 2026-03-16
> 수정일: 2026-03-16 (검증 결과 반영 — C1~C4, M1~M7, 누락 단계 추가)
> 기준 브랜치: feature/pharmsquare-api-key
> 참고: DESIGN_CRAWLER_DISTRIBUTION.md, WORKFLOW_v2.md

---

## 검증 반영 사항 요약

| ID | 수정 내용 |
|----|----------|
| C1 | 서명 방식을 payload 원문 SHA256 해시 서명으로 변경 (JSON 직렬화 순서 문제 제거) |
| C3/C4 | Prisma 스키마를 String id + cuid()로 변경, pharmacyId를 String?으로 |
| M1 | CrawlerLoader에 config.base_dir 사용 (config_dir 아님) |
| M2 | FastAPI lifespan에서 asyncio.to_thread(loader.load) 사용 |
| M4 | registry.py의 get_all() 버그 수정 |
| M6 | 크롤러 0개 로드 시 경고 로그 추가 |
| M7 | 캐시 파일 atomic write (temp → rename) |
| m3 | API 키 raw 저장 안 함 → keyHash + keyPrefix만 저장 |
| 누락1 | Step 0에 Ed25519 키페어 생성 추가 |
| 누락2 | Step 6에 크롤러 .py 파일 git rm 명시 |
| 누락3 | Step 1에 test_crawlers.py 수정 포함 |
| 누락4 | Step 2에 OrderService 크롤러 없을 때 graceful 처리 |
| 누락5 | Step 7에 API 키 변경 시 캐시 무효화 + loader 재생성 |
| 누락6 | Step 8 이후 프론트엔드 빌드 단계 |

### Codex 3차 검증 반영 (2026-03-16)

| ID | 수정 내용 |
|----|----------|
| Codex-Major1 | 설계서에서 SearchService/OrderService "변경 없음" → "is_loaded 체크 추가"로 수정 |
| Codex-Major2 | 번들 구조 섹션을 실제 와이어 포맷({payload, signature})으로 업데이트 |
| Codex-Major3 | 오프라인 유예를 "7일" → "최대 14일(만료7일+유예7일)"로 명확화 |
| Codex-Minor1 | VERSION_CHECK_INTERVAL 상수 제거, "앱 시작 시마다 체크"로 변경 |
| Codex-Minor2 | _import_crawlers의 os.write에 try/finally 추가 (fd leak 방지) |
| Codex-Info | 크롤러 코드 하드 요구사항 명시 (절대 import, 상대 import 금지, 의존성 제한) |

---

## 작업 순서 총괄

```
Step 0: 현재 상태 정리 + Ed25519 키 생성 + 커밋        (30분)
Step 1: 로컬 — CrawlerLoader + Registry 변경          (1일)    ← 커밋 #1
Step 2: 로컬 — 앱 시작 흐름 연동 + 서비스 보강         (0.5일)  ← 커밋 #2
Step 3: 서버 — Prisma 스키마 + 마이그레이션             (0.5일)  ← 커밋 #3 (pharmsquare)
Step 4: 서버 — 크롤러 배포 API + 서명                  (1일)    ← 커밋 #4 (pharmsquare)
Step 5: 서버 — 크롤러 DB 시딩                          (0.5일)  ← 커밋 #5 (pharmsquare)
Step 6: 통합 테스트 + 크롤러 파일 git rm               (0.5일)  ← 커밋 #6
Step 7: 로컬 — 셋업 위자드 + API키 검증 UI             (1.5일)  ← 커밋 #7
Step 8: 로컬 — 검색 UX 개선 + MCP 가이드               (1일)    ← 커밋 #8
Step 8.5: 프론트엔드 빌드 + static 배치                (15분)   ← 커밋 #8.5
Step 9: 서버 — 어드민 API + API키 발급 UI               (1일)    ← 커밋 #9 (pharmsquare)
Step 10: 최종 테스트 + 문서 업데이트                    (0.5일)  ← 커밋 #10
────────────────────────────────────────────────────
합계: 약 9~10일
```

---

## Step 0: 현재 상태 정리 + Ed25519 키 생성 (30분)

현재 커밋되지 않은 변경사항을 정리하고, 서명용 키페어를 생성한다.

### 0-1. Ed25519 키페어 생성 (누락1)

```bash
python -c "
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
import base64

private_key = Ed25519PrivateKey.generate()
private_bytes = private_key.private_bytes(
    encoding=serialization.Encoding.DER,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption()
)
public_bytes = private_key.public_key().public_bytes(
    encoding=serialization.Encoding.Raw,
    format=serialization.PublicFormat.Raw
)
print('PRIVATE (pharmsquare .env):')
print(base64.b64encode(private_bytes).decode())
print()
print('PUBLIC (loader.py PUBLIC_KEY_B64):')
print(base64.b64encode(public_bytes).decode())
"
```

산출물:
  - 공개키 → Step 1에서 loader.py의 PUBLIC_KEY_B64에 설정
  - 개인키 → Step 4에서 pharmsquare-server-main/.env에 설정
  - 양쪽 다 사용하므로 Step 0에서 미리 생성

### 0-2. 현재 변경사항 커밋

```
커밋 메시지: "크롤러 서버 배포 설계 문서 및 프로젝트 규칙 업데이트"
대상 파일:
  - docs/DESIGN_CRAWLER_DISTRIBUTION.md (신규)
  - docs/WORKFLOW_v2.md (신규)
  - docs/IMPLEMENTATION_PLAN.md (신규)
  - CLAUDE.md (변경)
  - .gitignore (변경)
  - src/domae_mcp/core/crawlers/registry.py (기존 변경)
  - src/domae_mcp/local/config.py (기존 변경)
  - tests/test_crawlers.py (기존 변경)
```

### 체크포인트
- [ ] git status 깨끗한 상태
- [ ] Ed25519 키페어 생성 완료 (안전한 곳에 백업)

---

## Step 1: CrawlerLoader + Registry 변경 (1일)

크롤러 동적 로드 시스템의 핵심 모듈을 구현한다.

### 1-1. 공개키를 loader.py에 설정 (5분)

```
Step 0에서 생성한 Ed25519 공개키를 loader.py의 PUBLIC_KEY_B64 상수에 설정.
(키페어 생성은 Step 0에서 이미 완료됨 — 중복 생성 금지)
```

### 1-2. loader.py 구현 (4시간)

```
파일: src/domae_mcp/core/crawlers/loader.py (신규)

구현할 것:
  class CrawlerLoader:
    __init__(base_dir, api_key)    # M1: ConfigManager.base_dir 전달
    load() → dict[str, type[BaseCrawler]]
    check_update() → bool
    _fetch_from_server() → Optional[dict]
    _verify_signature(bundle) → bool
    _save_to_cache(bundle)
    _load_from_cache() → Optional[dict]
    _load_cached_bundle() → Optional[dict]
    _is_expired(bundle) → bool
    _import_crawlers(bundle) → dict[str, type[BaseCrawler]]

의존성:
  - httpx (이미 requirements.txt에 있음)
  - cryptography (추가 필요)

테스트:
  - 서명 검증 로직 단위 테스트 (서버 없이, 로컬 키페어로)
  - 캐시 저장/로드 테스트
  - 만료 체크 테스트
  - 동적 import 테스트 (로컬 크롤러 파일로)
```

### 1-3. registry.py 변경 (1시간)

```
파일: src/domae_mcp/core/crawlers/registry.py (변경)

변경 내용:
  - _register_all() 함수 제거 (정적 import 제거)
  - register_all(crawlers: dict) 메서드 추가 (CrawlerLoader에서 일괄 등록)
  - is_loaded() 메서드 추가
  - 기존 register(), get(), list_all(), get_all()은 변경 없음

주의:
  - SearchService, OrderService가 CrawlerRegistry.get()을 호출하는 방식 변경 없음
```

### 1-4. __init__.py 변경 (15분)

```
파일: src/domae_mcp/core/crawlers/__init__.py (변경)

변경 내용:
  - 기존: 크롤러 파일들 import + _register_all() 호출
  - 변경: BaseCrawler, CrawlerRegistry, CrawlerLoader만 export
  - 크롤러 파일 import 전부 제거
```

### 1-5. requirements.txt 업데이트 (5분)

```
추가: cryptography>=42.0.0
```

### 1-6. 단위 테스트 (1시간)

```
파일: tests/test_loader.py (신규)

테스트 케이스:
  1. test_signature_verify_valid — 올바른 서명 검증 성공
  2. test_signature_verify_tampered — 변조된 번들 검증 실패
  3. test_cache_save_and_load — 캐시 저장 후 로드 (atomic write 확인)
  4. test_cache_expired — 만료된 캐시 거부
  5. test_import_crawler — .py 파일에서 BaseCrawler 서브클래스 로드
  6. test_api_key_hash_mismatch — 다른 키의 번들 거부
  7. test_empty_crawlers_warning — 번들에 크롤러 있는데 0개 로드 시 경고 (M6)

주의: 서명 테스트는 로컬 키페어 사용 (Step 0에서 생성한 키와 별개로 테스트용 키페어)
```

### 1-7. test_crawlers.py 수정 (누락3, 15분)

```
파일: tests/test_crawlers.py (변경)

변경:
  - 기존: CrawlerRegistry에서 정적 import된 크롤러 사용
  - 변경: 로컬 크롤러 파일을 직접 import하여 테스트
    → registry를 거치지 않고 직접 import
    → 또는 테스트용 번들 생성 → 로드 → 테스트
```

### 커밋
```
커밋 메시지: "크롤러 동적 로드 시스템 구현 (CrawlerLoader + Registry 변경)"
대상 파일:
  - src/domae_mcp/core/crawlers/loader.py (신규)
  - src/domae_mcp/core/crawlers/registry.py (변경)
  - src/domae_mcp/core/crawlers/__init__.py (변경)
  - requirements.txt (변경)
  - tests/test_loader.py (신규)
  - tests/test_crawlers.py (변경)
```

### 체크포인트
- [ ] test_loader.py 전체 통과
- [ ] 서명 검증 로직 정상 동작
- [ ] 동적 import로 크롤러 클래스 로드 확인

---

## Step 2: 앱 시작 흐름 연동 (0.5일)

서버 모드와 MCP 모드 양쪽에서 CrawlerLoader를 사용하도록 연동한다.

### 2-1. server.py 변경 (1시간)

```
파일: src/domae_mcp/local/server.py (변경)

변경 내용:
  lifespan()에 추가:
    1. config.get_api_key() 확인
    2. API 키 있으면 → CrawlerLoader.load() → CrawlerRegistry.register_all()
    3. API 키 없으면 → 경고 로그 + 크롤러 0개로 시작
    4. 로드 실패해도 서버는 시작 (설정 페이지 접근 필요하므로)
    5. app.state에 loader 저장 (런타임 리로드용)
```

### 2-2. mcp_server.py 변경 (1시간)

```
파일: src/domae_mcp/local/mcp_server.py (변경)

변경 내용:
  _init_services()에 추가:
    1. 동일한 CrawlerLoader 로직
    2. MCP 모드에서도 API 키 → 크롤러 로드
```

### 2-3. api_key.py 정리 (30분)

```
파일: src/domae_mcp/local/api_key.py (변경)

변경 내용:
  - verify() 로직을 CrawlerLoader와 역할 분리
  - CrawlerLoader가 API 키 검증 + 크롤러 배포를 담당
  - api_key.py는 heartbeat 전송만 담당 (또는 제거 검토)
  - m4: verify URL을 GET → POST로 변경, Bearer 헤더 사용
```

### 2-3.5. OrderService/SearchService 크롤러 없을 때 처리 (누락4, 30분)

```
파일: src/domae_mcp/core/services/search_service.py (변경)
파일: src/domae_mcp/core/services/order_service.py (변경)

변경:
  - CrawlerRegistry.get(name)이 KeyError를 던질 때
    기존: 500 에러 (unhandled)
    변경: "크롤러가 로드되지 않았습니다. API 키를 확인하세요." 메시지 반환

  - SearchService.search(): CrawlerRegistry.is_loaded() 체크
    → False면 즉시 빈 결과 + 경고 메시지 반환

  - OrderService.place_order(): CrawlerRegistry.is_loaded() 체크
    → False면 OrderResult(success=False, message="크롤러 미로드")
```

### 2-4. 로컬 통합 테스트 (1시간)

```
서버 없이 테스트 (캐시 모드):
  1. 크롤러 .py 파일들로 수동 번들 생성 (테스트용 스크립트)
  2. 번들을 캐시 디렉토리에 저장
  3. python -m domae_mcp 실행 → 크롤러 로드 확인
  4. curl /api/search?keyword=아모잘탄 → 결과 반환 확인

  테스트 스크립트: tests/create_test_bundle.py
    - 로컬 크롤러 파일 읽기
    - 로컬 키페어로 서명
    - bundle.json 생성 → 캐시 디렉토리에 저장
```

### 커밋
```
커밋 메시지: "앱 시작 시 크롤러 동적 로드 연동 (웹서버 + MCP)"
대상 파일:
  - src/domae_mcp/local/server.py (변경)
  - src/domae_mcp/local/mcp_server.py (변경)
  - src/domae_mcp/local/api_key.py (변경)
  - tests/create_test_bundle.py (신규)
```

### 체크포인트
- [ ] 테스트 번들로 python -m domae_mcp 시작 → 크롤러 로드 성공
- [ ] curl /api/search 동작 확인
- [ ] API 키 없이 시작 시 경고 + 크롤러 0개 확인
- [ ] MCP 모드에서도 크롤러 로드 확인

---

## Step 3: Prisma 스키마 + 마이그레이션 (0.5일)

팜스퀘어 서버에 도매 관련 테이블을 추가한다.

### 3-1. Prisma 스키마 추가 (30분)

```
파일: pharmsquare-server-main/prisma/schema.prisma (변경)

추가 모델 (C3/C4 수정 반영):
  - DomaeApiKey — String id + cuid(), keyHash + keyPrefix (raw 키 저장 안 함)
  - DomaeCrawler — String id + cuid()
  - pharmacyId는 String? (기존 Pharmacy.id가 String이므로)
  - Pharmacy 모델에 domaeApiKeys DomaeApiKey[] 관계 추가 (M5)

주의: 기존 스키마의 모든 모델이 String @id @default(cuid()) 사용.
     절대 Int @id @default(autoincrement()) 사용하지 말 것.
```

### 3-2. 마이그레이션 실행 (15분)

```bash
cd pharmsquare-server-main
npx prisma migrate dev --name add_domae_tables
```

### 3-3. 타입 생성 확인 (15분)

```bash
npx prisma generate
# → @prisma/client에 DomaeApiKey, DomaeCrawler 타입 생성 확인
```

### 커밋 (pharmsquare-server-main)
```
커밋 메시지: "도매 크롤러 배포용 DB 스키마 추가"
대상 파일:
  - prisma/schema.prisma (변경)
  - prisma/migrations/xxx_add_domae_tables/ (신규)
```

### 체크포인트
- [ ] prisma migrate dev 성공
- [ ] prisma generate 성공
- [ ] DomaeApiKey, DomaeCrawler 타입 사용 가능

---

## Step 4: 크롤러 배포 API + 서명 (1일)

팜스퀘어 서버에 크롤러 배포 API를 구현한다.

### 4-1. API 키 검증 미들웨어 (1시간)

```
파일: pharmsquare-server-main/src/lib/validateDomaeApiKey.ts (신규)

구현:
  - Authorization: Bearer dmk_xxx 헤더에서 키 추출
  - DB에서 키 조회 + isActive 확인
  - req.domaeApiKey에 레코드 저장
```

### 4-2. 크롤러 배포 라우터 (3시간)

```
파일: pharmsquare-server-main/src/routers/domae.ts (신규)

엔드포인트:
  GET  /api/domae/crawlers         — 서명된 번들 응답
  GET  /api/domae/crawlers/version — 해시만 (변경 체크)
  POST /api/domae/verify           — API 키 검증 + 정보
  POST /api/domae/heartbeat        — 사용 통계 수집

환경변수 추가:
  DOMAE_SIGNING_PRIVATE_KEY — Ed25519 개인키 (base64)
```

### 4-3. app.ts에 라우터 등록 (15분)

```
apiRouter.use('/domae', domaeRouter);
```

### 4-4. 서버 테스트 (1시간)

```
테스트 방법:
  1. 테스트용 API 키를 DB에 수동 삽입
  2. curl로 각 엔드포인트 호출
  3. 서명된 번들 응답 확인
  4. Python에서 서명 검증 확인

curl -H "Authorization: Bearer dmk_free_test123" \
  http://localhost:3001/api/domae/crawlers/version

curl -H "Authorization: Bearer dmk_free_test123" \
  http://localhost:3001/api/domae/crawlers
```

### 커밋 (pharmsquare-server-main)
```
커밋 메시지: "도매 크롤러 배포 API 구현 (서명 + 검증)"
대상 파일:
  - src/routers/domae.ts (신규)
  - src/lib/validateDomaeApiKey.ts (신규)
  - src/app.ts (변경 — 라우터 등록)
  - .env.example (변경 — DOMAE_SIGNING_PRIVATE_KEY 추가)
```

### 체크포인트
- [ ] GET /api/domae/crawlers → 서명된 번들 JSON 응답
- [ ] GET /api/domae/crawlers/version → 해시 응답
- [ ] POST /api/domae/verify → 키 정보 응답
- [ ] 잘못된 키 → 401/403
- [ ] Python에서 받은 번들의 서명 검증 성공

---

## Step 5: 크롤러 DB 시딩 (0.5일)

크롤러 원본 코드를 서버 DB에 등록한다.

### 5-1. 크롤러 원본 복사 (30분)

```
복사 경로:
  maipharm-domae-mcp/src/domae_mcp/core/crawlers/*.py
  → pharmsquare-server-main/prisma/seeds/domae-crawlers/*.py

대상 (10개):
  geoweb.py, boksan.py, inchun.py, tjpharm.py, hmpmall.py,
  beakje.py, picomall.py, saeropharm.py, sdpharm.py, upharmmall.py
```

### 5-2. 시드 스크립트 구현 (1시간)

```
파일: pharmsquare-server-main/prisma/seed-crawlers.ts (신규)

동작:
  1. seeds/domae-crawlers/ 디렉토리의 .py 파일 읽기
  2. 각 파일에서 SUPPLIER_NAME 추출
  3. SHA256 해시 계산
  4. prisma.domaeCrawler.upsert()로 DB 저장

실행: npx ts-node prisma/seed-crawlers.ts
```

### 5-3. 시딩 실행 + 확인 (30분)

```bash
npx ts-node prisma/seed-crawlers.ts
# → "10개 크롤러 시딩 완료" 확인

# DB 확인
npx prisma studio
# → DomaeCrawler 테이블에 10개 레코드
```

### 커밋 (pharmsquare-server-main)
```
커밋 메시지: "도매 크롤러 10개 DB 시딩"
대상 파일:
  - prisma/seeds/domae-crawlers/*.py (신규, 10개)
  - prisma/seed-crawlers.ts (신규)
```

### 체크포인트
- [ ] DB에 크롤러 10개 저장 확인
- [ ] GET /api/domae/crawlers → 10개 크롤러 코드 포함된 번들 반환

---

## Step 6: 통합 테스트 (0.5일)

로컬 클라이언트 ↔ 팜스퀘어 서버 전체 흐름을 테스트한다.

### 6-1. 테스트 API 키 발급 (15분)

```
팜스퀘어 DB에 테스트 키 삽입:
  key: dmk_free_testkey123
  keyHash: SHA256("dmk_free_testkey123")
  tier: "free"
  isActive: true
```

### 6-2. End-to-End 테스트 (2시간)

```
시나리오 1: 첫 실행 (캐시 없음)
  1. 팜스퀘어 서버 실행 (localhost:3001)
  2. config.json에 api_key: "dmk_free_testkey123" 설정
  3. 캐시 디렉토리 비우기
  4. python -m domae_mcp 실행
  5. 확인: 크롤러 10개 로드 로그
  6. 확인: ~/.maipharm-domae-mcp/crawlers/bundle.json 생성
  7. 확인: curl /api/search?keyword=아모잘탄 → 결과 반환

시나리오 2: 오프라인 (서버 중지)
  1. 팜스퀘어 서버 중지
  2. python -m domae_mcp 재시작
  3. 확인: 캐시에서 크롤러 로드 성공
  4. 확인: 검색 정상 동작

시나리오 3: API 키 없음
  1. config.json에 api_key: "" 설정
  2. 캐시 디렉토리 비우기
  3. python -m domae_mcp 실행
  4. 확인: 크롤러 0개 경고
  5. 확인: 설정 페이지 접근 가능 (서버는 시작됨)
  6. 확인: curl /api/search → 결과 없음 (에러 아닌 빈 결과)

시나리오 4: 잘못된 API 키
  1. config.json에 api_key: "dmk_free_invalid" 설정
  2. python -m domae_mcp 실행
  3. 확인: 서버 403 → 크롤러 0개
  4. 확인: 에러 메시지 "유효하지 않은 API 키"

시나리오 5: MCP 모드
  1. python -m domae_mcp --mcp 실행
  2. 확인: 크롤러 로드 성공
  3. 확인: search_inventory 도구 동작
```

### 6-3. 버그 수정 (1시간, 여유)

```
통합 테스트에서 발견된 문제 수정.
예상되는 이슈:
  - import 경로 문제 (크롤러 코드의 from domae_mcp.core.crawlers.base import ...)
  - 캐시 파일 권한 문제
  - C1은 설계 단계에서 수정 완료 (payload 원문 바이트 서명)
```

### 6-4. 크롤러 .py 파일 git rm (누락2)

```
통합 테스트 통과 확인 후, 크롤러 원본 파일을 Git에서 제거:

주의: .gitignore에 이미 등록되어 있지만, 기존 커밋에 포함된 파일은
      git rm --cached로 명시적으로 제거해야 함.

  git rm --cached src/domae_mcp/core/crawlers/geoweb.py
  git rm --cached src/domae_mcp/core/crawlers/boksan.py
  ... (이미 tracked인 파일만 대상)

  → 로컬 파일은 삭제하지 않음 (--cached 옵션)
  → 개발용으로 로컬에는 남겨두되, Git 이력에서만 제거
```

### 커밋
```
커밋 메시지: "크롤러 서버 배포 통합 테스트 및 크롤러 파일 비공개 전환"
대상 파일:
  - 버그 수정된 파일들
  - git rm --cached된 크롤러 파일들
```

### 체크포인트
- [ ] 시나리오 1~5 전체 통과
- [ ] 서명 검증 로컬 ↔ 서버 정상
- [ ] 오프라인 모드 정상
- [ ] MCP 모드 정상

---

## Step 7: 셋업 위자드 + API키 검증 UI (1.5일)

첫 실행 시 사용자를 안내하는 온보딩 위자드를 구현한다.

### 7-1. 백엔드 API 추가 (1시간)

```
파일: src/domae_mcp/local/routers/settings.py (변경)

추가 엔드포인트:
  GET  /api/settings/setup-status   → { api_key_set, credentials_count, telegram_set, crawlers_loaded }
  PUT  /api/settings/api-key        → API 키 저장
  POST /api/settings/api-key/verify → 서버 검증 + 크롤러 다운로드 트리거

API 키 변경 시 처리 (누락5):
  PUT /api/settings/api-key 핸들러에서:
    1. config에 새 API 키 저장
    2. 기존 캐시 무효화 (bundle.json 삭제 — api_key_hash 불일치이므로)
    3. 새 CrawlerLoader 인스턴스 생성
    4. loader.load() 호출 → 새 키로 크롤러 다운로드
    5. CrawlerRegistry.register_all() 갱신
    6. app.state.crawler_loader 교체
```

### 7-2. SetupPage.jsx 구현 (4시간)

```
파일: frontend/src/pages/SetupPage.jsx (신규)

3단계 위자드:
  Step 1: API 키 입력 + 검증
  Step 2: 도매 계정 등록 + 테스트
  Step 3: 텔레그램 설정 (선택)

첫 실행 감지:
  App.jsx에서 GET /api/settings/setup-status → 미완료면 /setup 리다이렉트
```

### 7-3. App.jsx 라우트 추가 (30분)

```
파일: frontend/src/App.jsx (변경)

추가:
  - /setup 라우트 → SetupPage
  - useEffect에서 setup-status 체크 → 리다이렉트 로직
```

### 7-4. SettingsPage API키 탭 완성 (1시간)

```
파일: frontend/src/pages/SettingsPage.jsx (변경)

변경:
  - handleVerify() stub → 실제 /api/settings/api-key/verify 호출
  - 검증 결과 표시 (약국명, 등급, 크롤러 로드 상태)
  - 팜스퀘어 가입 링크 추가
```

### 커밋
```
커밋 메시지: "셋업 위자드 및 API 키 검증 UI 구현"
대상 파일:
  - frontend/src/pages/SetupPage.jsx (신규)
  - frontend/src/App.jsx (변경)
  - frontend/src/pages/SettingsPage.jsx (변경)
  - src/domae_mcp/local/routers/settings.py (변경)
```

### 체크포인트
- [ ] 첫 실행 → /setup 자동 이동
- [ ] API 키 입력 → 검증 → 크롤러 다운로드 → "완료" 표시
- [ ] 도매 계정 테스트 동작
- [ ] 설정 완료 → 메인 검색 페이지로 이동
- [ ] 이후 실행 시 /setup 스킵

---

## Step 8: 검색 UX 개선 + MCP 가이드 (1일)

### 8-1. 미설정 도매상 안내 배너 (1시간)

```
파일: frontend/src/pages/SearchPage.jsx (변경)

추가:
  - GET /api/settings/credentials → 미설정 도매상 목록
  - 배너: "N개 도매상 미설정 — 설정하면 더 많은 결과"
  - 크롤러 0개일 때: "API 키를 먼저 등록하세요" 배너
```

### 8-2. 검색 로딩 개선 (30분)

```
파일: frontend/src/pages/SearchPage.jsx (변경)

변경:
  - 스피너 + "8개 도매상 검색 중... (보통 10~20초)" 텍스트
```

### 8-3. 스케줄 설정 min값 제한 (30분)

```
파일: frontend/src/pages/SettingsPage.jsx (변경)

변경:
  - 스케줄 탭: <input min="60"> 적용
  - "더 빠른 간격은 클라우드 버전 지원" 안내
```

### 8-4. MCP 연결 가이드 탭 (2시간)

```
파일: frontend/src/pages/SettingsPage.jsx (변경)

추가:
  - 5번째 탭 "MCP 연결"
  - Claude Desktop 설정 JSON 표시 + 복사 버튼
  - 사용 예시 문구
  - OS별 경로 안내
```

### 8-5. 클라우드 업그레이드 넛지 (1시간)

```
변경 파일: ProductsPage.jsx, HistoryPage.jsx, UrgentPage.jsx

추가:
  - 모니터링: "PC 꺼지면 중단. 클라우드는 24/7"
  - 주문이력: "로컬은 7일 보관. 클라우드는 최대 1년"
  - 긴급주문: "클라우드에서 24시간 자동 감시·주문"
```

### 커밋
```
커밋 메시지: "검색 UX 개선, MCP 가이드, 클라우드 넛지 추가"
대상 파일:
  - frontend/src/pages/SearchPage.jsx (변경)
  - frontend/src/pages/SettingsPage.jsx (변경)
  - frontend/src/pages/ProductsPage.jsx (변경)
  - frontend/src/pages/HistoryPage.jsx (변경)
  - frontend/src/pages/UrgentPage.jsx (변경)
```

### 체크포인트
- [ ] 미설정 도매상 배너 정상 표시
- [ ] MCP 탭 → 설정 JSON 복사 동작
- [ ] 스케줄 60분 미만 입력 불가
- [ ] 넛지 메시지 각 페이지에 표시

---

## Step 8.5: 프론트엔드 빌드 + static 배치 (누락6, 15분)

Step 7~8에서 수정한 프론트엔드를 빌드하여 서버에 배치한다.

```bash
cd frontend && npm run build
rm -rf ../src/domae_mcp/static/*
cp -r dist/* ../src/domae_mcp/static/
```

### 커밋
```
커밋 메시지: "프론트엔드 빌드 반영"
대상 파일:
  - src/domae_mcp/static/* (빌드 결과물)
```

### 체크포인트
- [ ] python -m domae_mcp → localhost:5900에서 수정된 UI 정상 표시

---

## Step 9: 어드민 API + API키 발급 UI (1일)

### 9-1. 어드민 라우터 (3시간)

```
파일: pharmsquare-server-main/src/routers/domae-admin.ts (신규)

엔드포인트:
  GET    /api/admin/domae/crawlers       → 크롤러 목록
  PUT    /api/admin/domae/crawlers/:name → 크롤러 코드 수정
  GET    /api/admin/domae/api-keys       → 키 목록 + 통계
  PUT    /api/admin/domae/api-keys/:id/revoke → 키 비활성화
  GET    /api/admin/domae/stats          → 대시보드 통계
```

### 9-2. API 키 발급 엔드포인트 (1시간)

```
파일: pharmsquare-server-main/src/routers/domae.ts (변경)

추가:
  POST /api/domae/api-keys → 로그인한 사용자에게 API 키 발급
  (세션 인증 필요)
```

### 9-3. 팜스퀘어 프론트 — API 키 발급 페이지 (2시간)

```
파일: pharmsquare-next-main (별도 레포) — 해당 시 진행

또는: 팜스퀘어 어드민 페이지에서 수동 발급 (간단 버전)
```

### 커밋 (pharmsquare-server-main)
```
커밋 메시지: "도매 어드민 API 및 API 키 발급 구현"
대상 파일:
  - src/routers/domae-admin.ts (신규)
  - src/routers/domae.ts (변경)
  - src/app.ts (변경 — 어드민 라우터 등록)
```

### 체크포인트
- [ ] 어드민 API로 크롤러 목록 조회 가능
- [ ] 어드민 API로 크롤러 코드 수정 → 버전 해시 변경 확인
- [ ] API 키 발급 → 발급된 키로 크롤러 다운로드 성공
- [ ] 키 비활성화 → 해당 키로 접근 불가

---

## Step 10: 최종 테스트 + 문서 업데이트 (0.5일)

### 10-1. 전체 E2E 테스트 (2시간)

```
시나리오 (처음부터 끝까지):
  1. 팜스퀘어 서버에서 API 키 발급
  2. maipharm-domae-mcp 설치 (git clone + pip install)
  3. python -m domae_mcp → localhost:5900
  4. 셋업 위자드 → API 키 입력 → 검증 → 크롤러 다운로드
  5. 도매 계정 등록 → 테스트
  6. 검색 → 주문 → 주문이력 확인
  7. MCP 모드 테스트 (Claude Desktop)
  8. 서버 중지 → 재시작 → 캐시에서 로드 확인
```

### 10-2. 문서 업데이트 (1시간)

```
업데이트 대상:
  - docs/WORKFLOW.md → "완료" 상태로 표기 또는 v2로 교체
  - docs/ARCHITECTURE.md → 크롤러 로드 방식 변경 반영
  - docs/USER_GUIDE.md → API 키 발급 + 셋업 위자드 설명 추가
  - README.md → 설치 가이드 업데이트
```

### 커밋
```
커밋 메시지: "v1.0.0 문서 업데이트 및 최종 정리"
```

### 체크포인트
- [ ] 전체 E2E 시나리오 통과
- [ ] 문서 최신 상태 반영

---

## 커밋 요약

| # | 레포 | 메시지 | 주요 파일 |
|---|------|--------|-----------|
| 0 | domae-mcp | 크롤러 서버 배포 설계 문서 및 프로젝트 규칙 업데이트 | docs/*, CLAUDE.md, .gitignore |
| 1 | domae-mcp | 크롤러 동적 로드 시스템 구현 | loader.py, registry.py, test_loader.py, test_crawlers.py |
| 2 | domae-mcp | 앱 시작 시 크롤러 동적 로드 연동 | server.py, mcp_server.py, search_service.py, order_service.py |
| 3 | pharmsquare | 도매 크롤러 배포용 DB 스키마 추가 | schema.prisma, migrations/ |
| 4 | pharmsquare | 도매 크롤러 배포 API 구현 | domae.ts, validateDomaeApiKey.ts |
| 5 | pharmsquare | 도매 크롤러 10개 DB 시딩 | seeds/domae-crawlers/, seed-crawlers.ts |
| 6 | domae-mcp | 크롤러 서버 배포 통합 테스트 및 크롤러 비공개 전환 | 버그 수정 + git rm --cached 크롤러 |
| 7 | domae-mcp | 셋업 위자드 및 API 키 검증 UI 구현 | SetupPage.jsx, settings.py |
| 8 | domae-mcp | 검색 UX 개선, MCP 가이드, 클라우드 넛지 추가 | SearchPage.jsx 외 |
| 8.5 | domae-mcp | 프론트엔드 빌드 반영 | src/domae_mcp/static/* |
| 9 | pharmsquare | 도매 어드민 API 및 API 키 발급 구현 | domae-admin.ts |
| 10 | domae-mcp | v1.0.0 문서 업데이트 및 최종 정리 | docs/* |

---

## 의존성 맵

```
Step 0 ──▶ Step 1 ──▶ Step 2 ──────────────────────────────▶ Step 6
                                                               │
           Step 3 ──▶ Step 4 ──▶ Step 5 ───────────────────▶ Step 6
                                                               │
                                                               ▼
                                                    Step 7 ──▶ Step 8 ──▶ Step 10
                                                               │
                                                    Step 9 ────┘
```

- Step 1~2 (로컬)와 Step 3~5 (서버)는 **병렬 진행 가능**
- Step 6 (통합)은 양쪽 완료 후
- Step 7~9는 Step 6 통과 후 순차 진행
- Step 9 (어드민)는 Step 7~8과 병렬 가능
