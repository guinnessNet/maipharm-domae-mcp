"""Local-only, per-test PostgreSQL schemas; never mutate public tables."""
import json
import os
import uuid
from contextlib import closing, contextmanager

import psycopg2
from psycopg2 import sql
from psycopg2.pool import ThreadedConnectionPool
import pytest


def validate_test_dsn(dsn):
    options = psycopg2.extensions.parse_dsn(dsn)
    allowed = {'127.0.0.1', '::1', 'localhost'}
    if options.get('host') not in allowed or options.get('hostaddr', options.get('host')) not in allowed or options.get('dbname') != 'domae_resilience':
        raise ValueError('통합 테스트는 loopback의 전용 domae_resilience DB만 허용합니다')
    return dsn


DDL = '''
CREATE TABLE domae_cloud_monitors (
 id text PRIMARY KEY, "apiKeyId" text NOT NULL UNIQUE, products jsonb NOT NULL DEFAULT '[]',
 credentials jsonb NOT NULL DEFAULT '{}', "supplierOrder" jsonb NOT NULL DEFAULT '[]',
 "autoFallbackOrder" boolean NOT NULL DEFAULT false, "telegramChatId" text,
 "isActive" boolean NOT NULL DEFAULT true, "createdAt" timestamp NOT NULL DEFAULT now(),
 "updatedAt" timestamp NOT NULL DEFAULT now());
CREATE TABLE domae_order_batches (
 id text PRIMARY KEY, "monitorId" text NOT NULL REFERENCES domae_cloud_monitors(id),
 status text NOT NULL DEFAULT 'pending', "totalItems" integer NOT NULL,
 "successCount" integer NOT NULL DEFAULT 0, "failCount" integer NOT NULL DEFAULT 0,
 "adjustedCount" integer NOT NULL DEFAULT 0, "missingQuantity" integer NOT NULL DEFAULT 0,
 "createdAt" timestamp NOT NULL DEFAULT now(), "completedAt" timestamp);
CREATE TABLE domae_cloud_orders (
 id text PRIMARY KEY, "monitorId" text NOT NULL REFERENCES domae_cloud_monitors(id),
 "batchId" text REFERENCES domae_order_batches(id), supplier text NOT NULL,
 "productName" text NOT NULL, unit text, "insuranceCode" text, quantity integer NOT NULL,
 price integer, success boolean, "productId" text, "orderId" text, message text,
 "orderedAt" timestamp NOT NULL DEFAULT now(), "adjustedQuantity" integer,
 "availableStock" integer, "reasonCode" text, "confirmedQuantity" integer, "attemptKey" text UNIQUE);
CREATE TABLE domae_inventory_snapshots (
 id text PRIMARY KEY, "monitorId" text NOT NULL REFERENCES domae_cloud_monitors(id),
 supplier text NOT NULL, "productName" text NOT NULL, unit text, "insuranceCode" text,
 quantity integer, price integer, "productId" text, "scannedAt" timestamp NOT NULL DEFAULT now());
CREATE TABLE domae_urgent_orders (
 id text PRIMARY KEY, "monitorId" text NOT NULL REFERENCES domae_cloud_monitors(id),
 "productName" text NOT NULL, unit text, "insuranceCode" text, "totalQuantity" integer NOT NULL,
 "filledQuantity" integer NOT NULL DEFAULT 0, active boolean NOT NULL DEFAULT true,
 "createdAt" timestamp NOT NULL DEFAULT now(), "completedAt" timestamp,
 "checkRequired" boolean NOT NULL DEFAULT false, "checkReason" text, "sendingAt" timestamp,
 "sendingToken" text, "checkRevision" integer NOT NULL DEFAULT 0);
CREATE TABLE domae_urgent_suppliers (
 id text PRIMARY KEY, "urgentOrderId" text NOT NULL REFERENCES domae_urgent_orders(id) ON DELETE CASCADE,
 supplier text NOT NULL, "productId" text NOT NULL, price integer, position integer NOT NULL DEFAULT 0);
CREATE TABLE domae_urgent_logs (
 id text PRIMARY KEY, "urgentOrderId" text NOT NULL REFERENCES domae_urgent_orders(id) ON DELETE CASCADE,
 supplier text NOT NULL, "orderedQuantity" integer NOT NULL, success boolean NOT NULL,
 message text, "scannedAt" timestamp, "orderedAt" timestamp NOT NULL DEFAULT now());
'''


class UrgentDatabase:
    def __init__(self, dsn, schema):
        self.dsn = psycopg2.extensions.make_dsn(dsn, options=f'-c search_path={schema}')
        self.schema = schema

    def connect(self):
        return psycopg2.connect(self.dsn)

    @contextmanager
    def connection(self):
        with closing(self.connect()) as conn, conn:
            yield conn

    def pool(self):
        return ThreadedConnectionPool(1, 4, self.dsn)


@contextmanager
def isolated_database(dsn):
    admin = psycopg2.connect(validate_test_dsn(dsn))
    schema = 'urgent_' + uuid.uuid4().hex
    try:
        with admin.cursor() as cur:
            cur.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
            cur.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
            cur.execute(DDL)
        admin.commit()
        yield UrgentDatabase(dsn, schema)
    finally:
        admin.rollback()
        with admin.cursor() as cur:
            cur.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))
        admin.commit()
        admin.close()


@pytest.fixture
def urgent_database():
    dsn = os.environ.get('DOMAE_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('격리된 로컬 DB 필요')
    with isolated_database(dsn) as database:
        yield database


def seed_monitor(database, suppliers=('티제이팜', '백제')):
    mid = 'monitor1' + uuid.uuid4().hex
    credentials = {s: {'login_id': 'local-test', 'login_pw': 'local-test'} for s in suppliers}
    with database.connection() as conn, conn.cursor() as cur:
        cur.execute('''INSERT INTO domae_cloud_monitors
            (id,"apiKeyId",credentials,"supplierOrder","telegramChatId") VALUES (%s,%s,%s,%s,'c')''',
            (mid, 'api_' + mid, json.dumps(credentials), json.dumps(list(suppliers))))
    return mid


def seed_urgent(database, mid, total=10, filled=0, suppliers=(('인천', 'P1'),), **cols):
    uo = 'uo_' + uuid.uuid4().hex
    with database.connection() as conn, conn.cursor() as cur:
        cur.execute('''INSERT INTO domae_urgent_orders
            (id,"monitorId","productName","insuranceCode","totalQuantity","filledQuantity",active,
             "checkRequired","sendingAt","sendingToken","checkRevision")
            VALUES (%s,%s,'씨투스건조시럽/100g','645702221',%s,%s,%s,%s,%s,%s,%s)''',
            (uo, mid, total, filled, cols.get('active', True), cols.get('checkRequired', False),
             cols.get('sendingAt'), cols.get('sendingToken'), cols.get('checkRevision', 0)))
        for i, (supplier, pid) in enumerate(suppliers):
            cur.execute('''INSERT INTO domae_urgent_suppliers
                (id,"urgentOrderId",supplier,"productId",position) VALUES (%s,%s,%s,%s,%s)''',
                ('us_' + uuid.uuid4().hex, uo, supplier, pid, i))
    return uo


def read_urgent(database, uo):
    with database.connection() as conn, conn.cursor() as cur:
        cur.execute('''SELECT "filledQuantity",active,"checkRequired","sendingToken",
            "completedAt" IS NOT NULL FROM domae_urgent_orders WHERE id=%s''', (uo,))
        return cur.fetchone()
