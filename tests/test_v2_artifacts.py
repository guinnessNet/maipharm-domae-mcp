"""도매 v2 runtime_id·빌드 종류·manifest 검사·marshal 로드 시험 (계획 Task 4). 합성 크롤러만 쓴다."""
import builtins
import copy
import importlib.util
import json
import marshal
import sys
import types

import pytest

from domae_mcp.core.crawlers.base import BaseCrawler
from domae_mcp.v2 import runtime
from domae_mcp.v2.artifacts import (MAX_ARTIFACT_BYTES, MODULE_PREFIX, LoadFailed, check_manifest, load_modules,
                                    manifest_sha256, unload_all)
from domae_mcp.v2.encoding import EncodingError, encode_runtime_id
from v2_fakes import compile_artifact, fake_artifacts, make_manifest

BUILD_RUNTIME = {"implementation": "cpython", "python_version": "3.12.10", "magic": "cb0d0d0a",
                 "artifact_kind": "marshal-code", "loader_abi": 1, "platform": "win_amd64"}


@pytest.fixture(autouse=True)
def _clean_modules():
    unload_all()
    yield
    unload_all()


@pytest.fixture
def plain():
    return fake_artifacts()


@pytest.fixture
def rid():
    return runtime.local_runtime_id()


def _reason(fn, *a, **k):
    with pytest.raises(LoadFailed) as e:
        fn(*a, **k)
    return e.value.reason


def _loaded():
    return sorted(m for m in sys.modules if m.startswith(MODULE_PREFIX))


def test_runtime_id_shape(monkeypatch):
    monkeypatch.delenv("DOMAE_V2_RUNTIME_ID_FILE", raising=False)
    rid = runtime.local_runtime_id()
    assert set(rid) == set(BUILD_RUNTIME)
    encode_runtime_id(rid)
    assert rid["magic"] == importlib.util.MAGIC_NUMBER.hex()
    if sys.platform == "win32" and rid["python_version"] == "3.12.10":
        assert rid == BUILD_RUNTIME


def test_runtime_id_override_file(tmp_path, monkeypatch):
    f = tmp_path / "rid.json"
    f.write_text(json.dumps(BUILD_RUNTIME), encoding="utf-8")
    monkeypatch.setenv("DOMAE_V2_RUNTIME_ID_FILE", str(f))
    assert runtime.local_runtime_id() == BUILD_RUNTIME
    f.write_text(json.dumps({**BUILD_RUNTIME, "extra": 1}), encoding="utf-8")
    with pytest.raises(EncodingError):
        runtime.local_runtime_id()


def test_build_kind_and_overrides(monkeypatch):
    monkeypatch.delitem(sys.modules, "domae_mcp._build_info", raising=False)
    monkeypatch.setenv("DOMAE_V2_SERVER_BASE", "http://127.0.0.1:1/api/domae")
    assert runtime.build_kind() == "source"
    assert runtime.dev_override("DOMAE_V2_SERVER_BASE") == "http://127.0.0.1:1/api/domae"
    for kind, want in (("release", "release"), ("test", "test"), ("weird", "release")):
        fake = types.ModuleType("domae_mcp._build_info")
        fake.KIND = kind
        monkeypatch.setitem(sys.modules, "domae_mcp._build_info", fake)
        assert runtime.build_kind() == want
        got = runtime.dev_override("DOMAE_V2_SERVER_BASE")
        assert got == (None if want == "release" else "http://127.0.0.1:1/api/domae")


def _mutate(manifest, fn):
    m = copy.deepcopy(manifest)
    fn(m)
    return m


def test_manifest_ok(plain, rid):
    check_manifest(make_manifest(plain), set(plain), rid, "1.4.0")


def test_manifest_set_mismatch(plain, rid):
    m = make_manifest(plain)
    assert _reason(check_manifest, m, set(plain) - {"fake_beta"}, rid, "1.4.0") == "manifest"
    m2 = _mutate(m, lambda x: x["required_modules"].pop())
    assert _reason(check_manifest, m2, set(plain), rid, "1.4.0") == "manifest"
    m3 = _mutate(m, lambda x: x["required_modules"].append("fake_alpha"))
    assert _reason(check_manifest, m3, set(plain), rid, "1.4.0") == "manifest"


def test_manifest_empty_modules(rid):
    assert _reason(check_manifest, make_manifest({}), set(), rid, "1.4.0") == "manifest"


def test_manifest_magic_mismatch(plain, rid):
    m = _mutate(make_manifest(plain), lambda x: x["artifacts"]["fake_alpha"].update(magic="00000000"))
    assert _reason(check_manifest, m, set(plain), rid, "1.4.0") == "runtime"


