"""도매 v2 로컬 보호 저장 (스펙 B2·B10). CurrentUser DPAPI 만 쓴다. 실패하면 다른 방식으로 대신하지 않는다."""
import ctypes
import glob
import os
import sys
import tempfile
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from .encoding import lp

CRYPTPROTECT_UI_FORBIDDEN = 0x1
_DEV_MAGIC = b"DEVPLAIN1"


class ProtectError(Exception):
    """reason: "protect" | "unprotect" | "unavailable" """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Protector(Protocol):
    protected: bool

    def protect(self, data: bytes, purpose: bytes) -> bytes: ...

    def unprotect(self, blob: bytes, purpose: bytes) -> bytes: ...


def purpose(name: str, *parts: bytes) -> bytes:
    """DPAPI entropy. 용도 이름과 묶을 값을 길이 접두로 잇는다(P5)."""
    return lp(b"domae-v2/" + name.encode("ascii")) + b"".join(lp(p) for p in parts)


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(b: bytes):
    buf = ctypes.create_string_buffer(b, len(b))
    return _Blob(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf  # buf 는 호출 동안 살아 있어야 한다


class DpapiProtector:
    protected = True

    def __init__(self):
        try:
            c = ctypes.WinDLL("crypt32", use_last_error=True)
            k = ctypes.WinDLL("kernel32", use_last_error=True)
        except (AttributeError, OSError):
            raise ProtectError("unavailable") from None
        self._free = k.LocalFree
        self._free.argtypes = [ctypes.c_void_p]
        self._free.restype = ctypes.c_void_p
        self._protect, self._unprotect = c.CryptProtectData, c.CryptUnprotectData
        P = ctypes.POINTER(_Blob)
        self._protect.argtypes = [P, wintypes.LPCWSTR, P, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, P]
        self._unprotect.argtypes = [P, ctypes.c_void_p, P, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, P]
        self._protect.restype = self._unprotect.restype = wintypes.BOOL

    def _run(self, fn, data: bytes, entropy: bytes, reason: str) -> bytes:
        din, _k1 = _blob(data)
        ent, _k2 = _blob(entropy)
        out = _Blob()
        if not fn(ctypes.byref(din), None, ctypes.byref(ent), None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
            raise ProtectError(reason)
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            self._free(ctypes.cast(out.pbData, ctypes.c_void_p))

    def protect(self, data: bytes, purpose: bytes) -> bytes:
        return self._run(self._protect, bytes(data), purpose, "protect")

    def unprotect(self, blob: bytes, purpose: bytes) -> bytes:
        return self._run(self._unprotect, bytes(blob), purpose, "unprotect")


class DevPlainProtector:
    """Windows 가 아닌 source 실행 전용(P17). 보호하지 않는다. purpose 만 맞춰 본다."""
    protected = False

    def protect(self, data: bytes, purpose: bytes) -> bytes:
        return _DEV_MAGIC + lp(purpose) + bytes(data)

    def unprotect(self, blob: bytes, purpose: bytes) -> bytes:
        head = _DEV_MAGIC + lp(purpose)
        if not blob.startswith(head):
            raise ProtectError("unprotect")
        return blob[len(head):]


def default_protector(kind: str) -> Protector:
    if sys.platform == "win32":
        return DpapiProtector()
    if kind == "source":
        return DevPlainProtector()
    raise ProtectError("unavailable")


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """임시 파일 → flush → fsync → os.replace (P4). 실패하면 원래 파일은 그대로이고 임시 파일을 지운다."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        try:
            view = memoryview(bytes(data))
            while view:
                n = os.write(fd, view)
                view = view[n:]
            os.fsync(fd)
        finally:
            os.close(fd)
        if sys.platform != "win32":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


QUARANTINE_KEEP = 3  # 같은 파일의 격리 사본은 최근 것만 남긴다


def quarantine(path: Path) -> Path | None:
    path = Path(path)
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')  # 마이크로초까지: 정리 뒤 같은 이름을 다시 쓰지 않게
    dest, i = path.with_name(f"{path.name}.bad-{stamp}"), 1
    while dest.exists():  # 같은 초의 두 번째 격리가 앞 사본을 덮지 않게 한다
        dest, i = path.with_name(f"{path.name}.bad-{stamp}-{i}"), i + 1
    os.replace(path, dest)
    _prune_quarantine(path, dest)
    return dest


def _prune_quarantine(path: Path, keep: Path) -> None:
    prefix = f"{path.name}.bad-"

    def order(f: Path) -> tuple:
        stamp, _, n = f.name[len(prefix):].partition("-")
        return stamp, int(n) if n.isdigit() else 0

    # 방금 만든 사본은 시계가 뒤로 가 이름이 오래돼 보여도 남긴다
    copies = sorted((f for f in path.parent.glob(f"{glob.escape(prefix)}*") if f.is_file() and f != keep), key=order)
    for old in copies[:len(copies) - (QUARANTINE_KEEP - 1)]:
        try:
            old.unlink()
        except OSError:
            pass
