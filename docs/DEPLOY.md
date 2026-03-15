# 배포 가이드: maipharm-domae-mcp

## 1. 로컬 배포 (무료 버전)

### 1.1 GitHub 릴리스 배포

```bash
# 1) 프론트엔드 빌드 → static/ 배치
bash scripts/build_frontend.sh

# 2) static/ 빌드 결과물 커밋
git add src/domae_mcp/static/
git commit -m "프론트엔드 빌드 결과물 추가"

# 3) 태그 생성
git tag v1.0.0

# 4) 원격 저장소 푸시
git remote add origin https://github.com/maipharm/maipharm-domae-mcp.git
git push origin main --tags

# 5) GitHub Releases 페이지에서 v1.0.0 릴리스 생성
#    → Release notes에 설치 가이드 포함
```

### 1.2 사용자 설치 절차

사용자(약국)가 따라할 단계:

```bash
# 사전 요구: Python 3.10+, pip

# 1) 다운로드
git clone https://github.com/maipharm/maipharm-domae-mcp.git
cd maipharm-domae-mcp

# 2) 설치
pip install -e .

# 3) 실행
python -m domae_mcp

# 4) 브라우저에서 접속
#    http://localhost:5900
```

> 프론트엔드 빌드 결과물(static/)이 git에 포함되어 있으므로,
> 사용자는 Node.js 없이 Python만으로 설치 가능.

### 1.3 초기 설정

브라우저에서 localhost:5900 접속 후:

1. **설정 → API 키**: 팜스퀘어에서 발급받은 API 키 입력
2. **설정 → 도매 계정**: 사용 중인 도매상 ID/PW 입력 → [테스트] 버튼으로 확인
3. **설정 → 텔레그램** (선택): 봇 토큰 + Chat ID 입력
4. **통합검색**: 의약품 키워드 검색 → 주문

### 1.4 Windows 자동시작

```bash
# 등록 (PC 부팅 시 자동 실행)
python -m domae_mcp --install-startup

# 해제
python -m domae_mcp --uninstall-startup
```

### 1.5 MCP 모드 (Claude Desktop 연동)

`claude_desktop_config.json` 에 추가:

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

Claude Desktop 재시작 후 "아모잘탄 검색해줘" 등으로 사용 가능.

---

## 2. PyPI 배포 (선택)

pip install로 설치 가능하게 만들려면:

```bash
# 1) 빌드
pip install build
python -m build

# 2) 업로드
pip install twine
twine upload dist/*

# 3) 사용자 설치
pip install maipharm-domae-mcp
python -m domae_mcp
```

> 주의: PyPI 배포 시 static/ 빌드 결과물이 패키지에 포함되어야 함.
> pyproject.toml에 `[tool.setuptools.package-data]` 설정 필요:

```toml
[tool.setuptools.package-data]
domae_mcp = ["static/**/*"]
```

---

## 3. 클라우드 배포 (유료 버전)

> 별도 저장소: maipharm-domae-cloud (비공개)

### 3.1 서버 요구사항

| 항목 | 최소 사양 |
|------|----------|
| OS | Ubuntu 22.04 LTS |
| CPU | 2 vCPU |
| RAM | 4 GB |
| Storage | 20 GB SSD |
| Docker | 24.0+ |
| Docker Compose | 2.20+ |

### 3.2 배포 절차

```bash
# 1) 서버에 저장소 클론
git clone https://github.com/maipharm/maipharm-domae-cloud.git
cd maipharm-domae-cloud

# 2) 환경변수 설정
cp .env.example .env
vi .env
# DATABASE_URL=postgresql://domae:password@db:5432/domae
# REDIS_URL=redis://redis:6379
# PHARMSQUARE_API_URL=https://api.pharmsquare.com
# PHARMSQUARE_SERVICE_KEY=ps_service_key_xxx
# KAKAO_API_KEY=kakao_api_key_xxx
# ENCRYPTION_MASTER_KEY=your-256-bit-key
# SECRET_KEY=your-jwt-secret

# 3) Docker Compose 실행
docker compose up -d

# 4) 초기 DB 마이그레이션
docker compose exec api python -m cloud.database init

# 5) 확인
docker compose ps
curl https://domae.pharmsquare.com/api/health
```

### 3.3 docker-compose.yml 구조

```yaml
services:
  api:
    build: .
    ports: ["8000:8000"]
    env_file: .env
    depends_on: [db, redis]
    restart: unless-stopped

  worker:
    build: .
    command: celery -A cloud.scheduler worker -l info
    env_file: .env
    depends_on: [db, redis]
    restart: unless-stopped

  beat:
    build: .
    command: celery -A cloud.scheduler beat -l info
    env_file: .env
    depends_on: [db, redis]
    restart: unless-stopped

  redis:
    image: redis:7-alpine
    restart: unless-stopped

  db:
    image: postgres:16-alpine
    volumes: ["pgdata:/var/lib/postgresql/data"]
    environment:
      POSTGRES_DB: domae
      POSTGRES_USER: domae
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    restart: unless-stopped

volumes:
  pgdata:
```

### 3.4 Nginx 리버스 프록시

```nginx
server {
    listen 443 ssl http2;
    server_name domae.pharmsquare.com;

    ssl_certificate     /etc/letsencrypt/live/pharmsquare.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/pharmsquare.com/privkey.pem;

    # API
    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # 프론트엔드
    location / {
        root /var/www/domae-cloud/static;
        try_files $uri /index.html;
    }
}

# HTTP → HTTPS 리다이렉트
server {
    listen 80;
    server_name domae.pharmsquare.com;
    return 301 https://$host$request_uri;
}
```

### 3.5 SSL 인증서 (Let's Encrypt)

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d domae.pharmsquare.com
```

### 3.6 모니터링

```bash
# 로그 확인
docker compose logs -f api
docker compose logs -f worker

# 서비스 상태
docker compose ps

# DB 백업 (매일 cron)
0 3 * * * docker compose exec db pg_dump -U domae domae > /backup/domae_$(date +\%Y\%m\%d).sql
```

---

## 4. 업데이트 배포

### 4.1 로컬 버전 업데이트

```bash
cd maipharm-domae-mcp
git pull origin main
pip install -e .
# 서버 재시작 (자동시작 중이면 PC 재부팅)
```

### 4.2 클라우드 버전 업데이트

```bash
cd maipharm-domae-cloud
git pull origin main
docker compose build
docker compose up -d
# 무중단 배포:
# docker compose up -d --no-deps --build api worker beat
```

---

## 5. 트러블슈팅

### 포트 충돌

```bash
# 5900 포트 사용 중인 프로세스 확인
lsof -i :5900
# 다른 포트로 실행
python -m domae_mcp --port 5901
```

### DB 초기화

```bash
# 로컬 DB 삭제 (설정은 유지)
rm ~/.maipharm-domae-mcp/data/domae.db
# 서버 재시작 시 자동 재생성
```

### 크롤러 로그인 실패

1. 해당 도매상 웹사이트에서 직접 로그인 확인
2. 설정 → 도매 계정 → [테스트] 버튼으로 연결 확인
3. 도매상 사이트 변경 시 크롤러 업데이트 필요 (GitHub Issues로 보고)

### MCP 모드 연결 안 됨

1. `python -m domae_mcp --mcp` 단독 실행하여 에러 확인
2. claude_desktop_config.json 경로가 맞는지 확인
3. Claude Desktop 완전 종료 후 재시작
