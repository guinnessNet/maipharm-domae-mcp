"""v1 로더를 지운 뒤에도 클라우드 워커 import 가 깨지지 않는지 (계획 Task 10, Review Focus 6)."""
import importlib
import subprocess
import sys

import pytest


def _fresh_import(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)


def test_core_imports_without_loader():
    code = (
        "import sys\n"
        "import domae_mcp.core.crawlers.base, domae_mcp.core.crawlers.cart_snapshot\n"
        "import domae_mcp.core.crawlers\n"
        "assert 'domae_mcp.core.crawlers.loader' not in sys.modules\n"
        "print('ok')\n"
    )
    r = _fresh_import(code)
    assert r.returncode == 0, r.stderr[-2000:]
    assert r.stdout.strip() == "ok"


def test_cloud_imports_without_loader():
    pytest.importorskip("psycopg2")
    code = (
        "import sys\n"
        "import domae_mcp.core.crawlers.base, domae_mcp.core.crawlers.cart_snapshot\n"
        "import domae_mcp.core.crawlers\n"
        "import domae_mcp.cloud.scheduler, domae_mcp.cloud.worker\n"
        "assert 'domae_mcp.core.crawlers.loader' not in sys.modules\n"
        "assert 'domae_mcp.core.crawlers._client_key' not in sys.modules\n"
        "print('ok')\n"
    )
    r = _fresh_import(code)
    assert r.returncode == 0, r.stderr[-2000:]
    assert r.stdout.strip() == "ok"


def test_crawlers_package_exports():
    pkg = importlib.import_module("domae_mcp.core.crawlers")
    assert set(pkg.__all__) == {"BaseCrawler", "SearchResult", "OrderResult", "CrawlerRegistry"}
    with pytest.raises(ImportError):
        importlib.import_module("domae_mcp.core.crawlers.loader")
