import base64

import pytest

from domae_mcp.v2.verify import (MAX_LIFETIME_S, ClientIdentity, MinPolicy, PayloadRejected, PendingRequest, check_loaded,
                           merge_min_policy, parse_payload, verify_bundle_payload, verify_release_payload,
                           verify_revocation_payload)

KEY_FP = bytes(range(32))
FP_B64 = base64.b64encode(KEY_FP).decode()
DEV = "ab" * 32
RT = {"implementation": "cpython", "python_version": "3.12.10", "magic": "cb0d0d0a",
      "artifact_kind": "marshal-code", "loader_abi": 1, "platform": "win_amd64"}
IDENT = ClientIdentity(key_fp=KEY_FP, device_id_hex=DEV, runtime_id=RT)
REQ = "AAAAAAAAAAAAAAAAAAAAAA=="                       # 16바이트 0
OTHER_REQ = "AQAAAAAAAAAAAAAAAAAAAA=="
PUB = "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE="  # 32바이트 0x01
PENDING = PendingRequest(request_id_b64=REQ, client_pub_b64=PUB)
NO_POLICY = MinPolicy(min_release_seq=1, min_protocol_version=1)
T0 = 1_800_000_000
H = 3600


def bundle(**over):
    p = {"protocol_version": 2, "request_id": REQ, "client_pub": PUB, "key_fp": FP_B64, "device_id": DEV,
         "device_generation": 1, "runtime_id": RT, "bundle_hash": "e" * 64, "issued_at": T0,
         "expires_at": T0 + 72 * H, "offline_deadline": T0 + 144 * H, "release_seq": 10,
         "manifest": {"min_release_seq": 1, "min_protocol_version": 2}}
    p.update(over)
    return p


def ok(p, mode="network", now=T0 + 60, highest=0, policy=NO_POLICY, gen=None):
    return verify_bundle_payload(p, IDENT, mode, now, highest, policy, gen, PENDING if mode == "network" else None)


def rejects(reason, *a, **k):
    with pytest.raises(PayloadRejected) as e:
        ok(*a, **k)
    assert e.value.reason == reason


def test_network_happy_path_returns_policy_and_generation():
    v = ok(bundle())
    assert v.policy == MinPolicy(1, 2) and v.device_generation == 1


def test_network_request_binding():
    rejects("request_mismatch", bundle(request_id=OTHER_REQ))
    rejects("request_mismatch", bundle(client_pub="AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI="))


def test_network_freshness_window():
    ok(bundle(), now=T0 + 600)
    rejects("stale", bundle(), now=T0 + 601)
    rejects("stale", bundle(), now=T0 - 601)


def test_cache_mode_boundaries():
    ok(bundle(), mode="cache", now=T0 + 11 * 60)                              # 발급 11분 뒤: 통과
    ok(bundle(), mode="cache", now=T0 + 72 * H + 1)                           # 72시간 직후 오프라인: 통과
    rejects("offline_deadline", bundle(), mode="cache", now=T0 + 144 * H)     # 144시간 경계: 거부


def test_identity_binding():
    rejects("identity", bundle(key_fp=base64.b64encode(bytes(32)).decode()))
    rejects("identity", bundle(device_id="cd" * 32))
    rejects("identity", bundle(runtime_id={**RT, "loader_abi": 2}))
    rejects("identity", bundle(protocol_version=3))


def test_runtime_type_confusion_rejected():
    rejects("malformed", bundle(runtime_id={**RT, "loader_abi": True}))
    rejects("malformed", bundle(protocol_version=2.0))


def test_lifetime_ordering_and_max():
    rejects("lifetime", bundle(expires_at=T0 - 1))
    rejects("lifetime", bundle(offline_deadline=T0 + 72 * H - 1))
    rejects("lifetime", bundle(offline_deadline=T0 + MAX_LIFETIME_S + 1))


def test_release_seq_rollback_rejected_in_verify():
    rejects("downgrade", bundle(release_seq=9), highest=10)
    ok(bundle(release_seq=10), highest=10)


def test_min_release_seq_returned_then_enforced():
    v = ok(bundle(release_seq=9, manifest={"min_release_seq": 10, "min_protocol_version": 2}))
    assert v.policy == MinPolicy(10, 2)                     # 검증은 통과하고 정책을 돌려준다
    with pytest.raises(PayloadRejected) as e:
        check_loaded(v.policy, 9)                            # 저장 뒤 집행에서 거부
    assert e.value.reason == "downgrade"


def test_new_min_protocol_is_stored_then_blocks_everything():
    store = {"policy": NO_POLICY}
    v = rel(release(min_protocol_version=3), policy=store["policy"])   # 버전 확인이 새 최소 프로토콜 3 을 알림
    store["policy"] = v.policy                                         # 호출자 계약: 검증된 정책을 먼저 저장
    assert store["policy"] == MinPolicy(5, 3)
    with pytest.raises(PayloadRejected) as e:                          # 현재 실행 거부
        check_loaded(store["policy"], 10)
    assert e.value.reason == "protocol_too_old"
    # 이후 통신 실패·재시작: 캐시 번들 검증은 통과해도 저장된 정책으로 집행하면 계속 거부
    cached = ok(bundle(), mode="cache", now=T0 + H, policy=store["policy"])
    assert cached.policy == MinPolicy(5, 3)
    with pytest.raises(PayloadRejected) as e:
        check_loaded(cached.policy, 10)
    assert e.value.reason == "protocol_too_old"
    # 번들 manifest 가 직접 올린 경우도 같다
    v2 = ok(bundle(manifest={"min_release_seq": 1, "min_protocol_version": 3}))
    with pytest.raises(PayloadRejected):
        check_loaded(v2.policy, 10)


