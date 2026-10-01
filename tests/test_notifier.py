# tests/test_notifier.py
import sys
sys.path.insert(0, "src")

import domae_mcp.cloud.notifier as nmod
from domae_mcp.cloud.notifier import Notifier


class Resp:
    def __init__(self, code, body):
        self.status_code, self._body = code, body
        self.text = str(body)

    def json(self):
        return self._body


def _setup(monkeypatch, resp):
    monkeypatch.setenv("DOMAE_TELEGRAM_BOT_TOKEN", "1:x")
    monkeypatch.setattr(nmod.requests, "post", lambda *a, **k: resp)
    dead, ok, bot = [], [], []
    Notifier.set_delivery_sinks(lambda c, r: dead.append((c, r)), lambda c: ok.append(c),
                                lambda r: bot.append(r))
    return dead, ok, bot


def test_dead_chat_is_reported(monkeypatch):
    dead, ok, bot = _setup(monkeypatch, Resp(403, {"ok": False, "description":
                           "Forbidden: bot can't initiate conversation with a user"}))
    assert Notifier.send_telegram("123459363", "hi") is None
    assert dead and dead[0][0] == "123459363" and ok == [] and bot == []


def test_token_rejection_marks_bot(monkeypatch):
    dead, ok, bot = _setup(monkeypatch, Resp(401, {"ok": False, "description": "Unauthorized"}))
    Notifier.send_telegram("123459363", "hi")
    assert dead == [] and ok == [] and bot and "Unauthorized" in bot[0]


def test_success_clears(monkeypatch):
    dead, ok, bot = _setup(monkeypatch, Resp(200, {"ok": True, "result": {"message_id": 7}}))
    assert Notifier.send_telegram("123459363", "hi") == 7
    assert ok == ["123459363"] and dead == []


def test_sink_error_does_not_break_send(monkeypatch):
    monkeypatch.setenv("DOMAE_TELEGRAM_BOT_TOKEN", "1:x")
    monkeypatch.setattr(nmod.requests, "post",
                        lambda *a, **k: Resp(200, {"ok": True, "result": {"message_id": 1}}))
    def boom(c):
        raise RuntimeError("redis down")
    Notifier.set_delivery_sinks(None, boom, None)
    assert Notifier.send_telegram("1", "hi") == 1


def test_200_api_failure_does_not_clear_broken(monkeypatch):
    dead,ok,bot=_setup(monkeypatch,Resp(200,{'ok':False,'description':'chat not found'}))
    assert Notifier.send_telegram('chat','hi') is None
    assert not ok and dead


def test_transport_exception_does_not_log_bot_token(monkeypatch,caplog):
    secret='synthetic-secret'
    monkeypatch.setenv('DOMAE_TELEGRAM_BOT_TOKEN','1:'+secret)
    def failed_send(*a,**kw):
        raise nmod.requests.ConnectionError('HTTPSConnectionPool failed for /bot1:'+secret+'/sendMessage')
    monkeypatch.setattr(nmod.requests,'post',failed_send)
    assert Notifier.send_telegram('chat','hi') is None
    assert secret not in caplog.text
    assert 'ConnectionError' in caplog.text
