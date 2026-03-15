# 마이팜 도매 통합검색

로컬 설치형 의약품 도매 통합 검색/주문 도구. 8개 도매상의 재고를 한 번에 검색하고, 최저가로 주문할 수 있습니다.

> **처음 사용하시나요?** → [사용 가이드 (그림 포함)](docs/USER_GUIDE.md)를 먼저 읽어보세요.

## 주요 기능

- **통합 검색**: 8개 도매상 재고를 동시에 검색, 가격 비교
- **원클릭 주문**: 검색 결과에서 바로 주문
- **긴급주문**: 재고 감지 시 자동 주문 (품절약 확보)
- **가격 모니터링**: 등록 제품의 가격/재고 변동 추적
- **텔레그램 알림**: 변동 감지 시 즉시 알림
- **MCP 연동**: Claude Desktop에서 자연어로 검색/주문
- **Windows 자동시작**: PC 부팅 시 백그라운드 실행

## 지원 도매상

| 도매상 | 검색 | 주문 | 비고 |
|--------|:----:|:----:|------|
| 지오영 | O | O | |
| 복산 | O | O | NicePharm |
| 인천 | O | O | NicePharm |
| 티제이팜 | O | O | 2단계 로그인 |
| 백제 | O | O | JWT 인증 |
| HMP | O | X | DWR 프로토콜 |
| 피코 | O | X | |
| 새로팜 | O | X | |

## 설치

### 사전 요구사항

- Python 3.10 이상
- pip

### 설치 방법

```bash
# 1. 다운로드
git clone https://github.com/maipharm/maipharm-domae-mcp.git
cd maipharm-domae-mcp

# 2. 설치
pip install -e .

# 3. 실행
python -m domae_mcp

# 4. 브라우저에서 접속
# http://localhost:5900
```

> 프론트엔드 빌드 결과물이 포함되어 있으므로 Node.js는 필요 없습니다.

### 초기 설정

1. 브라우저에서 `http://localhost:5900` 접속
2. **설정 > API 키**: 팜스퀘어에서 발급받은 API 키 입력
3. **설정 > 도매 계정**: 사용 중인 도매상 ID/PW 입력 → [테스트]로 확인
4. **설정 > 텔레그램** (선택): 봇 토큰 + Chat ID 입력
5. **통합검색**에서 의약품 검색 시작

## 사용법

### 웹 UI 모드 (기본)

```bash
python -m domae_mcp
# http://localhost:5900
```

포트 변경:

```bash
python -m domae_mcp --port 5901
```

### MCP 모드 (Claude Desktop)

```bash
python -m domae_mcp --mcp
```

Claude Desktop 설정 (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "domae": {
      "command": "python",
      "args": ["-m", "domae_mcp", "--mcp"]
    }
  }
}
```

사용 예시:
- "아모잘탄 검색해줘"
- "지오영에서 아모잘탄 5개 주문해줘"
- "모니터링 시작해줘"

### Windows 자동시작

```bash
# 등록 (PC 부팅 시 자동 실행)
python -m domae_mcp --install-startup

# 해제
python -m domae_mcp --uninstall-startup
```

## 설정 파일

설정은 `~/.maipharm-domae-mcp/` 에 저장됩니다:

```
~/.maipharm-domae-mcp/
├── config.json    # 도매 계정 (암호화), 텔레그램, API 키
├── .key           # 암호화 키 (자동 생성)
└── data/
    └── domae.db   # SQLite 데이터베이스
```

## 클라우드 서비스

팜스퀘어 유료회원은 클라우드 서비스를 이용할 수 있습니다:

| | 로컬 (무료) | 베이직 (5만/월) | 프로 (10만/월) |
|---|:---:|:---:|:---:|
| 모든 기능 | O | O | O |
| 운영 시간 | PC 켜져있을 때 | 24시간 | 24시간 |
| 모니터링 간격 | 60분 | 30분 | 5분 |
| 알림 | 텔레그램 | 텔레그램 + 카카오톡 | 텔레그램 + 카카오톡 |

## 개발

### 프로젝트 구조

```
src/domae_mcp/
├── core/               # 공유 코어
│   ├── crawlers/       # 8개 도매상 크롤러
│   ├── services/       # 검색, 주문, 모니터링, 텔레그램
│   └── models/         # SQLAlchemy 모델
├── local/              # 로컬 모드
│   ├── server.py       # FastAPI 웹서버
│   ├── mcp_server.py   # MCP stdio 서버
│   ├── config.py       # 설정 관리
│   ├── database.py     # SQLite
│   ├── scheduler.py    # 스케줄러 (60분 최소)
│   └── routers/        # REST API 라우터
└── static/             # React 빌드 결과물
```

### 프론트엔드 빌드

```bash
cd frontend
npm install
npm run build
bash ../scripts/build_frontend.sh
```

### 크롤러 추가

`BaseCrawler`를 상속하여 `login()`, `search()`를 구현하고 `registry.py`에 등록합니다.

### 설계 문서

- [아키텍처](docs/ARCHITECTURE.md)
- [API 명세](docs/API_SPEC.md)
- [DB 스키마](docs/DATABASE_SCHEMA.md)
- [비즈니스 아키텍처](docs/BUSINESS_ARCHITECTURE.md)
- [구현 워크플로우](docs/WORKFLOW.md)
- [배포 가이드](docs/DEPLOY.md)

## 신규 도매상 요청

설정 페이지의 "신규 도매 요청"에서 새로운 도매상 크롤러 개발을 요청할 수 있습니다.

## 업데이트

새 버전 출시 시 웹 UI 상단에 알림이 표시됩니다:

```bash
cd maipharm-domae-mcp
git pull
pip install -e .
```

## 라이선스

[Business Source License 1.1 (BUSL-1.1)](LICENSE)

- 개인/단일 약국의 로컬 사용: 무료
- 교육/학습 목적: 무료
- 호스팅/SaaS/상업적 재배포: 별도 라이선스 필요
- 2029-03-15 이후 Apache 2.0으로 자동 전환

문의: kjh@maipharm.com
