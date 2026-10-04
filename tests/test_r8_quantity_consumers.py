"""Actual selected private seed receipts consumed by worker paths against scoped local PG."""
from datetime import datetime

import pytest

from tests.urgent_db_fixture import urgent_database, seed_monitor, read_urgent
from tests.test_tjpharm_order import Site as TjSite, crawler as tj_crawler
from tests.test_beakje_order import Site as BjSite, crawler as bj_crawler
from tests.test_telegram_paths import env as telegram_env
from tests.test_scheduler_unknown_boundaries import env as scheduler_env, run_reported_result
from tests.test_urgent_db import env as urgent_env, setup, immediate, make_crawler, logs
from tests.test_fallback_db import PICK
from domae_mcp.cloud.fallback import run_fallback
from domae_mcp.cloud.fallback_db import FallbackRecorder
from domae_mcp.cloud.scheduler import _record_order_result


@pytest.fixture(params=[('tj', False), ('tj', True), ('bj', False), ('bj', True)])
def seed_receipt(request):
    supplier, partial = request.param
    def accepted(requested):
        stock = 3 if partial else requested + 4
        if supplier == 'tj':
            site = TjSite({'A': stock})
            receipt = tj_crawler(site).order('A', requested)
            assert site.sends == [{'A': min(stock, requested)}]
        else:
            site = BjSite({'A|01': stock})
            receipt = bj_crawler(site).order('A|01', requested)
            assert site.sends == [{'A|01': min(stock, requested)}]
        assert receipt.success
        assert receipt.original_quantity == requested
        assert receipt.adjusted_quantity == (3 if partial else None)
        assert receipt.fulfilled_quantity == (3 if partial else requested)
        return receipt
    return accepted, partial


@pytest.mark.parametrize('path', ['batch_order', 'batch_retry', 'auto_order', 'order'])
def test_real_seed_receipt_scheduler_db_and_cart_remaining(scheduler_env, seed_receipt, path):
    accepted, partial = seed_receipt
    receipt = accepted(15)
    run_reported_result(scheduler_env, path, receipt)
    order, cart, _ = scheduler_env.read()
    assert order == (True, receipt.reason_code, 3 if partial else 15, 3 if partial else None)
    if path == 'order':
        assert cart[0] == 15
    elif partial:
        assert cart[0] == 12
    else:
        assert cart is None
    assert len(scheduler_env.calls) == (2 if path == 'batch_retry' else 1)
    assert 'order' not in scheduler_env.alternatives


def test_real_seed_receipt_telegram_db_and_notice_quantity(telegram_env, seed_receipt):
    accepted, partial = seed_receipt
    receipt = accepted(5)
    telegram_env.state['result'] = receipt
    telegram_env.run()
    row = telegram_env.rows()[0]
    actual = 3 if partial else 5
    assert row[:3] == (True, receipt.reason_code, actual)
    assert row[5] == (3 if partial else None)
    assert len(telegram_env.calls) == 1
    assert str(actual) in telegram_env.sent[-1]['text']


def test_real_seed_receipt_urgent_db_filled_quantity(urgent_env, seed_receipt):
    accepted, partial = seed_receipt
    receipt = accepted(5)
    sc, _, db, _ = urgent_env
    uo, mid, _ = setup(urgent_env, total=5)
    sc._crawlers = {'인천': make_crawler(results=[receipt])}
    outcome = immediate(urgent_env, uo, mid)
    actual = 3 if partial else 5
    assert (outcome['filled_quantity'], outcome['total_filled']) == (actual, actual)
    assert read_urgent(db, uo)[0] == actual
    assert logs(db, uo)[0][:3] == ('인천', actual, True)
    assert sc._crawlers['인천'].orders == [('P1', 5)]


def test_real_seed_receipt_fallback_durable_confirmed_quantity(urgent_database, seed_receipt):
    accepted, partial = seed_receipt
    receipt = accepted(5)
    db = urgent_database
    mid = seed_monitor(db, ('복산', '백제'))
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute('INSERT INTO domae_order_batches (id,"monitorId","totalItems") VALUES (%s,%s,1)', ('r8batch', mid))
    calls = []
    class Crawler:
        def search(self, keyword): return [PICK]
        def order(self, pid, qty, **metadata):
            calls.append((pid, qty))
            return receipt
    with db.connection() as conn:
        recorder = FallbackRecorder(conn, mid, 'r8batch', '인천', _record_order_result,
                                    lambda: 'r8receipt', datetime.now)
        outcome, = run_fallback([({'insurance_code': PICK.insurance_code, 'unit': PICK.unit, 'quantity': 5}, 5)],
            ['복산', '백제'], lambda supplier: Crawler(), lambda supplier: 'lock',
            lambda *args: True, lambda *args: None, recorder.pending, recorder.result, recorder.unconfirmed)
    actual = 3 if partial else 5
    assert outcome.state == 'ordered' and outcome.ordered_qty == actual
    assert calls == [('b1', 5)]
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute('SELECT success,"reasonCode","confirmedQuantity","adjustedQuantity" FROM domae_cloud_orders WHERE id=%s', ('r8receipt',))
        assert cur.fetchone() == (True, receipt.reason_code, actual, 3 if partial else None)
