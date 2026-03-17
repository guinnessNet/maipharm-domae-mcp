# 마이팜 도매 통합검색

로컬 설치형 의약품 도매 통합 검색/주문 도구. 14개 도매상의 재고를 한 번에 검색하고, 최저가로 주문할 수 있습니다.

> **처음 사용하시나요?** → [사용 가이드 (그림 포함)](docs/USER_GUIDE.md)를 먼저 읽어보세요.

## 주요 기능

- **통합 검색**: 14개 도매상 재고를 동시에 검색, 가격 비교
- **원클릭 주문**: 검색 결과에서 바로 주문
- **긴급주문**: 품절 제품 등록 → 재고 입고 시 자동 주문
- **재고 모니터링**: 등록 제품의 가격/재고 변동 추적 + 추이 그래프
- **텔레그램 알림**: 변동 감지 시 즉시 알림
- **MCP 연동**: Claude Desktop에서 자연어로 검색/주문
- **Windows 자동시작**: PC 부팅 시 백그라운드 실행

## 지원 도매상 (14개)

| 도매상 | 검색 | 주문 | 비고 |
|--------|:----:|:----:|------|
| 지오영 | O | O | |
| 복산 | O | O | NicePharm |
| 인천 | O | O | NicePharm |
| 티제이팜 | O | O | |
| 백제 | O | O | JWT 인증 |
| 신덕팜 | O | O | |
| 대전동원약품 | O | O | |
| 경동사 | O | O | |
| 도현팜 | O | O | NicePharm |
| 삼성팜 | O | O | |
| 훼미리팜 | O | O | |
| HMP | O | X | DWR 프로토콜 |
| 피코 | O | X | |
| 새로팜 | O | X | |

## 설치

### Windows (exe)

[최신 버전 다운로드](https://github.com/guinnessNet/maipharm-domae-mcp/releases/latest/download/MaipharmDomae.exe) → 실행 → 브라우저 자동 열림

### Windows (배치파일)

```
1. 최신 릴리즈의 zip 다운로드
2. 압축 해제
3. install.bat 더블클릭 (Python 자동 설치)
4. start.bat 더블클릭
```

### Python (개발자)

```bash
pip install -e .
python -m domae_mcp
# → http://localhost:5900
```

### 초기 설정

1. 브라우저에서 `http://localhost:5900` 접속
2. **설정 > API 키**: 팜스퀘어에서 발급받은 API 키 입력
3. **설정 > 도매 계정**: 사용 중인 도매상 ID/PW 입력 → [테스트]로 확인
4. **설정 > 텔레그램** (선택): 봇 토큰 + Chat ID 입력
5. **통합검색**에서 의약품 검색 시작

## 사용법

### 웹 UI 모드 (기본)

```bash
python -m domae_mcp          # http://localhost:5900
python -m domae_mcp --port 5901  # 포트 변경
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

### Windows 자동시작

```bash
python -m domae_mcp --install-startup    # 등록
python -m domae_mcp --uninstall-startup  # 해제
```

## 보안

- 도매상 아이디/비밀번호는 **Fernet(AES) 암호화**되어 저장됩니다
- 암호화 키는 `~/.maipharm-domae-mcp/.key`에 자동 생성됩니다
- 평문으로 보관되지 않으며, 검색/주문 실행 시에만 복호화합니다

## 클라우드 서비스

팜스퀘어 회원은 클라우드 모니터링을 이용할 수 있습니다:

| | 로컬 (무료) | 베이직 | 프로 |
|---|:---:|:---:|:---:|
| 통합 검색/주문 | O | O | O |
| 운영 시간 | PC 켜져있을 때 | 24시간 | 24시간 |
| 모니터링 간격 | 60분 | 30분 | 5분 |
| 알림 | 텔레그램 | 텔레그램 | 텔레그램 + 카카오톡 |

## 개발

### 프로젝트 구조

```
src/domae_mcp/
├── core/               # 공유 코어
│   ├── crawlers/       # 14개 도매상 크롤러
│   ├── services/       # 검색, 주문, 모니터링
│   └── models/         # SQLAlchemy 모델
├── local/              # 로컬 모드
│   ├── server.py       # FastAPI 웹서버 (포트 5900)
│   ├── mcp_server.py   # MCP stdio 서버
│   └── routers/        # REST API
├── cloud/              # 클라우드 워커 (Redis BRPOP)
├── desktop/            # Windows 트레이 앱
└── static/             # React 빌드 결과물
```

### 문서

- [아키텍처](docs/ARCHITECTURE.md)
- [API 명세](docs/API_SPEC.md)
- [DB 스키마](docs/DATABASE_SCHEMA.md)
- [사용 가이드](docs/USER_GUIDE.md)

## 라이선스

[Business Source License 1.1 (BUSL-1.1)](LICENSE)

- 개인/단일 약국의 로컬 사용: 무료
- 교육/학습 목적: 무료
- 호스팅/SaaS/상업적 재배포: 별도 라이선스 필요
- 2029-03-15 이후 Apache 2.0으로 자동 전환

문의: kjh@maipharm.com
