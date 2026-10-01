import sys
sys.path.insert(0, "src")

import json
import pytest
from domae_mcp.cloud import scheduler as sch
from domae_mcp.core.crawlers.base import OrderResult


class Conn:
    def __init__(self, fallback=False, fail_record=False):
        self.sql = []
        self.fallback = fallback
        self.fail_record = fail_record
        self.rowcount = 1
        self.commits = 0
        self.rollbacks = 0
        self.last = ''

    def cursor(self): return self
    def execute(self, sql, params=None):
        self.last = ' '.join(sql.split())
        self.sql.append((self.last, params))
        if self.fail_record and 'SET success = %s' in self.last:
            raise RuntimeError('record failed')
    def fetchone(self):
        if 'RETURNING id' in self.last: return ('b',)
        if 'SELECT count(*)' in self.last: return (0,)
        if 'SELECT m.credentials' in self.last: return ({'인천': {'login_id':'x'}, '복산': {'login_id':'x'}}, 'chat', ['인천','복산'], self.fallback)
        return None
    def commit(self): self.commits += 1
    def rollback(self): self.rollbacks += 1


class Pool:
    def __init__(self, primary, secondary): self.conns=[primary,secondary];self.returned=[]
    def getconn(self): return self.conns.pop(0)
    def putconn(self,c): self.returned.append(c)


class Redis:
    def __init__(self): self.events=[]
    def publish(self,k,v): self.events.append(json.loads(v))


def run(monkeypatch, results, fallback=False, fail_record=False):
    primary, secondary = Conn(fallback, fail_record), Conn()
    pool, redis = Pool(primary, secondary), Redis()
    scheduler = sch.CloudScheduler(pool,redis)
    scheduler._get_conn = pool.getconn
    scheduler._decrypt_creds = lambda raw: raw
    scheduler._crawlers_loaded = True
    class Crawler:
        def login(self,*a): return True
        def search(self,*a): return []
        def order_batch(self,items):
            assert items[0]['insurance_code'] == '694003321'
            if isinstance(results,Exception): raise results
            return results
    scheduler._crawlers={'인천':Crawler,'복산':Crawler}
    messages=[]
    scheduler._send_auto_order_telegram=lambda *a,**kw: messages.append((a,kw))
    item={'quantity':15,'price':1000,'cart_item_id':'c','db_order_id':'o','insurance_code':'694003321','unit':'12EA','product_name':'베아놀','product_id':'p'}
    scheduler.auto_order({'monitor_id':'m','batch_id':'b','supplier':'인천','items':[item]})
    return primary,secondary,redis,messages


def test_partial_quantity_is_preserved_and_reported(monkeypatch):
    primary, _, redis, messages=run(monkeypatch,[OrderResult(success=True,reason_code='stock_adjusted',adjusted_quantity=10)])
    assert any('SET quantity = %s' in sql and params[0]==5 for sql,params in primary.sql)
    assert not any(sql.startswith('DELETE') for sql,_ in primary.sql)
    assert messages[0][0][2][0]['quantity']==10
    assert redis.events[0]['shortfall']==5 and redis.events[0]['totalPrice']==10000


@pytest.mark.parametrize('results',[TimeoutError('lost'),[]])
def test_missing_and_exception_results_are_unconfirmed(monkeypatch,results):
    primary,_,redis,messages=run(monkeypatch,results)
    records=[p for sql,p in primary.sql if 'SET success = %s' in sql]
    assert records[0][0] is None and records[0][-2]=='send_unknown'
    assert messages[0][1]['unconfirmed_items']
    assert not messages[0][0][3]
    assert redis.events[0]['unconfirmed']==1


def test_sql_failure_preserves_pre_send_marker(monkeypatch):
    primary,*_=run(monkeypatch,[OrderResult(success=True)],fail_record=True)
    assert any('send_unknown' in str(params) and 'success IS NULL' in sql for sql,params in primary.sql if 'SET success = %s' not in sql)
    assert all('send_unknown' in sql for sql,_ in primary.sql if 'SET success = false' in sql)


def test_fallback_uses_dedicated_connection_and_does_not_poison_origin(monkeypatch):
    seen=[]
    def fallback(needs,candidates,opened,lock,renew,unlock,pending,result,unknown):
        seen.append(pending.__self__.conn)
        raise RuntimeError('fallback db failed')
    monkeypatch.setattr(sch,'run_fallback',fallback)
    primary,secondary,redis,_=run(monkeypatch,[OrderResult(success=False,reason_code='stock_zero')],fallback=True)
    assert seen==[secondary] and primary.rollbacks==0
    assert redis.events


def test_telegram_unknown_and_fallback_items_have_no_ao(monkeypatch):
    from domae_mcp.cloud.notifier import Notifier
    from domae_mcp.cloud.fallback import FallbackOutcome
    scheduler=sch.CloudScheduler(None,None)
    scheduler._crawlers={'복산':object}
    searched=[]
    scheduler._search_alternatives=lambda *a: searched.append(a) or []
    sent=[]
    monkeypatch.setattr(Notifier,'send_telegram',lambda *a,**kw: sent.append((a,kw)))
    item={'product_name':'베아놀','quantity':5}
    scheduler._send_auto_order_telegram('chat','인천',[],[{**item,'_src':item}],credentials={'복산':{}},monitor_id='m',unconfirmed_items=[item],fallback_outcomes=[FallbackOutcome(item,5,'복산',0,'unconfirmed','확인 필요')])
    assert not searched
    assert '확인 필요' in sent[0][0][1]
    assert not sent[0][1].get('reply_markup')


def test_partial_order_log_is_not_full_success(monkeypatch):
    primary,*_=run(monkeypatch,[OrderResult(success=True,reason_code='stock_adjusted',adjusted_quantity=10)])
    updates=[params for sql,params in primary.sql if sql.startswith('UPDATE domae_auto_order_logs')]
    assert updates[-1][0]=='partial_fail'
