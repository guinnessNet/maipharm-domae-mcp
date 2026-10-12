# 도매 v2 암호 계약 (B-1)

정본 스펙: openpharm `docs/plans/domae/LOCAL_CRAWLER_DISTRIBUTION_HARDENING_SPEC.md` §7 B1·B2·B5.
공용 테스트 벡터: worker `tests/fixtures/domae_v2_vectors.json` = server `src/lib/domaeV2/__fixtures__/domae-v2-vectors.json`(바이트 동일, sha256 은 server `vectorsFile.spec.ts`).
벡터 갱신은 worker `scripts/gen_domae_v2_vectors.py` 로만 하고 두 레포에 같은 커밋 단위로 복사한다.

| 항목 | 값 |
|---|---|
| LP | 4바이트 BE 길이 + 바이트 |
| 정수 | 8바이트 BE unsigned. release_seq·protocol_version 은 1~2^53-1, 시각·loader_abi·device_generation 은 0~2^53-1 의 정수, bool·소수·문자열 거부 |
| 시각 필드 | UTC epoch 초 정수 |
| bundle_hash | 소문자 hex 64자 문자열, UTF-8 로 LP |
| device_id | 소문자 hex 64자, 원시 32바이트로 LP |
| key_fp | SHA256(bearer UTF-8) 32바이트. wire 는 base64 |
| base64 | 표준 알파벳·패딩 필수·정규 형태만 |
| 서명 | Ed25519(SHA256(LP(domain) ‖ LP(signing_key_id) ‖ LP(payload))), domain 4종 |
| KDF | HKDF-SHA256(shared, salt 32B, info 스펙 B1), 32바이트 |
| 모듈 | AES-256-GCM, IV 12B 모듈별·중복 금지, tag 16B, AAD 스펙 B1 |

server 는 서명·암호화(엔드포인트는 B-3), worker 는 검증·복호화(`verify.py` 의 network·cache 모드, release·revocation 검증; 로더 연결은 B-4)를 쓴다.

## 호출 순서와 전제

클라이언트(worker)는 아래 순서를 지킨다. 앞 단계가 실패하면 뒤 단계를 실행하지 않는다.

1. `verify_envelope` 로 서명을 검증한다.
2. `parse_payload` 에는 서명이 확인된 envelope 의 원문 문자열만 넘긴다. 다시 직렬화한 값이나 다른 경로로 받은 값을 넘기지 않는다.
3. `verify_*_payload` 로 신뢰·요청 바인딩·시간·세대·`release_seq` 역행을 검증한다. 이 함수들은 서명과 JSON 형식을 다시 검사하지 않는 순수 정책 함수다.
4. 돌려받은 정책(최소 `release_seq`·최소 프로토콜), 최고 `release_seq`, 기기 세대를 낮아지지 않도록 단조 저장한다.
5. `check_loaded(저장 정책, 실행할 release_seq)` 로 집행한다. 재시작 직후, 통신 실패 뒤, 이미 로드한 실행 참조를 쓸 때를 포함해 모든 진입점에서 같은 검사를 한다.
6. 세션 키를 유도하고 `decrypt_modules` 로 복호화한다.
7. 복호화한 모듈을 로드한다.

전제는 다음과 같다.

- cache 모드에 넘기는 `now` 는 B-4 가 시계 역행 검사를 거쳐 만든 `effective_now` 다.
- 서버는 요청마다 새 X25519 키와 salt 를 만들고 세션 키를 재사용하지 않는다. 운영 경로에서는 `ivFor`(고정 IV)를 쓰지 않으며, 이 인자는 시험 전용이다. 한 payload 의 모든 모듈은 `encryptModules` 한 번에 처리한다. IV 중복 검사가 호출 한 번 안에서만 유지되기 때문이다.
- 정수를 `1.0` 처럼 표기한 JSON 숫자를 어떻게 처리할지(JSON 숫자 토큰 규칙)는 B-3 에서 정한다.
- `VerifiedBundle` 은 정책과 세대만 돌려준다. 따라서 B-4 는 salt·server_pub·manifest 해시를 payload 에서 다시 읽어야 하며, 이때 같은 엄격 디코더(`b64d`)를 쓴다.
