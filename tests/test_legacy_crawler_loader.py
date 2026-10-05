"""Actual historical base/result + signed encrypted bundle + loader exec.

Private seed bodies remain in the server repo and are read only at test runtime.
The historical definitions come from Git, never a hand-written old result stub.
"""
import base64
import builtins
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import types

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

sys.path.insert(0, 'src')
from domae_mcp.core.crawlers import base, cart_snapshot

HISTORIC_SHA = '540324016cd28f398f90ae6f4e5c0d787a8cdcd6'
ROOT = Path(__file__).resolve().parents[1]
SEEDS = Path.home() / '.config/superpowers/worktrees/pharmsquare-server-main/order-resilience/prisma/seeds/domae-crawlers'
SUPPLIERS = {'beakje': '백제', 'tjpharm': '티제이팜', 'geoweb': '지오영'}
CART_MODULE = 'domae_mcp.core.crawlers.cart_snapshot'


def historical_repo():
    """Only Git fixture provenance may come from outside the selected archive."""
    repo = Path(os.environ.get('DOMAE_TEST_HISTORIC_WORKER_REPO', ROOT)).expanduser().resolve()
    if not repo.is_dir():
        raise RuntimeError('historical Git provenance repository does not exist')
    for args in (['rev-parse', '--git-dir'], ['cat-file', '-e', f'{HISTORIC_SHA}^{{commit}}']):
        if subprocess.run(['git', *args], cwd=repo, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode != 0:
            raise RuntimeError('historical Git provenance must contain the exact historic commit pin')
    return repo


def historical_module(monkeypatch, name):
    full_name = 'domae_mcp.core.crawlers.' + name
    code = subprocess.check_output(['git', 'show', f'{HISTORIC_SHA}:src/domae_mcp/core/crawlers/{name}.py'], cwd=historical_repo()).decode()
    module = types.ModuleType(full_name)
    monkeypatch.setitem(sys.modules, full_name, module)
    exec(compile(code, f'<git:{HISTORIC_SHA}:{name}>', 'exec'), module.__dict__)
    return module


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    def load(mode):
        if mode in ('historic_absent', 'historic_present'):
            assert subprocess.run(['git', 'cat-file', '-e', f'{HISTORIC_SHA}:src/domae_mcp/core/crawlers/cart_snapshot.py'], cwd=historical_repo(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0
            old = historical_module(monkeypatch, 'base')
            assert 'no_retry' not in old.OrderResult.__dataclass_fields__
        else:
            old = base
            monkeypatch.setitem(sys.modules, 'domae_mcp.core.crawlers.base', old)
        if mode.endswith('absent'):
            monkeypatch.setitem(sys.modules, CART_MODULE, None)
        elif mode == 'missing_guard':
            fake = types.ModuleType(CART_MODULE)
            fake.CartChanged = cart_snapshot.CartChanged
            monkeypatch.setitem(sys.modules, CART_MODULE, fake)
        else:
            monkeypatch.setitem(sys.modules, CART_MODULE, cart_snapshot)
        if mode == 'missing_guard_method':
            monkeypatch.delattr(cart_snapshot.CartGuardMixin, '_cart_finish')
        if mode == 'missing_snapshot':
            monkeypatch.delattr(cart_snapshot, 'CartSnapshot')
        if mode == 'missing_fallback':
            monkeypatch.delattr(base, 'PartialStockFallbackMixin')
        if mode == 'missing_result':
            # Actual historic result with current base and CartGuard.
            historic = historical_module(monkeypatch, 'base')
            monkeypatch.setitem(sys.modules, 'domae_mcp.core.crawlers.base', base)
            monkeypatch.setattr(base, 'OrderResult', historic.OrderResult)
        if mode == 'missing_base':
            monkeypatch.delattr(base.BaseCrawler, 'send_guard')
        loader_module = historical_module(monkeypatch, 'loader')
        loader_module.BaseCrawler = old.BaseCrawler
        loader = loader_module.CrawlerLoader(tmp_path, 'local-test-key')
        private_key = Ed25519PrivateKey.generate()
        loader_module.PUBLIC_KEY_B64 = base64.b64encode(private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
        client_key = base64.b64decode(loader_module._CRAWLER_CLIENT_KEY_B64)
        codes = {}
        for name in SUPPLIERS:
            path = Path(os.environ.get(name.upper() + '_PY', SEEDS / (name + '.py')))
            nonce = os.urandom(12)
            ciphertext = AESGCM(client_key).encrypt(nonce, path.read_bytes(), None)
            codes[name] = 'v1:' + base64.b64encode(nonce + ciphertext).decode()
        payload = json.dumps({'crawlers': codes})
        response = {'payload': payload, 'signature': base64.b64encode(private_key.sign(hashlib.sha256(payload.encode()).digest())).decode()}
        artifact = os.environ.get('DOMAE_TEST_CRAWLERS_HTTP_BUNDLE')
        if artifact:
            actual = json.loads(Path(artifact).read_text())
            loader_module.PUBLIC_KEY_B64 = actual['publicKeyB64']
            loader_module._CRAWLER_CLIENT_KEY_B64 = actual['clientKeyB64']
            response = actual['response']
            payload = response['payload']
        bundle = loader._verify_and_parse(response)
        assert bundle is not None
        assert loader._verify_and_parse(dict(response, payload=payload + ' ')) is None
        loaded = loader._import_crawlers(bundle)
        return loaded
    return load


class Resp:
    status_code = 200
    url = 'https://local-test.invalid/Order/'
    def __init__(self, text='{}'):
        self.text = text
    def json(self):
        return json.loads(self.text)


class SearchLoginSession:
    def __init__(self, supplier):
        self.supplier, self.wire = supplier, []
        self.headers, self.proxies = {}, {}
    def get(self, url, **kw):
        self.wire.append(url)
        if url.endswith('/ord/itemSearch'):
            return Resp(json.dumps([{'ITEM_CD':'A', 'ITEM_GB_CD':'01', 'ITEM_NM':'n', 'UNIT':'u', 'AVAIL_STOCK':2, 'ORD_WP2_AMT':100}]))
        return Resp()
    def post(self, url, **kw):
        self.wire.append(url)
        if url.endswith('/jwt/login'):
            return Resp('{"token":"local-test-only"}')
        if url.endswith('/login_proc.php') or url.endswith('/Member/Login'):
            return Resp()
        if url.endswith('/Order/item_api.php'):
            return Resp(json.dumps({'ResultSet':[{'ItemCode':'A','ItemName':'n','InvQty':2,'Cst':100,'ItemToken':'tA'}]}))
        if url.endswith('/Home/PartialSearchProduct'):
            # 실측 검색 행 구조(2026-10-04): 8칸, 제품코드는 div.div-product-detail 의 첫 li.
            return Resp('<tr class="tr-product-list"><td class="check"></td><td class="code">1</td>'
                        '<td class="phaCompany">m</td><td class="proName">n</td><td class="standard">u</td>'
                        '<td class="stock">2</td><td class="stock">0</td><td class="return">'
                        '<div class="div-product-detail"><ul><li>A</li><li>u</li><li>0</li><li>0</li><li>2</li></ul>'
                        '</div></td></tr>')
        if '/Home/PartialProductInfo/' in url:
            return Resp('<table><tbody><tr></tr><tr></tr><tr><td>100</td></tr></tbody></table>')
        raise AssertionError('Unexpected mutation/order request')
    def delete(self, *a, **kw):
        raise AssertionError('Unexpected cart deletion')


@pytest.mark.parametrize('mode', ['historic_absent', 'historic_present', 'missing_guard', 'missing_result', 'missing_base', 'missing_guard_method', 'missing_snapshot', 'missing_fallback'])
def test_legacy_loader_login_search_and_all_order_entries_blocked(runtime, mode):
    loaded = runtime(mode)
    assert set(loaded) == set(SUPPLIERS.values())
    for supplier, cls in loaded.items():
        crawler = cls()
        site = SearchLoginSession(supplier)
        crawler.session = site
        assert crawler.login('local-user', 'local-test-password')
        rows = crawler.search('n')
        assert len(rows) == 1 and rows[0].quantity == 2
        assert cls.URGENT_ORDER_SAFE is False
        before = list(site.wire)
        items = [{'product_id': rows[0].product_id, 'quantity': 2, 'product_name':'n'}]
        calls = [('order', (rows[0].product_id, 2)), ('_order_bare', (rows[0].product_id, 2)), ('order_batch', (items,))]
        for entry in ('_send_cart', '_run_with_cart'):
            if hasattr(crawler, entry):
                calls.append((entry, (items,)))
        if hasattr(crawler, '_stage'):
            calls.append(('_stage', (rows[0].product_id, 2, '')))
        calls.append(('_order_with_stock_fallback', (lambda *a: site.post('forbidden'), 'A', 2)))
        if hasattr(crawler, '_order_one'):
            calls.append(('_order_one', (rows[0].product_id, 2, items[0])))
        for name, args in calls:
            # Historic callers can ignore no_retry: every repeated entry must block.
            for _ in range(3):
                result = getattr(crawler, name)(*args)
                for r in result if isinstance(result, list) else [result]:
                    assert not r.success and r.reason_code == 'not_sent' and r.no_retry
                    assert '업데이트' in r.message
        assert site.wire == before


def test_latest_loader_keeps_cartguard_and_capability(runtime):
    loaded = runtime('latest')
    assert len(loaded) == 3
    for cls in loaded.values():
        assert cls.URGENT_ORDER_SAFE is True
        assert issubclass(cls, cart_snapshot.CartGuardMixin)


@pytest.mark.parametrize('error', [ImportError('internal defect'), ModuleNotFoundError('transitive missing', name='unrelated_dependency')])
def test_cart_module_internal_import_errors_are_not_hidden(monkeypatch, runtime, error):
    original = builtins.__import__
    def broken(name, *a, **kw):
        if name == CART_MODULE:
            raise error
        return original(name, *a, **kw)
    monkeypatch.setattr(builtins, '__import__', broken)
    assert runtime('latest') == {}


def test_archive_root_uses_explicit_historic_git_provenance(monkeypatch, tmp_path, runtime):
    selected_runtime_base = base
    assert Path(base.__file__).resolve().is_relative_to(ROOT / 'src')
    historic_repo = Path(os.environ.get('DOMAE_TEST_HISTORIC_WORKER_REPO', ROOT))
    archive_root = tmp_path / 'extracted-worker'
    archive_root.mkdir()
    assert not (archive_root / '.git').exists()
    monkeypatch.setitem(globals(), 'ROOT', archive_root)
    monkeypatch.setenv('DOMAE_TEST_HISTORIC_WORKER_REPO', str(historic_repo))
    # Same real old base/result + loader exec + repeated order wire-zero test.
    test_legacy_loader_login_search_and_all_order_entries_blocked(runtime, 'historic_absent')
    test_latest_loader_keeps_cartguard_and_capability(runtime)
    # Only historical Git fixtures use the override; selected runtime stays bound.
    assert base is selected_runtime_base


@pytest.mark.parametrize('invalid', ['not_git', 'missing_pin'])
def test_historic_git_provenance_must_contain_exact_pin(monkeypatch, tmp_path, invalid):
    if invalid == 'not_git':
        monkeypatch.setenv('DOMAE_TEST_HISTORIC_WORKER_REPO', str(tmp_path))
    else:
        monkeypatch.setenv('DOMAE_TEST_HISTORIC_WORKER_REPO', str(historical_repo()))
        monkeypatch.setitem(globals(), 'HISTORIC_SHA', '0' * 40)
    with pytest.raises(RuntimeError, match='historical Git provenance'):
        historical_module(monkeypatch, 'base')
