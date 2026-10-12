import json
from pathlib import Path

import pytest

from domae_mcp.v2.encoding import (EncodingError, b64d, b64e, encode_runtime_id, hexd, key_fp_from_bearer,
                                   key_fp_from_hex, lp, u64, validate_bundle_hash, validate_epoch,
                                   validate_protocol_version, validate_release_seq)

V = json.loads((Path(__file__).parent / "fixtures" / "domae_v2_vectors.json").read_text(encoding="utf-8"))


def test_lp_and_u64():
    assert lp(b"ab") == b"\x00\x00\x00\x02ab"
    assert u64(1) == b"\x00" * 7 + b"\x01"
    with pytest.raises(EncodingError):
        u64(-1)
    with pytest.raises(EncodingError):
        u64(True)


def test_runtime_id_matches_vector():
    assert encode_runtime_id(V["inputs"]["runtime_id"]).hex() == V["encoding"]["runtime_id_hex"]


def test_runtime_id_rejects_missing_or_extra_field():
    rt = dict(V["inputs"]["runtime_id"])
    with pytest.raises(EncodingError):
        encode_runtime_id({k: v for k, v in rt.items() if k != "platform"})
    with pytest.raises(EncodingError):
        encode_runtime_id({**rt, "extra": "x"})


def test_key_fp_matches_vector():
    assert key_fp_from_bearer(V["key_fp"]["bearer"]).hex() == V["key_fp"]["hex"]
    assert key_fp_from_hex(V["key_fp"]["hex"]) == key_fp_from_bearer(V["key_fp"]["bearer"])
    assert b64e(key_fp_from_hex(V["key_fp"]["hex"])) == V["key_fp"]["b64"]


@pytest.mark.parametrize("case", V["release_seq_cases"])
def test_release_seq_cases(case):
    if case["valid"]:
        assert validate_release_seq(case["value"]) == case["value"]
    else:
        with pytest.raises(EncodingError):
            validate_release_seq(case["value"])


def test_release_seq_rejects_bool():
    with pytest.raises(EncodingError):
        validate_release_seq(True)


def test_epoch_range():
    assert validate_epoch(0) == 0
    for bad in (-1, 1.0, "1", True, 2**53):
        with pytest.raises(EncodingError):
            validate_epoch(bad)


def test_b64_strict_rejects_noncanonical():
    assert b64d("AAEC") == b"\x00\x01\x02"
    for bad in ("AAE", "AA-_", "AAEC\n", " AAEC", "AAEC==", "AAE==", "AA="):
        with pytest.raises(EncodingError):
            b64d(bad)
    with pytest.raises(EncodingError):
        b64d("AAEC", size=4)


def test_hex_strict():
    assert hexd("00ff", 2) == b"\x00\xff"
    for bad in ("00FF", "0g00", "00f", "00ff00"):
        with pytest.raises(EncodingError):
            hexd(bad, 2)


@pytest.mark.parametrize("case", V["loader_abi_cases"])
def test_loader_abi_cases(case):
    rt = {**V["inputs"]["runtime_id"], "loader_abi": case["value"]}
    if case["valid"]:
        encode_runtime_id(rt)
    else:
        with pytest.raises(EncodingError):
            encode_runtime_id(rt)


def test_bundle_hash_and_protocol_version():
    assert validate_bundle_hash("e" * 64) == "e" * 64
    for bad in ("x", "E" * 64, "e" * 63, 1):
        with pytest.raises(EncodingError):
            validate_bundle_hash(bad)
    assert validate_protocol_version(2) == 2
    for bad in (0, -1, 2.0, True, "2"):
        with pytest.raises(EncodingError):
            validate_protocol_version(bad)


def test_lone_surrogate_rejected_as_encoding_error():
    with pytest.raises(EncodingError):
        key_fp_from_bearer("a\ud800")
    for k in ("implementation", "python_version", "artifact_kind", "platform"):
        with pytest.raises(EncodingError):
            encode_runtime_id({**V["inputs"]["runtime_id"], k: "x\ud800"})


def test_u64_range_matches_typescript():
    assert u64(2**53 - 1) == (2**53 - 1).to_bytes(8, "big")
    for bad in (2**53, 2**64 - 1, -1, True, False, 1.0, "1", None):
        with pytest.raises(EncodingError) as e:
            u64(bad)
        assert e.value.args == ("u64",)


@pytest.mark.parametrize("bad", ["\ud800", "\udc00"])
def test_lone_surrogate_reason_matches_typescript(bad):
    with pytest.raises(EncodingError) as e:
        key_fp_from_bearer(f"b{bad}")
    assert e.value.args == ("bearer",)
    for k in ("implementation", "python_version", "artifact_kind", "platform"):
        with pytest.raises(EncodingError) as e:
            encode_runtime_id({**V["inputs"]["runtime_id"], k: f"x{bad}y"})
        assert e.value.args == ("runtime_id",)
