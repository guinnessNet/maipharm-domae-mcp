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