def test_manifest_python_version_mismatch(plain, rid):
    m = _mutate(make_manifest(plain), lambda x: x["artifacts"]["fake_beta"].update(python_version="3.12.9"))
    assert _reason(check_manifest, m, set(plain), rid, "1.4.0") == "runtime"


def test_manifest_extra_artifact_key(plain, rid):
    m = _mutate(make_manifest(plain), lambda x: x["artifacts"]["fake_beta"].update(extra=1))
    assert _reason(check_manifest, m, set(plain), rid, "1.4.0") == "manifest"


@pytest.mark.parametrize("field,value", [("base_abi", 2), ("cart_abi", 0), ("base_abi", True)])
def test_manifest_abi_mismatch(plain, rid, field, value):
    m = _mutate(make_manifest(plain), lambda x: x.update({field: value}))
    assert _reason(check_manifest, m, set(plain), rid, "1.4.0") == "abi"


def test_client_too_old(plain, rid):
    m = make_manifest(plain, min_client_version="1.4.1")
    assert _reason(check_manifest, m, set(plain), rid, "1.4.0") == "client_too_old"
    check_manifest(make_manifest(plain, min_client_version="1.3.9"), set(plain), rid, "1.4.0")
    check_manifest(make_manifest(plain, min_client_version="1.4.0"), set(plain), rid, "1.10.0")


@pytest.mark.parametrize("v", ["1.4", "1.4.0.1", "v1.4.0", "1.4.0 ", "", None, 140])
def test_min_client_version_bad_format(plain, rid, v):
    m = _mutate(make_manifest(plain), lambda x: x.update(min_client_version=v))
    assert _reason(check_manifest, m, set(plain), rid, "1.4.0") == "manifest"


def test_manifest_bad_sha_b64(plain, rid):
    m = _mutate(make_manifest(plain), lambda x: x["artifacts"]["fake_alpha"].update(sha256="AAAA"))
    assert _reason(check_manifest, m, set(plain), rid, "1.4.0") == "manifest"
    assert _reason(manifest_sha256, m) == "manifest"


@pytest.mark.parametrize("size", [-1, 1.0, True, "10"])
def test_manifest_bad_size(plain, rid, size):
    m = _mutate(make_manifest(plain), lambda x: x["artifacts"]["fake_alpha"].update(size=size))
    assert _reason(check_manifest, m, set(plain), rid, "1.4.0") == "manifest"


def test_manifest_size_over_limit(plain, rid):
    m = _mutate(make_manifest(plain), lambda x: x["artifacts"]["fake_alpha"].update(size=MAX_ARTIFACT_BYTES + 1))
    assert _reason(check_manifest, m, set(plain), rid, "1.4.0") == "too_large"


def test_load_success_and_base_identity(plain):
    classes, swap = load_modules(plain, make_manifest(plain))
    swap.finalize()
    assert sorted(classes) == ["가짜A", "가짜B", "가짜C"]
    owners = {"가짜A": "fake_alpha", "가짜B": "fake_beta", "가짜C": "fake_gamma"}
    for name, cls in classes.items():
        assert issubclass(cls, BaseCrawler)
        assert cls.__module__ == MODULE_PREFIX + owners[name]
        assert sys.modules[cls.__module__].__file__.startswith("<v2:")
    assert classes["가짜A"]().search("x")[0].product_name == "x"
    assert _loaded() == [MODULE_PREFIX + n for n in sorted(plain)]


def test_load_all_or_nothing(plain):
    m = make_manifest(plain)
    bad = dict(plain)
    bad["fake_beta"] = compile_artifact("x = 1", "fake_beta")
    assert _reason(load_modules, bad, m) == "artifact"
    assert _loaded() == []


def test_failed_update_restores_previous_generation(plain):
    _, swap = load_modules(plain, make_manifest(plain))
    swap.finalize()
    first = {n: sys.modules[MODULE_PREFIX + n] for n in plain}
    gen2 = dict(plain)
    gen2["fake_gamma"] = compile_artifact("raise RuntimeError('boom')", "fake_gamma")
    assert _reason(load_modules, gen2, make_manifest(gen2)) == "exec"
    for n, mod in first.items():
        assert sys.modules[MODULE_PREFIX + n] is mod


def test_rollback_after_success_restores_previous(plain):
    _, swap1 = load_modules(plain, make_manifest(plain))
    swap1.finalize()
    first = sys.modules[MODULE_PREFIX + "fake_alpha"]
    _, swap2 = load_modules(plain, make_manifest(plain))
    assert sys.modules[MODULE_PREFIX + "fake_alpha"] is not first
    swap2.rollback()
    assert sys.modules[MODULE_PREFIX + "fake_alpha"] is first


