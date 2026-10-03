"""격리된 로컬 PostgreSQL에서 실제 커밋/rollback과 NULL 보호를 검증한다."""
import os
import sys
import uuid
sys.path.insert(0, 'src')
import psycopg2
import pytest
from domae_mcp.cloud import scheduler as sch
from domae_mcp.cloud.fallback_db import FallbackRecorder
from domae_mcp.cloud.fallback import run_fallback
from domae_mcp.core.crawlers.base import SearchResult, OrderResult


def validate_test_dsn(dsn):
    options = psycopg2.extensions.parse_dsn(dsn)
    allowed = {'127.0.0.1', '::1', 'localhost'}
    if options.get('host') not in allowed or options.get('hostaddr', options.get('host')) not in allowed or options.get('dbname') != 'domae_resilience':
        raise ValueError('통합 테스트는 loopback의 전용 domae_resilience DB만 허용합니다')
    return dsn


@pytest.fixture
def database():
    dsn=os.environ.get('DOMAE_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('격리된 로컬 DB DSN을 지정해야 하는 통합 테스트')
    schema='resilience_'+uuid.uuid4().hex
    origin=psycopg2.connect(validate_test_dsn(dsn))
    cur=origin.cursor()
    cur.execute(f'CREATE SCHEMA {schema}')
    cur.execute(f'SET search_path TO {schema}')
    cur.execute('''CREATE TABLE domae_cloud_orders (
      id text PRIMARY KEY, "monitorId" text, "batchId" text, supplier text,
      "productName" text, unit text, "insuranceCode" text, quantity integer, price integer,
      success boolean, "productId" text, "orderId" text, message text,
      "reasonCode" text, "orderedAt" timestamp, "adjustedQuantity" integer, "availableStock" integer, "confirmedQuantity" integer)''')
    cur.execute('''CREATE TABLE domae_order_batches (
      id text PRIMARY KEY, status text, "completedAt" timestamp, "successCount" integer,
      "failCount" integer, "adjustedCount" integer, "missingQuantity" integer)''')
    cur.execute("INSERT INTO domae_order_batches (id,status) VALUES ('b','processing')")
    origin.commit()
    recorder=psycopg2.connect(dsn)
    recorder.cursor().execute(f'SET search_path TO {schema}')
    recorder.commit()
    try:
        yield origin,recorder
    finally:
        recorder.close()
        origin.rollback()
        origin.cursor().execute(f'DROP SCHEMA {schema} CASCADE')
        origin.commit()
        origin.close()


def test_original_marker_survives_sql_failure_and_prevents_retry(database):
    origin,observer=database
    cur=origin.cursor()
    cur.execute('''INSERT INTO domae_cloud_orders (id,"batchId",supplier,success)
                   VALUES ('o','b','인천',NULL),('p','b','백제',NULL)''')
    origin.commit()
    sch._mark_sending(origin,cur,'b','인천')
    with pytest.raises(psycopg2.Error):
        cur.execute('SELECT 1/0')
    origin.rollback()
    sch._fail_pending_rows(cur,'b','SQL failure')
    origin.commit()
    c=observer.cursor()
    c.execute('SELECT id,success,"reasonCode" FROM domae_cloud_orders ORDER BY id')
    assert c.fetchall()==[('o',None,'send_unknown'),('p',False,None)]
    assert sch._finalize_if_confirmed(origin,cur,'b','retry blocked')
    c.execute("SELECT success FROM domae_cloud_orders WHERE id='o'")
    assert c.fetchone()==(None,)
    c.execute("SELECT status,\"completedAt\" FROM domae_order_batches WHERE id='b'")
    assert c.fetchone()==('processing',None)


def test_only_unconfirmed_rows_block_rerun(database):
    origin,_=database
    cur=origin.cursor()
    cur.execute('''INSERT INTO domae_cloud_orders (id,"batchId",success,"reasonCode")
                   VALUES ('u','b',NULL,'send_unknown')''')
    origin.commit()
    assert sch._finalize_if_confirmed(origin,cur,'b','unknown only')
    cur.execute("SELECT success FROM domae_cloud_orders WHERE id='u'")
    assert cur.fetchone()==(None,)
    cur.execute("SELECT status,\"completedAt\" FROM domae_order_batches WHERE id='b'")
    assert cur.fetchone()==('processing',None)


def test_fallback_success_record_sql_failure_preserves_null_and_origin_connection(database):
    origin,secondary=database
    pick=SearchResult(maker='',product_name='베아놀',unit='12EA',insurance_code='694003321',quantity=30,supplier='복산',price=1000,product_id='p')
    # 성공 결과 기록에서만 DB 오류가 난다. pending INSERT는 실제로 커밋된다.
    def broken_record(cur,*a,**kw):
        cur.execute('SELECT 1/0')
    rec=FallbackRecorder(secondary,'m','b','인천',broken_record,id_fn=lambda:'f',now_fn=lambda:None)
    calls=[]
    class Crawler:
        def search(self,code): return [pick]
        def order(self,*a,**kw): calls.append(a);return OrderResult(success=True)
    outcome=run_fallback([({'insurance_code':'694003321','unit':'12EA','quantity':15},15)],['복산','백제'],lambda s:Crawler(),lambda s:'nolock',lambda *a:True,lambda *a:None,rec.pending,rec.result,rec.unconfirmed)[0]
    assert outcome.state=='unconfirmed' and len(calls)==1
    cur=origin.cursor()
    cur.execute("SELECT success,\"reasonCode\" FROM domae_cloud_orders WHERE id='f'")
    assert cur.fetchone()==(None,'fallback_pending')
    sch._fail_pending_rows(cur,'b','worker failed')
    origin.commit()
    cur.execute("SELECT success FROM domae_cloud_orders WHERE id='f'")
    assert cur.fetchone()==(None,)
    assert sch._finalize_if_confirmed(origin,cur,'b','retry')


@pytest.mark.parametrize('dsn',[
    'postgresql://u:p@production.example/domae_resilience',
    'postgresql://u:p@127.0.0.1/pharmsquare',
    'host=localhost hostaddr=192.0.2.1 dbname=domae_resilience',
])
def test_test_database_guard_rejects_unsafe_target(dsn):
    with pytest.raises(ValueError):
        validate_test_dsn(dsn)


@pytest.mark.parametrize('fallback_result,remaining,unknown',[
    (OrderResult(success=True),None,False),
    (OrderResult(success=True,reason_code='stock_adjusted',adjusted_quantity=3),2,False),
    (TimeoutError('response lost'),5,True),
    (SystemExit('worker killed after send boundary'),15,True),
])
def test_auto_order_to_real_fallback_recorder_and_cart(database,monkeypatch,fallback_result,remaining,unknown):
    import json
    from domae_mcp.cloud import notifier as nmod
    origin,secondary=database
    cur=origin.cursor()
    cur.execute('ALTER TABLE domae_order_batches ADD COLUMN "monitorId" text')
    cur.execute("UPDATE domae_order_batches SET status='pending',\"monitorId\"='m'")
    cur.execute('''CREATE TABLE domae_cloud_monitors (
        id text, credentials jsonb, "telegramChatId" text, "supplierOrder" jsonb,
        "autoFallbackOrder" boolean, "isActive" boolean)''')
    cur.execute('''CREATE TABLE domae_cart_items (
        id text, quantity integer, "failedAt" timestamp, "failReason" text)''')
    cur.execute('''CREATE TABLE domae_auto_order_logs (
        "monitorId" text, "batchId" text, status text, message text)''')
    creds={'인천':{'login_id':'test'},'복산':{'login_id':'test'}}
    cur.execute('INSERT INTO domae_cloud_monitors VALUES (%s,%s,%s,%s,true,true)',('m',json.dumps(creds),'chat',json.dumps(['인천','복산'])))
    cur.execute("INSERT INTO domae_cart_items VALUES ('c',15,NULL,NULL)")
    cur.execute("INSERT INTO domae_auto_order_logs VALUES ('m','b','pending',NULL)")
    cur.execute('''INSERT INTO domae_cloud_orders (id,"monitorId","batchId",supplier,
                   "productName",quantity,success) VALUES ('o','m','b','인천','베아놀',15,NULL)''')
    origin.commit()
    network_calls=[]
    class OriginCrawler:
        def login(self,*a): return True
        def search(self,*a): return []
        def order_batch(self,items):
            assert items[0]['insurance_code']=='694003321'
            if isinstance(fallback_result,SystemExit): raise fallback_result
            return [OrderResult(success=True,reason_code='stock_adjusted',adjusted_quantity=10)]
    class CandidateCrawler:
        def login(self,*a): return True
        def search(self,code):
            assert code=='694003321'
            return [SearchResult(maker='',product_name='wrong pack',unit='100EA',insurance_code=code,quantity=99,supplier='복산',price=900,product_id='wrong'),
                    SearchResult(maker='',product_name='베아놀',unit='12EA',insurance_code=code,quantity=8,supplier='복산',price=1000,product_id='candidate')]
        def order(self,pid,qty,**kw):
            # 외부 전송 경계에서 독립 DB 연결로 pending 커밋과 원 잔량 커밋을 읽는다.
            c=origin.cursor()
            c.execute("SELECT success,\"reasonCode\",quantity FROM domae_cloud_orders WHERE supplier='복산'")
            assert c.fetchone()==(None,'fallback_pending',5)
            c.execute("SELECT quantity,\"failedAt\" IS NOT NULL FROM domae_cart_items WHERE id='c'")
            assert c.fetchone()==(5,True)
            network_calls.append((pid,qty))
            if isinstance(fallback_result,Exception): raise fallback_result
            return fallback_result
    class Pool:
        def __init__(self): self.available=[origin,secondary];self.returned=[]
        def getconn(self): return self.available.pop(0)
        def putconn(self,conn): self.returned.append(conn)
    class Redis:
        def __init__(self): self.events=[]
        def publish(self,key,data): self.events.append(json.loads(data))
    sent=[]
    class Response:
        status_code=200
        def json(self): return {'ok':True,'result':{'message_id':1}}
    def send(*a,**kw): sent.append(kw['json']);return Response()
    monkeypatch.setenv('DOMAE_TELEGRAM_BOT_TOKEN','test:local')
    monkeypatch.setattr(nmod.requests,'post',send)
    nmod.Notifier.set_delivery_sinks()
    pool,redis=Pool(),Redis()
    scheduler=sch.CloudScheduler(pool,redis)
    scheduler._crawlers_loaded=True
    scheduler._crawlers={'인천':OriginCrawler,'복산':CandidateCrawler}
    item={'product_id':'original','product_name':'베아놀','quantity':15,'price':1000,'insurance_code':'694003321','unit':'12EA','db_order_id':'o','cart_item_id':'c'}
    job={'monitor_id':'m','batch_id':'b','supplier':'인천','items':[item]}
    if isinstance(fallback_result,SystemExit):
        with pytest.raises(SystemExit):
            scheduler.auto_order(job)
        cur.execute("SELECT success,\"reasonCode\" FROM domae_cloud_orders WHERE id='o'")
        assert cur.fetchone()==(None,'send_unknown')
        cur.execute("SELECT status,\"completedAt\" FROM domae_order_batches WHERE id='b'")
        assert cur.fetchone()==('processing',None)
        # 재수신해도 소유권을 얻지 못하므로 외부 주문으로 진행하지 않는다.
        pool.available.insert(0,origin)
        scheduler.auto_order(job)
        assert not network_calls
        return
    scheduler.auto_order(job)
    assert network_calls==[('candidate',5)] and pool.returned==[secondary,origin]
    cur.execute("SELECT success,\"reasonCode\",\"adjustedQuantity\" FROM domae_cloud_orders WHERE id='o'")
    assert cur.fetchone()==(True,'stock_adjusted',10)
    cur.execute("SELECT success,\"reasonCode\",quantity FROM domae_cloud_orders WHERE supplier='복산'")
    row=cur.fetchone()
    assert row[0] is (None if unknown else True) and row[2]==5
    if unknown: assert row[1]=='send_unknown'
    cur.execute("SELECT quantity,\"failedAt\" IS NOT NULL FROM domae_cart_items WHERE id='c'")
    assert cur.fetchone()==(None if remaining is None else (remaining,True))
    assert '요청 15개' in sent[0]['text'] and not sent[0].get('reply_markup')
    assert redis.events[0]['fallbackOrdered']==(0 if unknown else 1)


@pytest.mark.parametrize('method',['auto_order','batch_order'])
def test_pre_send_marker_commit_failure_sends_nothing_and_keeps_safe_db_state(database,method):
    import json
    origin,observer=database
    cur=origin.cursor()
    cur.execute('ALTER TABLE domae_order_batches ADD COLUMN "monitorId" text')
    cur.execute("UPDATE domae_order_batches SET status='pending',\"monitorId\"='m'")
    cur.execute('''CREATE TABLE domae_cloud_monitors (
        id text, credentials jsonb, "telegramChatId" text, "supplierOrder" jsonb,
        "autoFallbackOrder" boolean, "isActive" boolean)''')
    cur.execute('''CREATE TABLE domae_cart_items (
        id text, quantity integer, "failedAt" timestamp, "failReason" text)''')
    cur.execute('''CREATE TABLE domae_auto_order_logs (
        "monitorId" text, "batchId" text, status text, message text)''')
    cur.execute('INSERT INTO domae_cloud_monitors VALUES (%s,%s,NULL,%s,false,true)',
                ('m',json.dumps({'인천':{'login_id':'test'}}),json.dumps(['인천'])))
    cur.execute("INSERT INTO domae_cart_items VALUES ('c',15,NULL,NULL)")
    cur.execute("INSERT INTO domae_auto_order_logs VALUES ('m','b','pending',NULL)")
    cur.execute('''INSERT INTO domae_cloud_orders (id,"monitorId","batchId",supplier,
                   "productName",quantity,success) VALUES ('o','m','b','인천','베아놀',15,NULL)''')
    origin.commit()

    class FailMarkerCursor:
        def __init__(self,connection): self.connection=connection;self.raw=origin.cursor()
        def execute(self,sql,params=None):
            self.raw.execute(sql,params)
            if 'SET "reasonCode" = %s, message = %s' in sql and params[0]=='send_unknown':
                self.connection.marker_executed=True
                self.connection.fail_next_commit=True
        def __getattr__(self,name): return getattr(self.raw,name)
    class FailMarkerConnection:
        marker_executed=False
        failed=False
        fail_next_commit=False
        rollback_count=0
        def cursor(self): return FailMarkerCursor(self)
        def commit(self):
            if self.fail_next_commit and not self.failed:
                self.failed=True
                self.fail_next_commit=False
                raise psycopg2.OperationalError('injected pre-send commit failure')
            return origin.commit()
        def rollback(self): self.rollback_count+=1;return origin.rollback()
    conn=FailMarkerConnection()
    class Pool:
        def __init__(self): self.returned=[]
        def getconn(self): return conn
        def putconn(self,c): self.returned.append(c)
    calls=[]
    class Crawler:
        def login(self,*a): return True
        def search(self,*a): return []
        def order_batch(self,*a): calls.append('order_batch');return [OrderResult(success=True)]
        def order(self,*a,**kw): calls.append('order');return OrderResult(success=True)
    class Redis:
        def publish(self,*a): pass
    pool=Pool()
    scheduler=sch.CloudScheduler(pool,Redis())
    scheduler._crawlers_loaded=True
    scheduler._crawlers={'인천':Crawler}
    item={'supplier':'인천','product_id':'p','product_name':'베아놀','quantity':15,
          'insurance_code':'694003321','unit':'12EA','db_order_id':'o','cart_item_id':'c'}
    job={'monitor_id':'m','batch_id':'b','supplier':'인천','items':[item]}
    getattr(scheduler,method)(job)
    assert conn.marker_executed and conn.failed and conn.rollback_count==1
    assert not calls and pool.returned==[conn]
    # 보호 커밋이 실패하면 전송하지 않았으므로 not_sent 로 확정한다(A4). 원 수량은 보존된다.
    c=observer.cursor()
    c.execute("SELECT success,\"reasonCode\" FROM domae_cloud_orders WHERE id='o'")
    assert c.fetchone()==(False,'not_sent')
    c.execute("SELECT quantity,\"failedAt\" IS NOT NULL FROM domae_cart_items WHERE id='c'")
    assert c.fetchone()==(15,True)
    cur.execute('SELECT 1')
    assert cur.fetchone()==(1,)
    # pending 상태로 재수신해도 보호된 행 때문에 실제 메서드의 멱등 게이트에서 중단한다.
    cur.execute("UPDATE domae_order_batches SET status='pending' WHERE id='b'")
    origin.commit()
    getattr(scheduler,method)(job)
    assert not calls
    c.execute("SELECT status,\"completedAt\" IS NOT NULL FROM domae_order_batches WHERE id='b'")
    assert c.fetchone()==('failed',True)


@pytest.mark.parametrize('scenario',['unknown','exception','crash','marker_commit_failure','result_sql_failure','accepted','not_sent'])
def test_quick_order_keeps_unknown_and_never_resends(database,scenario):
    import json
    origin,secondary=database
    cur=origin.cursor()
    cur.execute('ALTER TABLE domae_order_batches ADD COLUMN "monitorId" text')
    cur.execute("UPDATE domae_order_batches SET status='pending',\"monitorId\"='m'")
    cur.execute('CREATE TABLE domae_cloud_monitors (id text, credentials jsonb, "telegramChatId" text, "isActive" boolean)')
    cur.execute('CREATE TABLE domae_cart_items (id text, quantity integer, "failedAt" timestamp, "failReason" text)')
    cur.execute('INSERT INTO domae_cloud_monitors VALUES (%s,%s,NULL,true)',('m',json.dumps({'인천':{'login_id':'test'}})))
    cur.execute("INSERT INTO domae_cart_items VALUES ('c',15,NULL,NULL)")
    cur.execute('''INSERT INTO domae_cloud_orders (id,"monitorId","batchId",supplier,
                   "productName",quantity,success) VALUES ('o','m','b','인천','베아놀',15,NULL)''')
    origin.commit()
    class Connection:
        def __init__(self,raw): self.raw=raw;self.fail_commit=False;self.failed=False;self.rollbacks=0
        def cursor(self): return Cursor(self)
        def commit(self):
            if self.fail_commit and not self.failed:
                self.failed=True
                raise psycopg2.OperationalError('pre-send commit failure')
            self.raw.commit()
        def rollback(self): self.rollbacks+=1;self.raw.rollback()
    class Cursor:
        def __init__(self,connection): self.conn=connection;self.raw=connection.raw.cursor()
        def execute(self,sql,params=None):
            if scenario=='result_sql_failure' and self.conn.raw is secondary and 'SET success = %s' in sql:
                self.raw.execute('SELECT 1/0')
            self.raw.execute(sql,params)
            if scenario=='marker_commit_failure' and 'SET "reasonCode" = %s' in sql:
                self.conn.fail_commit=True
        def __getattr__(self,name): return getattr(self.raw,name)
    wrapped_origin,wrapped_secondary=Connection(origin),Connection(secondary)
    class Pool:
        def __init__(self): self.available=[wrapped_origin,wrapped_secondary]
        def getconn(self): return self.available.pop(0)
        def putconn(self,conn): pass
    class Redis:
        def __init__(self): self.responses=[]
        def lpush(self,key,data): self.responses.append(json.loads(data))
        def expire(self,*a): pass
    calls=[]
    class Crawler:
        def login(self,*a): return True
        def search(self,*a): return []
        def order(self,*a,**kw):
            calls.append(a)
            if scenario=='exception': raise TimeoutError('lost')
            if scenario=='crash': raise SystemExit('killed')
            if scenario in ('result_sql_failure','accepted'): return OrderResult(success=True,reason_code='ok')
            if scenario=='not_sent': return OrderResult(success=False,reason_code='not_sent')
            return OrderResult(success=False,reason_code='send_unknown',message='접수 불명')
    pool,redis=Pool(),Redis()
    scheduler=sch.CloudScheduler(pool,redis)
    scheduler._crawlers_loaded=True
    scheduler._crawlers={'인천':Crawler}
    job={'monitor_id':'m','response_key':'response','supplier':'인천','product_id':'p',
         'quantity':15,'db_order_id':'o','db_batch_id':'b','cart_item_id':'c'}
    if scenario=='crash':
        with pytest.raises(SystemExit): scheduler.order(job)
    else:
        scheduler.order(job)
    assert len(calls)==(0 if scenario=='marker_commit_failure' else 1)
    if scenario in ('accepted','not_sent'):
        cur.execute("SELECT success FROM domae_cloud_orders WHERE id='o'")
        assert cur.fetchone()==(scenario=='accepted',)
        cur.execute("SELECT status,\"failCount\",\"completedAt\" IS NOT NULL FROM domae_order_batches WHERE id='b'")
        assert cur.fetchone()==('completed',0 if scenario=='accepted' else 1,True)
        pool.available=[wrapped_origin,wrapped_secondary]
        scheduler.order(job)
        assert len(calls)==1
        return
    cur.execute("SELECT success,\"reasonCode\" FROM domae_cloud_orders WHERE id='o'")
    assert cur.fetchone()==(None,'send_unknown')
    cur.execute("SELECT status,\"failCount\",\"completedAt\" FROM domae_order_batches WHERE id='b'")
    status,count,completed=cur.fetchone()
    assert status=='processing' and (count or 0)==0 and completed is None
    if scenario not in ('crash','result_sql_failure'):
        assert redis.responses[-1]['success'] is None
        cur.execute("SELECT \"failedAt\" IS NOT NULL FROM domae_cart_items WHERE id='c'")
        assert cur.fetchone()==(True,)
    if scenario=='result_sql_failure':
        assert wrapped_secondary.rollbacks==1
    # 같은 잡을 다시 넣어도 요청을 반복하지 않는다.
    before=len(calls)
    pool.available=[wrapped_origin,wrapped_secondary]
    scheduler.order(job)
    assert len(calls)==before

def test_result_confirmed_quantity_is_persisted(database):
    from domae_mcp.cloud.scheduler import _record_order_result
    conn, _ = database
    cur = conn.cursor()
    _record_order_result(cur, 'm', 'b', '백제', {'quantity': 5, 'product_name': 'test'},
                         success=False, message='partial', reason_code='send_unknown', confirmed_qty=3)
    cur.execute('SELECT success, "confirmedQuantity" FROM domae_cloud_orders WHERE "productName" = %s', ('test',))
    assert cur.fetchone() == (None, 3)

def test_notify_monitor_closes_local_connection_before_send(database, monkeypatch):
    from domae_mcp.cloud.notifier import Notifier
    origin, independent = database
    cur = origin.cursor()
    cur.execute('CREATE TABLE domae_cloud_monitors (id text, "telegramChatId" text)')
    cur.execute("INSERT INTO domae_cloud_monitors VALUES ('m','12345')")
    origin.commit()
    monkeypatch.setattr(psycopg2, 'connect', lambda *a, **k: independent)
    monkeypatch.setenv('DATABASE_URL', 'unused-local-mocked')
    sent = []
    def send(chat, message, reply_markup=None):
        assert independent.closed
        sent.append((chat, message, reply_markup))
    monkeypatch.setattr(Notifier, 'send_telegram', send)
    Notifier.notify_monitor('m', 'hello', reply_markup={'x': 1})
    assert sent == [('12345', 'hello', {'x': 1})]

def test_cart_release_commits_audit_once_in_isolated_schema(database, monkeypatch):
    import fakeredis
    from domae_mcp.core.crawlers.cart_snapshot import CartSnapshot
    from domae_mcp.cloud.notifier import Notifier
    origin, observer = database
    cur = origin.cursor()
    cur.execute('CREATE TABLE domae_cloud_monitors (id text, credentials jsonb, "telegramChatId" text, "isActive" boolean)')
    cur.execute('''CREATE TABLE domae_order_audit_events (id text PRIMARY KEY, "monitorId" text, supplier text,
        "eventType" text, source text, payload jsonb, "createdAt" timestamp)''')
    cur.execute("INSERT INTO domae_cloud_monitors VALUES ('monitor1full','{}','12345',true)")
    origin.commit()
    class Pool:
        def getconn(self): return origin
        def putconn(self, c): c.rollback()
    scheduler = sch.CloudScheduler(Pool(), fakeredis.FakeRedis())
    scheduler._decrypt_creds = lambda c: {'백제': {'login_id': 'local-account'}}
    store = CartSnapshot(scheduler._redis, 'monitor1full', '백제', account='local-account')
    store.lock(); rev = store.save({'original': 3}); store.unlock()
    sent = []
    monkeypatch.setattr(Notifier, 'send_telegram', lambda *a, **k: sent.append(a))
    job = {'monitor_id': 'monitor1full', 'monitor_prefix': 'monitor1', 'supplier': '백제', 'revision': rev, 'chat_id': '12345'}
    scheduler.cart_release(job); scheduler.cart_release(job)
    cur = observer.cursor()
    cur.execute('SELECT "monitorId",supplier,"eventType",source,payload FROM domae_order_audit_events')
    assert cur.fetchall() == [('monitor1full','백제','cart_snapshot_released','worker',{'revision': rev})]
    assert store.load() is None and len(sent) == 2


def test_fallback_unknown_partial_survives_null_row(database):
    origin, recorder_conn = database
    recorder = FallbackRecorder(recorder_conn, 'm', 'b', '백제', sch._record_order_result, sch._generate_cuid, lambda: '2026-10-03')
    pick = SearchResult(maker='', product_name='약', supplier='백제', unit='12EA', insurance_code='694003321', quantity=30, product_id='p', price=0)
    item = {'quantity': 5}
    row = recorder.pending(item, '백제', pick, 5)
    recorder.unconfirmed(row, 'partial', confirmed=3)
    cur = origin.cursor(); cur.execute('SELECT success,"confirmedQuantity" FROM domae_cloud_orders WHERE id=%s', (row,))
    assert cur.fetchone() == (None, 3)

@pytest.mark.parametrize('boundary', ['insert_failure', 'commit_failure', 'after_release', 'after_insert', 'after_commit', 'commit_ambiguity', 'before_ack'])
def test_cart_release_audit_recovers_after_failure_and_interruption(database, monkeypatch, boundary):
    import fakeredis
    from domae_mcp.core.crawlers.cart_snapshot import CartSnapshot
    from domae_mcp.cloud.notifier import Notifier
    origin, observer = database
    cur = origin.cursor()
    cur.execute('CREATE TABLE domae_cloud_monitors (id text, credentials jsonb, "telegramChatId" text, "isActive" boolean)')
    cur.execute('''CREATE TABLE domae_order_audit_events (id text PRIMARY KEY, "monitorId" text, supplier text,
        "eventType" text, source text, payload jsonb, "createdAt" timestamp)''')
    cur.execute("INSERT INTO domae_cloud_monitors VALUES ('monitor1full','{}','12345',true)")
    origin.commit()
    redis = fakeredis.FakeRedis()
    store = CartSnapshot(redis, 'monitor1full', '백제', account='local-account')
    store.lock(); rev = store.save({'original': 3}); store.unlock()
    job = {'monitor_id': 'monitor1full', 'monitor_prefix': 'monitor1', 'supplier': '백제', 'revision': rev, 'chat_id': '12345'}
    state = {'fail': True}
    class Cursor:
        def __init__(self, cursor): self.cursor = cursor
        def execute(self, sql, params=None):
            audit = 'INSERT INTO domae_order_audit_events' in sql
            if audit and state['fail'] and boundary == 'insert_failure': raise RuntimeError('insert failed')
            out = self.cursor.execute(sql, params)
            if audit and state['fail'] and boundary == 'after_insert': raise KeyboardInterrupt('crash after insert')
            return out
        def __getattr__(self, name): return getattr(self.cursor, name)
    class Connection:
        def cursor(self): return Cursor(origin.cursor())
        def rollback(self): return origin.rollback()
        def commit(self):
            if state['fail'] and boundary == 'commit_failure': raise RuntimeError('commit failed')
            origin.commit()
            if state['fail'] and boundary == 'after_commit': raise KeyboardInterrupt('commit acknowledgement lost')
            if state['fail'] and boundary == 'commit_ambiguity': raise RuntimeError('commit acknowledgement lost')
    connection = Connection()
    class Pool:
        def getconn(self): return connection
        def putconn(self, c): c.rollback()
    scheduler = sch.CloudScheduler(Pool(), redis)
    scheduler._decrypt_creds = lambda c: {'백제': {'login_id': 'local-account'}}
    monkeypatch.setattr(Notifier, 'send_telegram', lambda *a, **k: None)
    release = CartSnapshot.release
    def release_crash(self, revision):
        result = release(self, revision)
        if state['fail'] and boundary == 'after_release': raise KeyboardInterrupt('crash after release')
        return result
    monkeypatch.setattr(CartSnapshot, 'release', release_crash)
    if hasattr(CartSnapshot, 'ack_release'):
        ack = CartSnapshot.ack_release
        def ack_crash(redis_client, key, receipt):
            if state['fail'] and boundary == 'before_ack': raise RuntimeError('ack failed')
            return ack(redis_client, key, receipt)
        monkeypatch.setattr(CartSnapshot, 'ack_release', ack_crash)
    try:
        scheduler.cart_release(job)
    except KeyboardInterrupt:
        pass
    assert store.load() is None
    assert redis.zcard('domae:cart_release_pending') == 1
    pending_key = redis.zrange('domae:cart_release_pending', 0, 0)[0]
    receipt = CartSnapshot.release_receipt(redis, pending_key)
    # Credential changes/deactivation must not erase the already accepted receipt.
    cur = origin.cursor(); cur.execute('UPDATE domae_cloud_monitors SET "isActive"=false, credentials=\'{}\''); origin.commit()
    # A new run may start before audit recovery: recovery never recreates/deletes its cart record.
    next_run = CartSnapshot(redis, 'another-monitor', '백제', account='local-account')
    next_run.lock(); next_run.save({'new': 7})
    record = next_run.load()
    state['fail'] = False
    scheduler.recover_cart_releases()
    scheduler.recover_cart_releases()
    assert next_run.load() == record and redis.get(next_run.lock_key).decode() == next_run.run_id
    assert redis.zcard('domae:cart_release_pending') == 0
    cur = observer.cursor(); cur.execute('SELECT "monitorId", supplier, "eventType", payload FROM domae_order_audit_events')
    assert cur.fetchall() == [('monitor1full', '백제', 'cart_snapshot_released', {'revision': rev})]
    from datetime import datetime
    cur.execute('SELECT id, "createdAt" FROM domae_order_audit_events')
    assert cur.fetchone() == (receipt['id'], datetime.fromisoformat(receipt['releasedAt']).replace(tzinfo=None))

def test_audit_recovery_rotates_permanent_pg_failures_without_starving(database):
    import fakeredis
    from domae_mcp.core.crawlers.cart_snapshot import CartSnapshot
    origin, observer = database
    cur = origin.cursor()
    cur.execute('''CREATE TABLE domae_order_audit_events (
        id text PRIMARY KEY, "monitorId" text CHECK ("monitorId" = 'healthy'), supplier text,
        "eventType" text, source text, payload jsonb, "createdAt" timestamp)''')
    origin.commit()
    redis = fakeredis.FakeRedis()
    originals = {}
    for i in range(11):
        store = CartSnapshot(redis, 'healthy' if i == 10 else f'failed{i}', '백제', account=f'pg-fair{i}')
        store.lock(); rev = store.save({}); store.unlock(); store.release(rev)
        key = store.release_key(rev)
        originals[key] = redis.get(key)
    class Pool:
        def getconn(self): return origin
        def putconn(self, conn, close=False): conn.rollback()
    scheduler = sch.CloudScheduler(Pool(), redis)
    persisted = scheduler._persist_cart_release
    attempted = []
    def persist(conn, key, receipt):
        attempted.append(receipt['id'])
        return persisted(conn, key, receipt)
    scheduler._persist_cart_release = persist
    for _ in range(3):
        before = len(attempted)
        scheduler.recover_cart_releases()
        assert len(attempted) - before <= 10
    cur = observer.cursor()
    cur.execute('SELECT "monitorId" FROM domae_order_audit_events')
    assert cur.fetchall() == [('healthy',)]
    assert redis.zcard('domae:cart_release_pending') == 10
    for key in redis.zrange('domae:cart_release_pending', 0, -1):
        assert redis.get(key) == originals[key.decode()] and redis.ttl(key) == -1
