import base64
import json
from pathlib import Path

import pytest

from domae_mcp.v2.envelope import (DOMAIN_BUNDLE, DOMAIN_RELEASE, DOMAIN_REVOCATION, DOMAIN_UPDATE,
                                   EnvelopeError, signed_digest, verify_envelope)

V = json.loads((Path(__file__).parent / "fixtures" / "domae_v2_vectors.json").read_text(encoding="utf-8"))
TRUST = {V["inputs"]["signing_key_id"]: base64.b64decode(V["inputs"]["signing_pub_b64"])}
DOMAINS = [DOMAIN_BUNDLE, DOMAIN_RELEASE, DOMAIN_REVOCATION, DOMAIN_UPDATE]


def env(domain):
    s = V["signatures"][domain]
    return {"payload": s["payload"], "signing_key_id": s["signing_key_id"], "signature": s["signature"]}


@pytest.mark.parametrize("domain", DOMAINS)
def test_digest_and_verify_match_vector(domain):
    s = V["signatures"][domain]
    assert signed_digest(domain, s["signing_key_id"], s["payload"]).hex() == s["digest_hex"]
    assert verify_envelope(env(domain), domain, TRUST) == s["payload"]


def test_cross_domain_rejected():
    with pytest.raises(EnvelopeError) as e:
        verify_envelope(env(DOMAIN_RELEASE), DOMAIN_BUNDLE, TRUST)
    assert e.value.reason == "bad_signature"


def test_unknown_signing_key_rejected():
    with pytest.raises(EnvelopeError) as e:
        verify_envelope({**env(DOMAIN_BUNDLE), "signing_key_id": "other"}, DOMAIN_BUNDLE, TRUST)
    assert e.value.reason == "unknown_signing_key"


def test_payload_tamper_rejected():
    e0 = env(DOMAIN_BUNDLE)
    with pytest.raises(EnvelopeError):
        verify_envelope({**e0, "payload": e0["payload"] + " "}, DOMAIN_BUNDLE, TRUST)


def test_envelope_malformed_fields():
    e0 = env(DOMAIN_BUNDLE)
    cases = [
        {**e0, "payload": 1},
        {**e0, "signature": base64.b64encode(b"x" * 63).decode()},
        {**e0, "signature": "not base64!"},
        {k: v for k, v in e0.items() if k != "signing_key_id"},
        "not a dict",
    ]
    for c in cases:
        with pytest.raises(EnvelopeError) as e:
            verify_envelope(c, DOMAIN_BUNDLE, TRUST)
        assert e.value.reason == "malformed"
