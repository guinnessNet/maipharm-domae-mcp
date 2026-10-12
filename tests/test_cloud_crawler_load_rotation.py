"""클라우드 워커 크롤러 로드 회귀: 키 교체 기간에 이전 키 행과 현재 키 행을 모두 로드한다(A3, B-2 Task 3).

합성 크롤러만 쓴다(공개 레포에 실제 크롤러 원문·실측값을 넣지 않는다).
"""
import base64
import hashlib
import os

from domae_mcp.cloud import crypto
from domae_mcp.cloud.scheduler import CloudScheduler

SRC = '''from domae_mcp.core.crawlers.base import BaseCrawler


class {cls}(BaseCrawler):
    SUPPLIER_NAME = "{supplier}"
'''


class _Cur:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, params=None):
        assert "FROM domae_crawlers" in sql

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return _Cur(self.rows)


def _key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


def _row(name, cls, supplier):
    src = SRC.format(cls=cls, supplier=supplier)
    return name, crypto.encrypt_crawler_code(src), hashlib.sha256(src.encode("utf-8")).hexdigest()


def test_loads_rows_from_both_keys_during_rotation(monkeypatch):
    old, new = _key(), _key()
    monkeypatch.delenv("DOMAE_CRAWLER_KEY_PREVIOUS", raising=False)
    monkeypatch.setenv("DOMAE_CRAWLER_KEY", old)
    row_old = _row("synth_old", "SynthOldCrawler", "합성이전")
    monkeypatch.setenv("DOMAE_CRAWLER_KEY", new)
    row_new = _row("synth_new", "SynthNewCrawler", "합성현재")
    monkeypatch.setenv("DOMAE_CRAWLER_KEY_PREVIOUS", old)
    s = CloudScheduler(None, None)
    s._load_crawlers(_Conn([row_old, row_new]))
    assert set(s._crawlers) == {"합성이전", "합성현재"}


def test_row_from_unknown_key_is_skipped_not_fatal(monkeypatch):
    monkeypatch.delenv("DOMAE_CRAWLER_KEY_PREVIOUS", raising=False)
    monkeypatch.setenv("DOMAE_CRAWLER_KEY", _key())
    stray = _row("synth_stray", "SynthStrayCrawler", "합성기타")
    monkeypatch.setenv("DOMAE_CRAWLER_KEY", _key())
    good = _row("synth_good", "SynthGoodCrawler", "합성정상")
    s = CloudScheduler(None, None)
    s._load_crawlers(_Conn([stray, good]))
    assert set(s._crawlers) == {"합성정상"}
