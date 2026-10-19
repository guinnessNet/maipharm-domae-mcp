"""도매 v2 기기 식별자 (스펙 B6, P6). device_id 는 로그에 앞 8자만 쓴다."""
import ctypes
import getpass
import hashlib
import platform
import sys
import uuid

from .encoding import lp

NONCE_LEN = 32


def machine_guid() -> str:
    if sys.platform != "win32":  # source 실행 전용(P17)
        node = uuid.getnode()
        if node & (1 << 40):  # 하드웨어 주소를 못 읽어 무작위 값이 나왔다 — 실행마다 바뀌므로 쓰지 않는다
            return f"host:{platform.node()}"
        return f"{node:012x}"
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                        winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
        value, kind = winreg.QueryValueEx(k, "MachineGuid")
    if kind != winreg.REG_SZ or not isinstance(value, str) or not value:
        raise OSError("machine_guid")
    return value


def user_sid() -> str:
    if sys.platform != "win32":
        return getpass.getuser()
    from ctypes import wintypes
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                             ctypes.POINTER(wintypes.DWORD)]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    TOKEN_QUERY, TOKEN_USER_CLASS = 0x0008, 1

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        raise OSError("user_sid")
    try:
        size = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, TOKEN_USER_CLASS, None, 0, ctypes.byref(size))
        if size.value == 0:
            raise OSError("user_sid")
        buf = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, TOKEN_USER_CLASS, buf, size, ctypes.byref(size)):
            raise OSError("user_sid")
        psid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]  # TOKEN_USER.User.Sid
        out = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(psid, ctypes.byref(out)):
            raise OSError("user_sid")
        try:
            return out.value
        finally:
            kernel32.LocalFree(ctypes.cast(out, ctypes.c_void_p))
    finally:
        kernel32.CloseHandle(token)


def device_id_hex(nonce: bytes) -> str:
    if not isinstance(nonce, (bytes, bytearray)) or len(nonce) != NONCE_LEN:
        raise ValueError("nonce")
    material = (lp(b"domae-device-v1") + lp(machine_guid().encode("utf-8")) + lp(user_sid().encode("utf-8"))
                + lp(bytes(nonce)))
    return hashlib.sha256(material).hexdigest()
