"""크롤러 번들 클라이언트 전용 복호화 키 (Step 1.5).

이 상수는 로컬 MCP가 서버 /crawlers?encrypted=1 응답을 메모리에서 복호화할 때만 사용.
서버는 동일한 키를 DOMAE_CRAWLER_CLIENT_KEY 환경변수로 보유.

⚠️ 보안 한계 (Step 1.5):
- 이 constant는 PyInstaller exe에 포함되며, 리버스 엔지니어링(pyinstxtractor + decompyle3)으로 추출 가능.
- Step 1.5의 목표는 "메모장으로 bundle.json 한 번에 못 읽게" 하는 중급 장벽 구축.
- Step 2 (Nuitka 네이티브 컴파일) 또는 Step 3 (라이선스 서버 런타임 키 발급)으로 고도화 예정.

⚠️ 키 관리:
- DB at-rest 암호화에 쓰는 DOMAE_CRAWLER_KEY와 **반드시 분리**.
- 이 키가 exe 리버싱으로 노출되어도 DB 크레덴셜(DOMAE_CREDENTIAL_KEY)은 안전하게 유지.
- 키 회전 시 이 파일과 서버 DOMAE_CRAWLER_CLIENT_KEY를 동시에 업데이트하고 exe 재배포 필요.
"""

# 32바이트 base64 (AES-256 키)
_CRAWLER_CLIENT_KEY_B64 = "c1GLcQE16kES33zFpzV+El3vcFEX1Ll+VPM11L8D2D8="
