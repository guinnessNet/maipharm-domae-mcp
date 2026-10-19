"""도매 v2 manifest 검사·marshal 로드 (스펙 B3 runtime_id·ABI, P14). 전부 아니면 무.

이 모듈은 서명·GCM 을 통과한 바이트만 받는다. 호출 순서는 loader.py(Task 6)·gate.py(Task 7)가 보장한다.
원문 컴파일 경로(compile·exec(str))는 없다.
"""
import hashlib
import marshal
import re
import sys
import types

from domae_mcp import __version__
from domae_mcp.core.crawlers.base import BASE_ABI, BaseCrawler
from domae_mcp.core.crawlers.cart_snapshot import CART_ABI

from .encoding import EncodingError, b64d, validate_safe_int

MODULE_PREFIX = "domae_crawlers."
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024    # 모듈당 상한. server release.ts 와 같은 값(P14)
_ARTIFACT_KEYS = {"magic", "python_version", "sha256", "size"}
_VERSION = re.compile(r"\A([0-9]+)\.([0-9]+)\.([0-9]+)\Z")


class LoadFailed(Exception):
    """reason: manifest | runtime | abi | client_too_old | too_large | artifact | not_code | exec | class"""

    def __init__(self, reason: str, module: str | None = None):
        super().__init__(reason if module is None else f"{reason}:{module}")
        self.reason = reason
        self.module = module


def _version_tuple(v: str) -> tuple[int, int, int] | None:
    m = _VERSION.match(v) if isinstance(v, str) else None
    return tuple(int(x) for x in m.groups()) if m else None


def manifest_sha256(manifest: dict) -> dict[str, bytes]:
    try:
        return {n: b64d(a["sha256"], 32) for n, a in manifest["artifacts"].items()}
    except (EncodingError, KeyError, TypeError, AttributeError):
        raise LoadFailed("manifest") from None


def check_manifest(manifest: dict, module_names: set[str], runtime_id: dict, client_version: str = __version__) -> None:
    if not isinstance(manifest, dict):
        raise LoadFailed("manifest")
    req, arts = manifest.get("required_modules"), manifest.get("artifacts")
    names_ok = isinstance(req, list) and all(isinstance(n, str) and n.isascii() and n.isidentifier() for n in req)
    if not names_ok or len(set(req)) != len(req):
        raise LoadFailed("manifest")
    if not isinstance(arts, dict) or not req or not (set(req) == set(module_names) == set(arts)):
        raise LoadFailed("manifest")
    for name in sorted(arts):
        a = arts[name]
        if not isinstance(a, dict) or set(a) != _ARTIFACT_KEYS:
            raise LoadFailed("manifest", name)
        if a["magic"] != runtime_id["magic"] or a["python_version"] != runtime_id["python_version"]:
            raise LoadFailed("runtime", name)
        try:
            size = validate_safe_int(a["size"], 0, "size")
        except EncodingError:
            raise LoadFailed("manifest", name) from None
        if size > MAX_ARTIFACT_BYTES:
            raise LoadFailed("too_large", name)
    manifest_sha256(manifest)
    if manifest.get("base_abi") != BASE_ABI or manifest.get("cart_abi") != CART_ABI \
            or isinstance(manifest.get("base_abi"), bool) or isinstance(manifest.get("cart_abi"), bool):
        raise LoadFailed("abi")
    need, have = _version_tuple(manifest.get("min_client_version")), _version_tuple(client_version)
    if need is None or have is None:
        raise LoadFailed("manifest")
    if need > have:
        raise LoadFailed("client_too_old")


class ModuleSwap:
    """성공한 로드의 되돌리기 핸들. 커밋 뒤 finalize(), 커밋 실패 시 rollback()."""

    def __init__(self, names: list[str], previous: dict[str, types.ModuleType | None]):
        self._names = names
        self._previous = previous

    def rollback(self) -> None:
        for full in self._names:
            old = self._previous.get(full)
            if old is None:
                sys.modules.pop(full, None)
            else:
                sys.modules[full] = old

    def finalize(self) -> None:
        keep = set(self._names)
        for full in [m for m in list(sys.modules) if m.startswith(MODULE_PREFIX) and m not in keep]:
            sys.modules.pop(full, None)


def _crawler_class(module: types.ModuleType) -> tuple[type, str]:
    name = module.__name__[len(MODULE_PREFIX):]
    try:
        found = [v for v in module.__dict__.values()
                 if isinstance(v, type) and issubclass(v, BaseCrawler) and v is not BaseCrawler
                 and v.__module__ == module.__name__]
        supplier = getattr(found[0], "SUPPLIER_NAME", "") if len(found) == 1 else None
    except Exception:
        raise LoadFailed("class", name) from None
    if len(found) != 1 or not isinstance(supplier, str) or not supplier:
        raise LoadFailed("class", name)
    return found[0], supplier


def unload_all() -> None:
    """철회 때 쓴다. 이미 로드한 v2 크롤러 모듈 참조를 sys.modules 에서 지운다(P15)."""
    for full in [m for m in list(sys.modules) if m.startswith(MODULE_PREFIX)]:
        sys.modules.pop(full, None)


def load_modules(plain: dict[str, bytes], manifest: dict) -> tuple[dict[str, type], ModuleSwap]:
    shas = manifest_sha256(manifest)
    arts = manifest["artifacts"]
    if not isinstance(plain, dict) or set(plain) != set(arts):
        raise LoadFailed("artifact")
    names = sorted(plain)
    for n in names:
        data = plain[n]
        if not isinstance(data, (bytes, bytearray)) or len(data) != arts[n]["size"] \
                or hashlib.sha256(data).digest() != shas[n]:
            raise LoadFailed("artifact", n)
    fulls = [MODULE_PREFIX + n for n in names]
    previous = {f: sys.modules.get(f) for f in fulls}
    swap = ModuleSwap(fulls, previous)
    out: dict[str, type] = {}
    try:
        for n in names:
            try:
                code = marshal.loads(bytes(plain[n]))
            except Exception:
                raise LoadFailed("not_code", n) from None
            if not isinstance(code, types.CodeType):
                raise LoadFailed("not_code", n)
            module = types.ModuleType(MODULE_PREFIX + n)
            module.__file__ = f"<v2:{n}>"
            module.__loader__ = None
            module.__spec__ = None
            sys.modules[module.__name__] = module
            try:
                exec(code, module.__dict__)
            except (Exception, SystemExit):
                raise LoadFailed("exec", n) from None
            cls, supplier = _crawler_class(module)
            if supplier in out:
                raise LoadFailed("class", n)
            out[supplier] = cls
    except BaseException:
        swap.rollback()
        raise
    return out, swap
