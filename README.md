# maipharm-domae-mcp

의약품 도매 통합 검색/주문 MCP 서버

8개 도매상(지오영, 복산, 인천, 티제이팜, HMP, 백제, 피코, 새로팜)의 재고를 통합 검색하고, 주문까지 자동화하는 로컬 도구입니다.

## 주요 기능

- **통합 재고 검색** — 8개 도매상 동시 검색, 가격/재고 비교
- **주문 자동화** — 검색 결과에서 바로 주문
- **긴급주문** — 품절 제품 입고 감지 시 자동 주문
- **재고 모니터링** — 등록 제품의 재고 변동 감지 + 텔레그램 알림
- **AI 연동** — Claude Desktop 등 MCP 클라이언트에서 AI로 활용

## 설치

```bash
git clone https://github.com/xxx/maipharm-domae-mcp
cd maipharm-domae-mcp
pip install -r requirements.txt
```

## 사용법

### 웹 UI로 사용
```bash
python -m domae_mcp
```
브라우저에서 http://localhost:5900 접속

### Claude Desktop에서 AI로 사용

`claude_desktop_config.json`에 추가:
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

### Windows 자동시작 등록
```bash
python -m domae_mcp --install-startup
```
PC 부팅 시 자동으로 백그라운드 실행됩니다.

## 초기 설정

1. 웹 UI 접속 (http://localhost:5900)
2. 설정 페이지에서 도매별 아이디/비밀번호 입력
3. (선택) 텔레그램 봇 토큰/채팅 ID 입력
4. (선택) 모니터링 제품 등록

## 사용 가능한 도매상

| 도매상 | 검색 | 주문 | 비고 |
|--------|------|------|------|
| 지오영 | O | O | |
| 복산 | O | O | |
| 인천 | O | O | |
| 티제이팜 | O | O | |
| HMP | O | X | 결제 필요 |
| 백제 | O | O | |
| 피코 | O | X | |
| 새로팜 | O | X | |

## 신규 도매상 요청

설정 페이지의 "신규 도매 요청" 버튼을 통해 새로운 도매상 크롤러 개발을 요청할 수 있습니다.

## 업데이트

새 버전이 출시되면 웹 UI 상단에 알림이 표시됩니다.
```bash
git pull
pip install -r requirements.txt
```

## 라이선스

Private - All rights reserved
