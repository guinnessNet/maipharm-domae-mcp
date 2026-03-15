import { useState, useEffect, useCallback } from 'react';
import client from '../api/client';

const PAGE_SIZE = 50;

const styles = {
  container: {
    maxWidth: 960,
    margin: '0 auto',
    padding: '24px 16px',
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
  },
  h1: {
    fontSize: 24,
    fontWeight: 700,
    marginBottom: 24,
    color: '#111',
  },
  tableWrap: {
    overflowX: 'auto',
    border: '1px solid #e5e7eb',
    borderRadius: 8,
    background: '#fff',
  },
  table: {
    width: '100%',
    borderCollapse: 'collapse',
    fontSize: 14,
    minWidth: 700,
  },
  th: {
    textAlign: 'left',
    padding: '10px 12px',
    borderBottom: '2px solid #e5e7eb',
    background: '#f9fafb',
    color: '#6b7280',
    fontWeight: 600,
    fontSize: 12,
    whiteSpace: 'nowrap',
  },
  td: {
    padding: '10px 12px',
    borderBottom: '1px solid #f3f4f6',
    color: '#111',
  },
  badge: (success) => ({
    display: 'inline-block',
    padding: '2px 10px',
    borderRadius: 12,
    fontSize: 12,
    fontWeight: 600,
    color: '#fff',
    background: success ? '#16a34a' : '#dc2626',
  }),
  pagination: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    gap: 16,
    marginTop: 16,
  },
  btn: (disabled) => ({
    padding: '8px 18px',
    borderRadius: 6,
    border: '1px solid #d1d5db',
    background: disabled ? '#f3f4f6' : '#fff',
    color: disabled ? '#9ca3af' : '#374151',
    fontSize: 14,
    cursor: disabled ? 'default' : 'pointer',
    fontWeight: 500,
  }),
  pageInfo: {
    fontSize: 14,
    color: '#6b7280',
  },
  empty: {
    textAlign: 'center',
    color: '#9ca3af',
    padding: 40,
    fontSize: 14,
  },
  error: {
    color: '#dc2626',
    fontSize: 14,
    padding: '8px 12px',
    background: '#fef2f2',
    borderRadius: 6,
    marginBottom: 12,
  },
  message: {
    maxWidth: 200,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
  },
};

export default function HistoryPage() {
  const [orders, setOrders] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);

  const fetchOrders = useCallback(async (currentOffset) => {
    try {
      setLoading(true);
      setError('');
      const res = await client.get('/orders', {
        params: { limit: PAGE_SIZE, offset: currentOffset },
      });
      const data = res.data;
      // API may return {orders: [...]} or just an array
      const list = Array.isArray(data) ? data : data.orders || [];
      setOrders(list);
      setHasMore(list.length >= PAGE_SIZE);
    } catch {
      setError('주문이력을 불러올 수 없습니다.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchOrders(offset);
  }, [offset, fetchOrders]);

  const page = Math.floor(offset / PAGE_SIZE) + 1;

  return (
    <div style={styles.container}>
      <h1 style={styles.h1}>주문이력</h1>

      {error && <div style={styles.error}>{error}</div>}

      {loading ? (
        <div style={styles.empty}>불러오는 중...</div>
      ) : orders.length === 0 ? (
        <div style={styles.empty}>주문 이력이 없습니다.</div>
      ) : (
        <>
          <div style={styles.tableWrap}>
            <table style={styles.table}>
              <thead>
                <tr>
                  <th style={styles.th}>날짜</th>
                  <th style={styles.th}>도매상</th>
                  <th style={styles.th}>제품명</th>
                  <th style={styles.th}>단위</th>
                  <th style={styles.th}>수량</th>
                  <th style={styles.th}>가격</th>
                  <th style={styles.th}>결과</th>
                  <th style={styles.th}>메시지</th>
                </tr>
              </thead>
              <tbody>
                {orders.map((order, idx) => (
                  <tr key={order.id || idx}>
                    <td style={{ ...styles.td, whiteSpace: 'nowrap', fontSize: 13 }}>
                      {order.created_at
                        ? new Date(order.created_at).toLocaleString('ko-KR')
                        : '-'}
                    </td>
                    <td style={styles.td}>{order.supplier || '-'}</td>
                    <td style={styles.td}>{order.product_name || '-'}</td>
                    <td style={styles.td}>{order.unit || '-'}</td>
                    <td style={styles.td}>{order.quantity ?? '-'}</td>
                    <td style={styles.td}>
                      {order.price != null ? order.price.toLocaleString() + '원' : '-'}
                    </td>
                    <td style={styles.td}>
                      <span style={styles.badge(order.success)}>
                        {order.success ? '성공' : '실패'}
                      </span>
                    </td>
                    <td style={{ ...styles.td, ...styles.message }} title={order.message || ''}>
                      {order.message || '-'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div style={styles.pagination}>
            <button
              style={styles.btn(offset === 0)}
              disabled={offset === 0}
              onClick={() => setOffset((prev) => Math.max(0, prev - PAGE_SIZE))}
            >
              이전
            </button>
            <span style={styles.pageInfo}>페이지 {page}</span>
            <button
              style={styles.btn(!hasMore)}
              disabled={!hasMore}
              onClick={() => setOffset((prev) => prev + PAGE_SIZE)}
            >
              다음
            </button>
          </div>
        </>
      )}
    </div>
  );
}
