"""Actual seed receipt conversions must preserve the scheduler deadline boundary.

The guarded runner supplies seed paths and a loopback PostgreSQL DSN. Supplier
transport is replaced by the established local Site fixtures; order code is real.
"""
import pytest

from tests.test_urgent_db import env, run, setup
from tests.urgent_db_fixture import urgent_database, read_urgent
from tests import test_beakje_order as beakje
from tests import test_tjpharm_order as tjpharm
from tests import test_geoweb_order as geoweb
from domae_mcp.cloud import scheduler as sch


def seed_factory(module, site, *, clock=None, guard_passes=None, receipts=None):
    """Instantiate the actual seed with only its session/login test boundary."""
    actual_class = {
        beakje: beakje.bj.BeakjeCrawler,
        tjpharm: tjpharm.tj.TjPharmCrawler,
        geoweb: geoweb.gw.GeoWebCrawler,
    }[module]

    class Factory:
        URGENT_ORDER_SAFE = actual_class.URGENT_ORDER_SAFE
        instances = []

        def __new__(cls):
            crawler = module.crawler(site)
            cls.instances.append(crawler)
            if receipts is not None:
                actual_order = crawler.order

                def recorded_order(*args, **kwargs):
                    scheduler_guard = crawler.send_guard

                    def recorded_guard():
                        scheduler_guard()
                        guard_passes.append(clock[0])

                    crawler.send_guard = recorded_guard
                    receipt = actual_order(*args, **kwargs)
                    receipts.append(receipt)
                    return receipt

                crawler.order = recorded_order
            return crawler

    return Factory


def arrange(env, monkeypatch, module, *, partial=False):
    scheduler, pool, database, _ = env
    clock, guard_passes, receipts, expired_reads = [0], [], [], []
    monkeypatch.setattr(sch.time, 'monotonic', lambda: clock[0])
    if module is beakje:
        supplier, product_id = '백제', 'A|01'
        site = beakje.Site({'A|01': 9})
        original = site.get

        def get(url, **kwargs):
            response = original(url, **kwargs)
            if url.endswith('/ord/basketList') and guard_passes and not expired_reads:
                expired_reads.append(url)
                clock[0] = 601
            return response

        site.get = get
    elif module is tjpharm:
        supplier, product_id = '티제이팜', 'A'
        site = tjpharm.Site({'A': 9})
        original = site.post

        def post(url, **kwargs):
            response = original(url, **kwargs)
            if url.endswith('/Order/basket_api.php') and guard_passes and not expired_reads:
                expired_reads.append(url)
                clock[0] = 601
            return response

        site.post = post
    else:
        supplier, product_id = '지오영', 'A'
        # 검색 행 재고는 Site 의 자기센터·타센터 재고로 만들어진다(실측 구조 2026-10-04).
        site = geoweb.Site({'A': 3 if partial else 9}, {'A': 9} if partial else {})
        original = site.post

        def post(url, **kwargs):
            response = original(url, **kwargs)
            # Stage 1 uses two successful guards. Wait until stage 2's first
            # guard, avoiding the stage 1 accepted-receipt cleanup read.
            threshold = 3 if partial else 1
            if (url.endswith('PartialProductCart') and len(guard_passes) >= threshold
                    and len(site.sends) == int(partial) and not expired_reads):
                expired_reads.append(url)
                clock[0] = 601
            return response

        site.post = post

    if module is beakje:
        next_name, next_pid = '티제이팜', 'A'
        next_site = tjpharm.Site({'A': 9})
        next_factory = seed_factory(tjpharm, next_site)
    else:
        next_name, next_pid = '백제', 'A|01'
        next_site = beakje.Site({'A|01': 9})
        next_factory = seed_factory(beakje, next_site)
    urgent_id, monitor_id, credentials = setup(env, total=5,
        suppliers=((supplier, product_id), (next_name, next_pid)))
    # The TJ Site models exact product-name search. Keep the actual search
    # method and make the scoped test row use its catalog name.
    with database.connection() as connection, connection.cursor() as cursor:
        cursor.execute('UPDATE domae_urgent_orders SET "productName"=%s,"insuranceCode"=NULL WHERE id=%s',
                       ('A', urgent_id))
    first_factory = seed_factory(module, site, clock=clock,
                                 guard_passes=guard_passes, receipts=receipts)
    scheduler._crawlers = {supplier: first_factory, next_name: next_factory}
    return (urgent_id, monitor_id, credentials, site, next_site, first_factory,
            next_factory, guard_passes, receipts, expired_reads)


@pytest.mark.parametrize('module', [beakje, tjpharm, geoweb], ids=['beakje', 'tjpharm', 'geoweb'])
def test_actual_seed_not_sent_deadline_finishes_owned_claim_without_next_supplier(env, monkeypatch, module):
    (urgent_id, _, credentials, site, next_site, first_factory, next_factory,
     guard_passes, receipts, expired_reads) = arrange(env, monkeypatch, module)

    result = run(env, urgent_id, credentials)

    assert len(first_factory.instances) == len(receipts) == len(expired_reads) == 1
    assert guard_passes == [0], 'deadline starts after a successful actual DB guard'
    assert receipts[0].reason_code == 'not_sent' and not receipts[0].success
    assert site.sends == next_site.sends == []
    assert read_urgent(env[2], urgent_id) == (0, True, False, None, False)
    assert next_factory.instances == [], 'no new supplier login/search after deadline'
    assert result.deadline_expired and result.claimed
    assert not result.lost and not result.halted and not result.completed
    assert result.filled == result.total_filled == 0
    assert not env[1]._used


def test_actual_geoweb_stage_two_deadline_commits_first_stage_once_and_waits(env, monkeypatch):
    (urgent_id, monitor_id, credentials, site, next_site, first_factory, next_factory,
     guard_passes, receipts, expired_reads) = arrange(env, monkeypatch, geoweb, partial=True)

    result = run(env, urgent_id, credentials)

    assert len(first_factory.instances) == len(receipts) == len(expired_reads) == 1
    assert guard_passes == [0, 0, 0]
    assert receipts[0].success
    assert receipts[0].adjusted_quantity == receipts[0].fulfilled_quantity == 3
    assert site.sends == [{('A', ''): 3}] and next_site.sends == []
    assert read_urgent(env[2], urgent_id) == (3, True, False, None, False)
    assert next_factory.instances == []
    assert result.deadline_expired and result.claimed
    assert not result.lost and not result.halted and not result.completed
    assert result.filled == result.total_filled == 3
    assert result.supplier_results['지오영']['quantity'] == 3
    assert not env[1]._used
    # No residual sendingAt/token is left for a false stale-recovery transition.
    with env[2].connection() as connection, connection.cursor() as cursor:
        assert env[0]._recover_stale_urgent(connection, cursor, monitor_id) == []
    assert read_urgent(env[2], urgent_id) == (3, True, False, None, False)
