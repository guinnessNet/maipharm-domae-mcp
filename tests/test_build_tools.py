"""빌드 도구 시험 (계획 Task 11): gen_build_info·check_crawler_imports·check_build_report·selftest. 합성 입력만 쓴다."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


cci = _load("check_crawler_imports")
cbr = _load("check_build_report")
gbi = _load("gen_build_info")


# ── check_crawler_imports (P18) ──

@pytest.mark.parametrize("src", [
    "from urllib.parse import quote",
    "import urllib.parse",
    "from bs4 import BeautifulSoup as bs",
    "from domae_mcp.core.crawlers.base import __dict__ as _base_abi",
    "from domae_mcp.core.crawlers.cart_snapshot import CartSnapshot",
    "import re, json, logging, time, math, base64, os, inspect, typing, datetime",
    "import requests\nimport urllib3\nfrom lxml import html",
])
def test_allowed_imports(src):
    assert cci.violations(cci.imported_modules(src)) == set()


@pytest.mark.parametrize("src,bad", [
    ("import psycopg2", "psycopg2"),
    ("from domae_mcp.cloud import x", "domae_mcp.cloud"),
    ("from domae_mcp.core.crawlers.loader import CrawlerLoader", "domae_mcp.core.crawlers.loader"),
    ("from . import sibling", "."),
    ("import subprocess", "subprocess"),
])
def test_rejected_imports(src, bad):
    assert cci.violations(cci.imported_modules(src)) == {bad}


def test_check_crawler_imports_cli(tmp_path, capsys):
    (tmp_path / "a.py").write_text("import requests\nfrom domae_mcp.core.crawlers.base import BaseCrawler\n",
                                   encoding="utf-8")
    assert cci.main([str(tmp_path)]) == 0
    (tmp_path / "b.py").write_text("import psycopg2\n", encoding="utf-8")
    assert cci.main([str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "psycopg2" in out and "import" not in out.split("files=")[0].replace("psycopg2", "")
    assert cci.main([str(tmp_path / "none")]) == 2


def test_lists_match_crawler_deps():
    from domae_mcp.v2 import crawler_deps
    assert tuple(cci.EXTERNAL_TOP) == tuple(crawler_deps.EXTERNAL_TOP)
    assert tuple(cci.INTERNAL_ABI) == tuple(crawler_deps.INTERNAL_ABI)


# ── check_build_report (P18·P19) ──

GOOD = ["domae_mcp", "domae_mcp.v2.crawler_deps", "domae_mcp.v2.gate", "domae_mcp._build_info",
        "domae_mcp.core.crawlers", "domae_mcp.core.crawlers.base", "domae_mcp.core.crawlers.registry",
        "domae_mcp.core.crawlers.cart_snapshot", "requests", "urllib3.util", "bs4", "lxml.etree",
        "re", "json", "logging", "time", "math", "base64", "os", "inspect", "typing", "datetime", "urllib.parse"]


def _nuitka_xml(path, mods):
    body = "".join(f'<module name="{m}" kind="x"/>' for m in mods)
    path.write_text(f"<nuitka-compilation-report>{body}</nuitka-compilation-report>", encoding="utf-8")


def test_build_report_ok(tmp_path):
    f = tmp_path / "r.xml"
    _nuitka_xml(f, GOOD)
    assert cbr.main([str(f), "--kind", "nuitka"]) == 0


@pytest.mark.parametrize("extra", ["domae_mcp.core.crawlers.loader", "domae_mcp.core.crawlers._client_key",
                                   "domae_mcp.core.crawlers.familypharm", "domae_mcp.cloud.scheduler", "pytest",
                                   "fakeredis", "tests.test_x", "domae_mcp.core.crawlers.geoweb"])
def test_build_report_forbidden(tmp_path, extra, capsys):
    f = tmp_path / "r.xml"
    _nuitka_xml(f, GOOD + [extra])
    assert cbr.main([str(f), "--kind", "nuitka"]) == 1
    assert f"forbidden:{extra}" in capsys.readouterr().out


@pytest.mark.parametrize("missing", ["domae_mcp.v2.crawler_deps", "bs4", "domae_mcp._build_info"])
def test_build_report_missing(tmp_path, missing):
    f = tmp_path / "r.xml"
    _nuitka_xml(f, [m for m in GOOD if m != missing])
    assert cbr.main([str(f), "--kind", "nuitka"]) == 1


def test_build_report_pyinstaller_toc(tmp_path):
    toc = tmp_path / "PYZ-00.toc"
    rows = ",\n".join(f"('{m}', 'C:\\\\x\\\\{m}.py', 'PYMODULE')" for m in GOOD)
    toc.write_text(f"('PYZ-00.pyz',\n [{rows}])", encoding="utf-8")
    assert cbr.main([str(tmp_path), "--kind", "pyinstaller"]) == 0
    toc.write_text(toc.read_text(encoding="utf-8").replace("'requests'", "'fakeredis'"), encoding="utf-8")
    assert cbr.main([str(toc), "--kind", "pyinstaller"]) == 1


def test_build_report_input_errors(tmp_path):
    assert cbr.main([str(tmp_path / "none.xml"), "--kind", "nuitka"]) == 2
    bad = tmp_path / "bad.xml"
    bad.write_text("<not-closed", encoding="utf-8")
    assert cbr.main([str(bad), "--kind", "nuitka"]) == 2


# ── gen_build_info (P3) ──

def test_gen_build_info_render_and_remove(tmp_path):
    out = tmp_path / "_build_info.py"
    assert gbi.main(["--kind", "test", "--out", str(out)]) == 0
    ns = {}
    exec(out.read_text(encoding="utf-8"), ns)
    assert ns["KIND"] == "test" and ns["VERSION"] == "1.4.0" and ns["GIT_SHA"]
    assert gbi.main(["--remove", "--out", str(out)]) == 0 and not out.exists()
    with pytest.raises(ValueError):
        gbi.render("weird", "1.4.0", "abc")
    with pytest.raises(ValueError):
        gbi.render("test", '1.4.0"\nimport os', "abc")


def test_build_info_gitignored():
    text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "src/domae_mcp/_build_info.py" in text.splitlines()


# ── selftest ──

def test_selftest_writes_report(tmp_path, monkeypatch):
    from domae_mcp.v2 import selftest
    monkeypatch.delitem(sys.modules, "domae_mcp._build_info", raising=False)
    out = tmp_path / "st.json"
    assert selftest.run(str(out)) == 0
    r = json.loads(out.read_text(encoding="utf-8"))
    assert r["kind"] == "source" and r["version"] == "1.4.0" and r["deps_ok"] is True
    assert "ed25519-0477f26ca36a1ebd" in r["trust_ids"]
    assert set(r) == {"kind", "version", "runtime_id", "trust_ids", "deps_ok"}


# ── v2_fake_server: worker 검증기로 끝까지 통과하는가 ──

def test_fake_server_roundtrip_with_worker_loader(tmp_path):
    import hashlib
    import threading
    from http.server import ThreadingHTTPServer

    from domae_mcp.v2.client import V2Client
    from domae_mcp.v2.loader import V2Loader, V2Revoked
    from domae_mcp.v2.runtime import local_runtime_id
    from domae_mcp.v2.store import StateStore
    from domae_mcp.v2.trust import _read_trust_file
    from domae_mcp.v2.winprotect import DevPlainProtector
    from v2_fakes import DEVICE_HEX, TEST_BEARER, fake_artifacts

    fs = _load("v2_fake_server")
    art = tmp_path / "artifacts"
    rid = local_runtime_id()
    key = hashlib.sha256(b"rt").hexdigest()
    (art / key[:16]).mkdir(parents=True)
    plain = fake_artifacts()
    arts = {}
    for n, b in plain.items():
        (art / key[:16] / f"{n}.marshal").write_bytes(b)
        arts[n] = {"file": f"{key[:16]}/{n}.marshal", "sha256": hashlib.sha256(b).hexdigest(), "size": len(b)}
    build = {"base_abi": 1, "cart_abi": 1, "min_client_version": "1.4.0", "bundle_hash": "ef" * 32,
             "modules": sorted(plain), "runtimes": [{"artifacts": arts, "runtime_id": rid, "runtime_key": key}]}
    (art / "build.json").write_text(json.dumps(build), encoding="ascii")
    st = fs.FakeState(art)
    trust_file = tmp_path / "trust.json"
    trust_file.write_text(json.dumps({"signing_key_id": st.key_id, "public_key": st.pub_b64}), encoding="utf-8")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), fs.make_handler(st, None))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}/api/domae"
        prot = DevPlainProtector()
        state = StateStore(tmp_path / "v2", prot)

        def loader():
            return V2Loader(TEST_BEARER, tmp_path / "v2" / "cache", state, prot,
                            client=V2Client(TEST_BEARER, DEVICE_HEX, base=base),
                            trust=_read_trust_file(str(trust_file)), device_id_hex=DEVICE_HEX, runtime_id=rid)

        ld = loader()
        v = ld.check_version()
        p = ld.prepare_download()
        assert p.plain == plain and p.release_seq == v.release_seq == 1
        st.mode = "revoke"
        with pytest.raises(V2Revoked):
            loader().check_version()
        assert [r[0] for r in st.requests] == ["GET", "POST", "GET"]
    finally:
        srv.shutdown()
