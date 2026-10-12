import base64
import json
from pathlib import Path

import pytest

from domae_mcp.v2.bundle_crypto import (BundleContext, CryptoError, decrypt_modules, derive_session_key,
                                        encrypt_module, kdf_info, module_aad, x25519_shared)

V = json.loads((Path(__file__).parent / "fixtures" / "domae_v2_vectors.json").read_text(encoding="utf-8"))
I, K = V["inputs"], V["keys"]
d = base64.b64decode


def ctx(**over):
    base = dict(protocol_version=I["protocol_version"], request_id=d(I["request_id_b64"]),
                client_pub=d(K["client_pub_b64"]), server_pub=d(K["server_pub_b64"]),
                key_fp=bytes.fromhex(V["key_fp"]["hex"]), device_id=bytes.fromhex(I["device_id_hex"]),
                bundle_hash=I["bundle_hash"], release_seq=I["release_seq"], runtime_id=I["runtime_id"])
    base.update(over)
    return BundleContext(**base)


def manifest():
    return {n: d(m["artifact_sha256"]) for n, m in V["modules"].items()}


def enc_modules():
    return {n: {"iv": m["iv"], "ct": m["ct"], "tag": m["tag"]} for n, m in V["modules"].items()}


def session_key():
    shared = x25519_shared(d(I["client_priv_b64"]), d(K["server_pub_b64"]))
    return derive_session_key(shared, d(I["salt_b64"]), ctx())


def test_shared_both_sides_match_vector():
    assert x25519_shared(d(I["client_priv_b64"]), d(K["server_pub_b64"])).hex() == K["shared_hex"]
    assert x25519_shared(d(I["server_priv_b64"]), d(K["client_pub_b64"])).hex() == K["shared_hex"]


def test_kdf_info_aad_session_key_match_vector():
    assert kdf_info(ctx()).hex() == V["encoding"]["kdf_info_hex"]
    for n, m in V["modules"].items():
        assert module_aad(ctx(), n, d(m["artifact_sha256"])).hex() == V["encoding"]["aad"][n]
    assert session_key().hex() == K["session_key_hex"]


def test_encrypt_matches_vector_and_decrypt_roundtrip():
    sk = session_key()
    for n, m in V["modules"].items():
        assert encrypt_module(sk, ctx(), n, d(m["plain_b64"]), d(m["iv"])) == {"iv": m["iv"], "ct": m["ct"], "tag": m["tag"]}
    out = decrypt_modules(sk, ctx(), enc_modules(), manifest())
    assert out == {n: d(m["plain_b64"]) for n, m in V["modules"].items()}


@pytest.mark.parametrize("field", ["request_id", "key_fp", "device_id"])
def test_context_change_breaks_gcm(field):
    other = ctx(**{field: b"\x00" * len(getattr(ctx(), field))})
    with pytest.raises(CryptoError) as e:
        decrypt_modules(session_key(), other, enc_modules(), manifest())
    assert e.value.reason == "gcm"


def test_release_seq_and_runtime_change_breaks_gcm():
    for other in (ctx(release_seq=43), ctx(runtime_id={**I["runtime_id"], "loader_abi": 2})):
        with pytest.raises(CryptoError):
            decrypt_modules(session_key(), other, enc_modules(), manifest())


def test_tag_and_iv_tamper_rejected():
    for field in ("tag", "iv"):
        mods = enc_modules()
        raw = bytearray(d(mods["alpha"][field]))
        raw[0] ^= 1
        mods["alpha"][field] = base64.b64encode(bytes(raw)).decode()
        with pytest.raises(CryptoError):
            decrypt_modules(session_key(), ctx(), mods, manifest())


def test_duplicate_iv_rejected():
    mods = enc_modules()
    mods["beta"]["iv"] = mods["alpha"]["iv"]
    with pytest.raises(CryptoError) as e:
        decrypt_modules(session_key(), ctx(), mods, manifest())
    assert e.value.reason == "duplicate_iv"


def test_module_set_must_equal_manifest():
    mods = enc_modules()
    del mods["beta"]
    with pytest.raises(CryptoError) as e:
        decrypt_modules(session_key(), ctx(), mods, manifest())
    assert e.value.reason == "module_set"


def test_bad_lengths_rejected():
    with pytest.raises(CryptoError):
        x25519_shared(d(I["client_priv_b64"]), b"\x01" * 31)
    mods = enc_modules()
    mods["alpha"]["iv"] = base64.b64encode(b"\x00" * 11).decode()
    with pytest.raises(CryptoError):
        decrypt_modules(session_key(), ctx(), mods, manifest())


def test_all_zero_shared_rejected():
    with pytest.raises(CryptoError) as e:
        x25519_shared(d(I["client_priv_b64"]), b"\x00" * 32)
    assert e.value.reason == "zero_shared"
