"""도매 v2 키 슬롯 (스펙 B1 첫 키 입력과 설정 재검증, B 완료 기준 4, P13).

키마다 v2/keys/<key_fp_hex>/ 에 bearer 의 DPAPI 사본(api_key.dpapi)과 번들 캐시를 둔다. 어느 키가 활성인지는
단일 포인터 v2/active.dpapi 가 정한다. 키 전환의 커밋은 이 포인터 하나의 os.replace 뿐이다(롤백 쓰기 없음).
"""
import json
import logging
import shutil
from pathlib import Path

from .encoding import EncodingError, hexd, key_fp_from_bearer
from .winprotect import Protector, ProtectError, atomic_write_bytes, purpose, quarantine

logger = logging.getLogger(__name__)


class KeySlots:
    def __init__(self, v2_root: Path, prot: Protector):
        self._root = Path(v2_root)
        self._keys = self._root / "keys"
        self._ptr = self._root / "active.dpapi"
        self._prot = prot

    def cache_dir(self, key_fp: bytes) -> Path:
        return self._keys / key_fp.hex()

    def _key_file(self, key_fp: bytes) -> Path:
        return self.cache_dir(key_fp) / "api_key.dpapi"

    def active_fp(self) -> bytes | None:
        if not self._ptr.exists():
            return None
        try:
            blob = self._ptr.read_bytes()
        except OSError:  # 백신 잠금 같은 일시 오류는 격리하지 않는다
            logger.warning("v2 활성 키 포인터를 지금 읽지 못했다")
            return None
        try:
            d = json.loads(self._prot.unprotect(blob, purpose("active")).decode("utf-8"))
            return hexd(d["key_fp"], 32)
        except (ProtectError, ValueError, KeyError, TypeError, EncodingError):
            quarantine(self._ptr)
            logger.warning("v2 활성 키 포인터를 읽지 못해 격리했다")
            return None

    def active(self) -> tuple[bytes, str] | None:
        fp = self.active_fp()
        if fp is None:
            return None
        bearer = self.read_bearer(fp)
        return (fp, bearer) if bearer is not None else None

    def read_bearer(self, key_fp: bytes) -> str | None:
        f = self._key_file(key_fp)
        if not f.exists():
            return None
        try:
            blob = f.read_bytes()
        except OSError:
            logger.warning("v2 키 파일을 지금 읽지 못했다")
            return None
        try:
            bearer = self._prot.unprotect(blob, purpose("api-key", key_fp)).decode("utf-8")
            if key_fp_from_bearer(bearer) != key_fp:
                raise ValueError("key_fp")
            return bearer
        except (ProtectError, ValueError, EncodingError):
            quarantine(f)
            logger.warning("v2 키 파일을 읽지 못해 격리했다")
            return None

    def stage(self, bearer: str) -> Path:
        fp = key_fp_from_bearer(bearer)
        d = self.cache_dir(fp)
        d.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(self._key_file(fp), self._prot.protect(bearer.encode("utf-8"), purpose("api-key", fp)))
        return d

    def commit(self, key_fp: bytes) -> None:
        if not self._key_file(key_fp).exists() or self.read_bearer(key_fp) is None:
            raise FileNotFoundError("staged key missing")
        body = json.dumps({"key_fp": key_fp.hex()}, separators=(",", ":")).encode("utf-8")
        atomic_write_bytes(self._ptr, self._prot.protect(body, purpose("active")))

    def cleanup(self) -> None:
        """활성이 아닌 keys/* 를 지운다. 실패는 경고만 남기고 다음 시작에 다시 한다.
        키 전환이 진행 중일 때 부르면 후보 폴더가 지워지므로 호출자가 직렬화한다."""
        if not self._keys.exists():
            return
        pointer_exists = self._ptr.exists()
        active = self.active_fp()
        if active is None and pointer_exists:  # 포인터가 있는데 지금 읽지 못하면 아무것도 지우지 않는다
            return
        for d in self._keys.iterdir():
            if active is not None and d.name == active.hex():
                continue
            try:
                shutil.rmtree(d) if d.is_dir() else d.unlink()
            except OSError:
                logger.warning("v2 이전 키 폴더 정리 실패(다음 시작에 다시 한다)")