def test_finalize_drops_old_names(plain):
    _, swap = load_modules(plain, make_manifest(plain))
    swap.finalize()
    two = {k: plain[k] for k in ("fake_alpha", "fake_beta")}
    _, swap2 = load_modules(two, make_manifest(two))
    swap2.finalize()
    assert _loaded() == [MODULE_PREFIX + "fake_alpha", MODULE_PREFIX + "fake_beta"]


def test_size_mismatch(plain):
    m = _mutate(make_manifest(plain), lambda x: x["artifacts"]["fake_alpha"].update(size=1))
    assert _reason(load_modules, plain, m) == "artifact"
    assert _loaded() == []


def test_not_code_object(plain):
    bad = dict(plain)
    bad["fake_alpha"] = marshal.dumps(123)
    assert _reason(load_modules, bad, make_manifest(bad)) == "not_code"
    assert _loaded() == []


def test_marshal_garbage(plain):
    bad = dict(plain)
    bad["fake_alpha"] = b"\x00\x01"
    assert _reason(load_modules, bad, make_manifest(bad)) == "not_code"
    assert _loaded() == []


def test_exec_error_rolls_back(plain):
    bad = dict(plain)
    bad["fake_gamma"] = compile_artifact("import no_such_module_xyz", "fake_gamma")
    assert _reason(load_modules, bad, make_manifest(bad)) == "exec"
    assert _loaded() == []


ZERO = "x = 1\n"
TWO = '''
from domae_mcp.core.crawlers.base import BaseCrawler
class A(BaseCrawler):
    SUPPLIER_NAME = "a"
    def login(self, i, p): return True
    def search(self, k): return []
class B(A):
    SUPPLIER_NAME = "b"
'''
IMPORTED_ONLY = "from domae_mcp.core.crawlers.base import BaseCrawler\n"
DUP = '''
from domae_mcp.core.crawlers.base import BaseCrawler
class D(BaseCrawler):
    SUPPLIER_NAME = "가짜A"
    def login(self, i, p): return True
    def search(self, k): return []
'''


SYSEXIT = "raise SystemExit(3)\n"


def test_system_exit_is_exec_failure(plain):
    bad = dict(plain)
    bad["fake_gamma"] = compile_artifact(SYSEXIT, "fake_gamma")
    assert _reason(load_modules, bad, make_manifest(bad)) == "exec"
    assert _loaded() == []


@pytest.mark.parametrize("bad_name", ["a.b", "a b", "", "가나"])
def test_manifest_bad_module_name(rid, bad_name):
    p = {bad_name: compile_artifact(ZERO, "x")}
    assert _reason(check_manifest, make_manifest(p), set(p), rid, "1.4.0") == "manifest"


def test_release_override_warns_once(monkeypatch, caplog):
    fake = types.ModuleType("domae_mcp._build_info")
    fake.KIND = "release"
    monkeypatch.setitem(sys.modules, "domae_mcp._build_info", fake)
    monkeypatch.setenv("DOMAE_V2_TRUST_FILE", "x.json")
    monkeypatch.setattr(runtime, "_warned", set())
    with caplog.at_level("WARNING", logger="domae_mcp.v2.runtime"):
        assert runtime.dev_override("DOMAE_V2_TRUST_FILE") is None
        assert runtime.dev_override("DOMAE_V2_TRUST_FILE") is None
    assert len([r for r in caplog.records if "DOMAE_V2_TRUST_FILE" in r.getMessage()]) == 1


def test_build_info_import_error_means_release(monkeypatch):
    real = runtime.importlib.import_module

    def broken(name, *a, **k):
        if name == "domae_mcp._build_info":
            raise ModuleNotFoundError("no module named x", name="x")
        return real(name, *a, **k)

    monkeypatch.setattr(runtime.importlib, "import_module", broken)
    assert runtime.build_kind() == "release"


@pytest.mark.parametrize("src", [ZERO, TWO, IMPORTED_ONLY, DUP])
def test_zero_or_two_classes(plain, src):
    bad = dict(plain)
    bad["fake_gamma"] = compile_artifact(src, "fake_gamma")
    assert _reason(load_modules, bad, make_manifest(bad)) == "class"
    assert _loaded() == []


def test_no_compile_called(plain, monkeypatch):
    m = make_manifest(plain)

    def no_compile(*a, **k):
        raise AssertionError("compile called")

    monkeypatch.setattr(builtins, "compile", no_compile)
    classes, swap = load_modules(plain, m)
    assert len(classes) == 3