def test_rejected_response_policy_is_not_returned():
    with pytest.raises(PayloadRejected) as e:                          # 만료된 응답: 정책을 돌려주지 않는다
        rel(release(min_protocol_version=3), now=T0 + 300)
    assert e.value.reason == "stale"
    with pytest.raises(PayloadRejected) as e:
        rel(release(min_protocol_version=3, request_id=OTHER_REQ))
    assert e.value.reason == "request_mismatch"


def test_check_loaded_uses_stored_policy():
    check_loaded(MinPolicy(10, 2), loaded_release_seq=10)
    for policy, seq, reason in ((MinPolicy(10, 3), 10, "protocol_too_old"), (MinPolicy(11, 2), 10, "downgrade")):
        with pytest.raises(PayloadRejected) as e:
            check_loaded(policy, seq)
        assert e.value.reason == reason


def test_bundle_generation_required_and_monotonic():
    rejects("malformed", bundle(device_generation=None))
    rejects("malformed", {k: v for k, v in bundle().items() if k != "device_generation"})
    for bad in (True, -1, 1.5):
        rejects("malformed", bundle(device_generation=bad))
    rejects("old_generation", bundle(device_generation=1), gen=2)
    assert ok(bundle(device_generation=3), gen=2).device_generation == 3


def test_bundle_hash_format():
    rejects("malformed", bundle(bundle_hash="x"))
    rejects("malformed", bundle(manifest={"min_release_seq": 1, "min_protocol_version": -1}))


def test_bool_time_rejected():
    rejects("malformed", bundle(issued_at=True))
    rejects("malformed", bundle(release_seq=True))


def test_duplicate_json_keys_rejected():
    with pytest.raises(PayloadRejected) as e:
        parse_payload('{"a":1,"a":2}')
    assert e.value.reason == "malformed"
    assert parse_payload('{"a":1}') == {"a": 1}


def release(**over):
    p = {"protocol_version": 2, "request_id": REQ, "key_fp": FP_B64, "device_id": DEV, "device_generation": 1,
         "bundle_hash": "e" * 64, "release_seq": 10, "min_release_seq": 5, "min_protocol_version": 2,
         "issued_at": T0, "valid_until": T0 + 300}
    p.update(over)
    return p


def rel(p, now=T0 + 10, highest=10, policy=NO_POLICY, registered=True, gen=None):
    return verify_release_payload(p, IDENT, REQ, now, highest, policy, registered, gen)


def rel_rejects(reason, *a, **k):
    with pytest.raises(PayloadRejected) as e:
        rel(*a, **k)
    assert e.value.reason == reason


def test_release_happy_path():
    v = rel(release())
    assert v.policy == MinPolicy(5, 2) and v.release_seq == 10 and v.device_generation == 1


def test_release_rejections():
    rel_rejects("request_mismatch", release(request_id=OTHER_REQ))
    rel_rejects("identity", release(device_id="cd" * 32))
    rel_rejects("stale", release(), now=T0 + 300)
    rel_rejects("downgrade", release(release_seq=9))


def test_release_validity_window_exact():
    rel_rejects("malformed", release(valid_until=T0 + 86400))
    rel_rejects("malformed", release(valid_until=T0 - 1))
    rel(release(), now=T0 + 299)


def test_release_generation_by_registration_state():
    no_gen = {k: v for k, v in release().items() if k != "device_generation"}
    assert rel(no_gen, registered=False).device_generation is None      # 첫 설치(미등록): 누락 허용
    rel_rejects("malformed", no_gen, registered=True)                    # 등록 기기: 누락 거부
    rel_rejects("malformed", release(device_generation=True))
    rel_rejects("malformed", release(device_generation=-1))
    rel_rejects("old_generation", release(device_generation=1), gen=2)
    assert rel(release(device_generation=3), gen=2).device_generation == 3   # 재승인 뒤 새 세대


def test_min_policy_only_rises():
    stored = MinPolicy(min_release_seq=7, min_protocol_version=2)
    assert merge_min_policy(stored, MinPolicy(5, 2)) == stored
    assert merge_min_policy(stored, MinPolicy(9, 3)) == MinPolicy(9, 3)
    assert rel(release(min_release_seq=5), policy=stored).policy == stored


def revocation(**over):
    p = {"error": "key_revoked", "scope": "key", "request_id": REQ, "key_fp": FP_B64, "device_id": DEV,
         "revoked_at": T0 - 5, "issued_at": T0, "valid_until": T0 + 300}
    p.update(over)
    return p


def test_revocation_key_scope_ignores_generation():
    assert verify_revocation_payload(revocation(), IDENT, REQ, T0 + 1, stored_generation=99) == "key"


def test_revocation_device_scope_generation():
    p = revocation(error="device_revoked", scope="device", device_generation=3)
    assert verify_revocation_payload(p, IDENT, REQ, T0 + 1, stored_generation=3) == "device"
    with pytest.raises(PayloadRejected) as e:
        verify_revocation_payload(p, IDENT, REQ, T0 + 1, stored_generation=4)
    assert e.value.reason == "old_generation"
    with pytest.raises(PayloadRejected):
        verify_revocation_payload(revocation(error="device_revoked", scope="device"), IDENT, REQ, T0 + 1, None)


def test_revocation_replay_binding_and_window():
    for p, now in ((revocation(request_id=OTHER_REQ), T0 + 1), (revocation(), T0 + 300),
                   (revocation(key_fp=base64.b64encode(bytes(32)).decode()), T0 + 1),
                   (revocation(valid_until=T0 + 86400), T0 + 1)):
        with pytest.raises(PayloadRejected):
            verify_revocation_payload(p, IDENT, REQ, now, stored_generation=None)
