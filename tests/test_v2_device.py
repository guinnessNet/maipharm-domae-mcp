"""도매 v2 기기 식별자(device) 시험. 실제 MachineGuid·SID 값은 출력하지 않는다."""
import hashlib
import re
import sys

import pytest

from domae_mcp.v2 import device
from domae_mcp.v2.encoding import lp


def test_device_id_formula(monkeypatch):
    monkeypatch.setattr(device, "machine_guid", lambda: "11111111-2222-3333-4444-555555555555")
    monkeypatch.setattr(device, "user_sid", lambda: "S-1-5-21-1-2-3-1001")
    nonce = b"\x07" * 32
    want = hashlib.sha256(lp(b"domae-device-v1") + lp(b"11111111-2222-3333-4444-555555555555")
                          + lp(b"S-1-5-21-1-2-3-1001") + lp(nonce)).hexdigest()
    got = device.device_id_hex(nonce)
    assert got == want
    assert re.fullmatch(r"[0-9a-f]{64}", got)
    assert device.device_id_hex(b"\x08" * 32) != got


def test_device_id_rejects_bad_nonce():
    with pytest.raises(ValueError):
        device.device_id_hex(b"\x00" * 31)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 레지스트리·토큰")
def test_device_id_windows_sources():
    assert re.fullmatch(r"[0-9a-fA-F-]{36}", device.machine_guid())
    assert device.user_sid().startswith("S-1-5-")
    assert re.fullmatch(r"[0-9a-f]{64}", device.device_id_hex(b"\x01" * 32))



@pytest.mark.skipif(sys.platform == "win32", reason="source 실행 전용 경로")
def test_machine_guid_source_uses_real_hwaddr(monkeypatch):
    monkeypatch.setattr(device.uuid, "getnode", lambda: 0x0223456789AB)
    assert device.machine_guid() == "0223456789ab"


@pytest.mark.skipif(sys.platform == "win32", reason="source 실행 전용 경로")
def test_device_id_stable_when_hwaddr_random(monkeypatch):
    # getnode() 가 실행마다 다른 무작위 값(멀티캐스트 비트)을 돌려줘도 같은 nonce 면 device_id 가 같아야 한다
    nonce = b"\x07" * 32
    ids = set()
    for node in (0x0223456789AB | (1 << 40), 0x1A2B3C4D5E6F | (1 << 40)):
        monkeypatch.setattr(device.uuid, "getnode", lambda n=node: n)
        assert device.machine_guid() == device.SOURCE_NO_HWADDR
        ids.add(device.device_id_hex(nonce))
    assert len(ids) == 1
    assert device.device_id_hex(b"\x08" * 32) not in ids
