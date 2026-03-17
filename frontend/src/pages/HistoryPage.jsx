import { useState, useEffect, useCallback, useMemo } from 'react';
import client from '../api/client';
import { addToCart } from '../utils/cart.js';

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
  pagination: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    gap: 16,
    marginTop: 16,
  },
  pageInfo: {
    fontSize: 14,
    color: '#6b7280',
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

  const [toast, setToast] = useState('');

  // Client-side filters
  const [filterSupplier, setFilterSupplier] = useState('');
  const [filterResult, setFilterResult] = useState('all'); // all | success | fail
  const [filterKeyword, setFilterKeyword] = useState('');

  const handleReorder = (order) => {
    addToCart({
      supplier: order.supplier,
      productName: order.product_name,
      unit: order.unit,
      productId: order.product_id,
      price: order.price,
      quantity: order.quantity || 1,
    });
    window.dispatchEvent(new CustomEvent('cart-updated'));
    setToast('장바구니에 추가됨');
    setTimeout(() => setToast(''), 2000);
  };

  const fetchOrders = useCallback(async (currentOffset) => {
    try {
      setLoading(true);
      setError('');
      const res = await client.get('/orders', {
        params: { limit: PAGE_SIZE, offset: currentOffset },
      });
      const data = res.data;
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

  // Unique supplier names for dropdown
  const supplierOptions = useMemo(() => {
    const set = new Set(orders.map((o) => o.supplier).filter(Boolean));
    return Array.from(set).sort();
  }, [orders]);

  // Filtered orders
  const filteredOrders = useMemo(() => {
    return orders.filter((order) => {
      if (filterSupplier && order.supplier !== filterSupplier) return false;
      if (filterResult === 'success' && !order.success) return false;
      if (filterResult === 'fail' && order.success) return false;
      if (filterKeyword) {
        const kw = filterKeyword.toLowerCase();
        const name = (order.product_name || '').toLowerCase();
        if (!name.includes(kw)) return false;
      }
      return true;
    });
  }, [orders, filterSupplier, filterResult, filterKeyword]);

  const page = Math.floor(offset / PAGE_SIZE) + 1;

  return (
    <div style={styles.container}>
      <h1 style={styles.h1}>주문이력</h1>

      {error && <div className="error-box" style={{ marginBottom: '0.75rem' }}>{error}</div>}

      {loading ? (
        <div className="empty-state">불러오는 중...</div>
      ) : orders.length === 0 ? (
        <div className="empty-state">주문 이력이 없습니다.</div>
      ) : (
        <>
          {/* Filter Bar */}
          <div className="filter-bar">
            <select
              value={filterSupplier}
              onChange={(e) => setFilterSupplier(e.target.value)}
            >
              <option value="">전체 도매상</option>
              {supplierOptions.map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>

            <button
              className={filterResult === 'all' ? 'btn-primary btn-sm' : 'btn-sm'}
              onClick={() => setFilterResult('all')}
            >
              전체
            </button>
            <button
              className={filterResult === 'success' ? 'btn-success btn-sm' : 'btn-sm'}
              onClick={() => setFilterResult('success')}
            >
              성공
            </button>
            <button
              className={filterResult === 'fail' ? 'btn-danger btn-sm' : 'btn-sm'}
              onClick={() => setFilterResult('fail')}
            >
              실패
            </button>

            <input
              type="text"
              value={filterKeyword}
              onChange={(e) => setFilterKeyword(e.target.value)}
              placeholder="제품명 검색"
              style={{ minWidth: 140 }}
            />

            <span className="text-secondary" style={{ fontSize: '0.85rem', marginLeft: 'auto' }}>
              전체 {filteredOrders.length}건
            </span>
          </div>

          <div className="table-wrapper"><table>
            <thead>
              <tr>
                <th>날짜</th>
                <th>도매상</th>
                <th>제품명</th>
                <th>단위</th>
                <th>수량</th>
                <th>가격</th>
                <th>결과</th>
                <th>메시지</th>
                <th>재주문</th>
              </tr>
            </thead>
            <tbody>
              {filteredOrders.map((order, idx) => (
                <tr key={order.id || idx}>
                  <td style={{ whiteSpace: 'nowrap', fontSize: '0.8rem' }}>
                    {order.ordered_at
                      ? new Date(order.ordered_at).toLocaleString('ko-KR')
                      : '-'}
                  </td>
                  <td>{order.supplier || '-'}</td>
                  <td>{order.product_name || '-'}</td>
                  <td>{order.unit || '-'}</td>
                  <td>{order.quantity ?? '-'}</td>
                  <td>
                    {order.price != null ? order.price.toLocaleString() + '원' : '-'}
                  </td>
                  <td>
                    <span className={`badge ${order.success ? 'badge-success' : 'badge-error'}`}>
                      {order.success ? '성공' : '실패'}
                    </span>
                  </td>
                  <td style={styles.message} title={order.message || ''}>
                    {order.message || '-'}
                  </td>
                  <td>
                    {order.success && order.product_id ? (
                      <button
                        className="btn-reorder btn-sm"
                        onClick={() => handleReorder(order)}
                      >
                        재주문
                      </button>
                    ) : (
                      <span className="text-secondary" style={{ fontSize: '0.8rem' }}>
                        {order.success ? '검색 필요' : '-'}
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table></div>

          <div style={styles.pagination}>
            <button
              disabled={offset === 0}
              onClick={() => setOffset((prev) => Math.max(0, prev - PAGE_SIZE))}
            >
              이전
            </button>
            <span style={styles.pageInfo}>페이지 {page}</span>
            <button
              disabled={!hasMore}
              onClick={() => setOffset((prev) => prev + PAGE_SIZE)}
            >
              다음
            </button>
          </div>
        </>
      )}

      {toast && <div className="cart-toast">{toast}</div>}
    </div>
  );
}
