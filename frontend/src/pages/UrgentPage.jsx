import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { useLocation } from 'react-router-dom';
import client from '../api/client';

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
  card: {
    border: '1px solid #e5e7eb',
    borderRadius: 8,
    padding: 16,
    marginBottom: 12,
    background: '#fff',
  },
  row: {
    display: 'flex',
    alignItems: 'center',
    gap: 12,
    flexWrap: 'wrap',
  },
  input: {
    padding: '8px 12px',
    borderRadius: 6,
    border: '1px solid #d1d5db',
    fontSize: 14,
    outline: 'none',
    width: '100%',
    boxSizing: 'border-box',
  },
  label: {
    display: 'block',
    fontSize: 13,
    fontWeight: 500,
    marginBottom: 4,
    color: '#374151',
  },
  fieldGroup: {
    marginBottom: 12,
  },
  grid2: {
    display: 'grid',
    gridTemplateColumns: '1fr 1fr',
    gap: 12,
  },
  grid3: {
    display: 'grid',
    gridTemplateColumns: '1fr 1fr 1fr',
    gap: 12,
  },
  supplierTag: {
    display: 'inline-block',
    padding: '2px 8px',
    borderRadius: 4,
    background: '#eff6ff',
    color: '#2563eb',
    fontSize: 12,
    marginRight: 4,
    marginBottom: 4,
  },
  error: {
    color: '#dc2626',
    fontSize: 14,
    padding: '8px 12px',
    background: '#fef2f2',
    borderRadius: 6,
    marginBottom: 12,
  },
};

const statusLabel = (order) => {
  if (!order.active && order.filled_quantity >= order.total_quantity) return '완료';
  if (!order.active) return '취소';
  return '활성';
};

const statusBadgeClass = (order) => {
  const s = statusLabel(order);
  if (s === '완료') return 'badge badge-success';
  if (s === '취소') return 'badge badge-muted';
  return 'badge badge-warning';
};

export default function UrgentPage() {
  const location = useLocation();
  const [orders, setOrders] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [expanded, setExpanded] = useState({});
  const [activeTab, setActiveTab] = useState('register'); // register | active | done

  // Form state
  const [form, setForm] = useState({
    product_name: '',
    unit: '',
    insurance_code: '',
    total_quantity: '',
  });
  const [suppliers, setSuppliers] = useState([{ supplier: '', product_id: '', price: '' }]);
  const [submitting, setSubmitting] = useState(false);

  const intervalRef = useRef(null);

  const fetchOrders = useCallback(async () => {
    try {
      setLoading(true);
      const res = await client.get('/urgent-orders');
      setOrders(res.data.orders || []);
      setError('');
    } catch (e) {
      setError('긴급주문 목록을 불러올 수 없습니다.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchOrders();
  }, [fetchOrders]);

  // Prefill from SearchPage navigation
  useEffect(() => {
    const prefill = location.state?.prefill;
    if (prefill) {
      setForm((prev) => ({
        ...prev,
        product_name: prefill.product_name || '',
        unit: prefill.unit || '',
        insurance_code: prefill.insurance_code || '',
      }));
      if (prefill.suppliers && prefill.suppliers.length > 0) {
        const s = prefill.suppliers[0];
        setSuppliers([{
          supplier: s.supplier || '',
          product_id: s.product_id || '',
          price: s.price != null ? String(s.price) : '',
        }]);
      }
      setActiveTab('register');
      // Clear the navigation state so refreshing doesn't re-prefill
      window.history.replaceState({}, '');
    }
  }, [location.state]);

  // Categorize orders
  const activeOrders = useMemo(() => orders.filter((o) => o.active), [orders]);
  const doneOrders = useMemo(
    () => orders.filter((o) => !o.active),
    [orders]
  );

  // Auto-refresh when there are active orders (30s)
  useEffect(() => {
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
    if (activeOrders.length > 0) {
      intervalRef.current = setInterval(() => {
        fetchOrders();
      }, 30000);
    }
    return () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
      }
    };
  }, [activeOrders.length, fetchOrders]);

  const toggleExpand = (id) => {
    setExpanded((prev) => ({ ...prev, [id]: !prev[id] }));
  };

  const handleCancel = async (id) => {
    try {
      await client.put(`/urgent-orders/${id}/cancel`);
      fetchOrders();
    } catch {
      setError('취소에 실패했습니다.');
    }
  };

  const handleReactivate = async (id) => {
    try {
      await client.put(`/urgent-orders/${id}/reactivate`);
      fetchOrders();
    } catch {
      setError('재활성화에 실패했습니다.');
    }
  };

  const handleDelete = async (id) => {
    if (!confirm('정말 삭제하시겠습니까?')) return;
    try {
      await client.delete(`/urgent-orders/${id}`);
      fetchOrders();
    } catch {
      setError('삭제에 실패했습니다.');
    }
  };

  const handleFormChange = (e) => {
    setForm((prev) => ({ ...prev, [e.target.name]: e.target.value }));
  };

  const handleSupplierChange = (idx, field, value) => {
    setSuppliers((prev) => prev.map((s, i) => (i === idx ? { ...s, [field]: value } : s)));
  };

  const addSupplier = () => {
    setSuppliers((prev) => [...prev, { supplier: '', product_id: '', price: '' }]);
  };

  const removeSupplier = (idx) => {
    setSuppliers((prev) => prev.filter((_, i) => i !== idx));
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!form.product_name || !form.total_quantity || suppliers.length === 0) {
      setError('제품명, 수량, 도매상 정보를 입력해주세요.');
      return;
    }
    try {
      setSubmitting(true);
      setError('');
      await client.post('/urgent-orders', {
        product_name: form.product_name,
        unit: form.unit,
        insurance_code: form.insurance_code || undefined,
        total_quantity: parseInt(form.total_quantity, 10),
        suppliers: suppliers
          .filter((s) => s.supplier && s.product_id)
          .map((s) => ({
            supplier: s.supplier,
            product_id: s.product_id,
            price: s.price ? parseInt(s.price, 10) : 0,
          })),
      });
      setForm({ product_name: '', unit: '', insurance_code: '', total_quantity: '' });
      setSuppliers([{ supplier: '', product_id: '', price: '' }]);
      fetchOrders();
      setActiveTab('active');
    } catch (e) {
      setError(e.response?.data?.message || '등록에 실패했습니다.');
    } finally {
      setSubmitting(false);
    }
  };

  const renderProgressBar = (order) => {
    const pct = order.total_quantity > 0
      ? Math.min((order.filled_quantity / order.total_quantity) * 100, 100)
      : 0;
    const isComplete = order.filled_quantity >= order.total_quantity;
    return (
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
        <span style={{ fontSize: 13, color: '#6b7280', whiteSpace: 'nowrap' }}>
          {order.filled_quantity} / {order.total_quantity}
        </span>
        <div className="progress">
          <div
            className={`progress-bar${isComplete ? ' complete' : ''}`}
            style={{ width: `${pct}%` }}
          />
        </div>
        <span style={{ fontSize: 12, color: '#6b7280', whiteSpace: 'nowrap' }}>
          {Math.round(pct)}%
        </span>
      </div>
    );
  };

  const renderOrderCard = (order) => {
    const isExpanded = expanded[order.id];
    return (
      <div key={order.id} style={styles.card}>
        <div style={{ ...styles.row, marginBottom: 8 }}>
          <strong style={{ fontSize: 15 }}>{order.product_name}</strong>
          {order.unit && (
            <span style={{ color: '#6b7280', fontSize: 13 }}>{order.unit}</span>
          )}
          <span className={statusBadgeClass(order)}>
            {statusLabel(order)}
          </span>
        </div>

        {renderProgressBar(order)}

        <div style={{ marginBottom: 8 }}>
          {(order.suppliers || []).map((s, i) => (
            <span key={i} style={styles.supplierTag}>
              {s.supplier}
              {s.price ? ` (${s.price.toLocaleString()}원)` : ''}
            </span>
          ))}
        </div>

        <div style={{ ...styles.row, gap: 8 }}>
          <button onClick={() => toggleExpand(order.id)}>
            {isExpanded ? '로그 접기' : '주문 로그'}
          </button>
          {order.active ? (
            <button onClick={() => handleCancel(order.id)}>
              취소
            </button>
          ) : (
            <button className="btn-primary" onClick={() => handleReactivate(order.id)}>
              재활성화
            </button>
          )}
          <button className="btn-danger" onClick={() => handleDelete(order.id)}>
            삭제
          </button>
        </div>

        {isExpanded && (
          <div style={{ marginTop: 12 }}>
            {(order.logs || []).length === 0 ? (
              <div style={{ color: '#9ca3af', fontSize: 13 }}>주문 로그가 없습니다.</div>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th>도매상</th>
                    <th>수량</th>
                    <th>성공</th>
                    <th>시간</th>
                  </tr>
                </thead>
                <tbody>
                  {order.logs.map((log, i) => (
                    <tr key={i}>
                      <td>{log.supplier}</td>
                      <td>{log.ordered_quantity}</td>
                      <td>
                        <span className={`badge ${log.success ? 'badge-success' : 'badge-error'}`}>
                          {log.success ? '성공' : '실패'}
                        </span>
                      </td>
                      <td>
                        {log.created_at
                          ? new Date(log.created_at).toLocaleString('ko-KR')
                          : '-'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
      </div>
    );
  };

  return (
    <div style={styles.container}>
      <h1 style={styles.h1}>긴급주문</h1>

      {error && <div style={styles.error}>{error}</div>}

      {/* Tabs */}
      <div className="tabs">
        <button
          className={`tab-btn${activeTab === 'register' ? ' active' : ''}`}
          onClick={() => setActiveTab('register')}
        >
          등록
        </button>
        <button
          className={`tab-btn${activeTab === 'active' ? ' active' : ''}`}
          onClick={() => setActiveTab('active')}
        >
          활성 주문 {activeOrders.length > 0 && `(${activeOrders.length})`}
        </button>
        <button
          className={`tab-btn${activeTab === 'done' ? ' active' : ''}`}
          onClick={() => setActiveTab('done')}
        >
          완료/취소 {doneOrders.length > 0 && `(${doneOrders.length})`}
        </button>
      </div>

      {/* Register Tab */}
      {activeTab === 'register' && (
        <form onSubmit={handleSubmit} style={styles.card}>
          <div style={styles.grid2}>
            <div style={styles.fieldGroup}>
              <label style={styles.label}>제품명 *</label>
              <input
                style={styles.input}
                name="product_name"
                value={form.product_name}
                onChange={handleFormChange}
                placeholder="예: 아모잘탄정 5/100mg"
              />
            </div>
            <div style={styles.fieldGroup}>
              <label style={styles.label}>단위</label>
              <input
                style={styles.input}
                name="unit"
                value={form.unit}
                onChange={handleFormChange}
                placeholder="예: 30T"
              />
            </div>
          </div>
          <div style={styles.grid2}>
            <div style={styles.fieldGroup}>
              <label style={styles.label}>보험코드</label>
              <input
                style={styles.input}
                name="insurance_code"
                value={form.insurance_code}
                onChange={handleFormChange}
                placeholder="예: 655801440"
              />
            </div>
            <div style={styles.fieldGroup}>
              <label style={styles.label}>필요수량 *</label>
              <input
                style={styles.input}
                name="total_quantity"
                type="number"
                min="1"
                value={form.total_quantity}
                onChange={handleFormChange}
                placeholder="10"
              />
            </div>
          </div>

          <div style={{ marginBottom: 12 }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
              <label style={{ ...styles.label, marginBottom: 0 }}>도매상 목록</label>
              <button type="button" onClick={addSupplier}>
                + 도매상 추가
              </button>
            </div>
            {suppliers.map((s, idx) => (
              <div key={idx} style={{ ...styles.grid3, marginBottom: 8, alignItems: 'end' }}>
                <div>
                  {idx === 0 && <label style={styles.label}>도매상명</label>}
                  <input
                    style={styles.input}
                    value={s.supplier}
                    onChange={(e) => handleSupplierChange(idx, 'supplier', e.target.value)}
                    placeholder="지오영"
                  />
                </div>
                <div>
                  {idx === 0 && <label style={styles.label}>제품코드</label>}
                  <input
                    style={styles.input}
                    value={s.product_id}
                    onChange={(e) => handleSupplierChange(idx, 'product_id', e.target.value)}
                    placeholder="GEO-12345"
                  />
                </div>
                <div style={{ display: 'flex', gap: 8, alignItems: 'end' }}>
                  <div style={{ flex: 1 }}>
                    {idx === 0 && <label style={styles.label}>가격</label>}
                    <input
                      style={styles.input}
                      type="number"
                      value={s.price}
                      onChange={(e) => handleSupplierChange(idx, 'price', e.target.value)}
                      placeholder="12500"
                    />
                  </div>
                  {suppliers.length > 1 && (
                    <button
                      type="button"
                      className="btn-danger btn-sm"
                      onClick={() => removeSupplier(idx)}
                    >
                      삭제
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>

          <button type="submit" className="btn-primary" disabled={submitting}>
            {submitting ? '등록 중...' : '긴급주문 등록'}
          </button>
        </form>
      )}

      {/* Active Orders Tab */}
      {activeTab === 'active' && (
        <div>
          {loading ? (
            <div className="empty-state">불러오는 중...</div>
          ) : activeOrders.length === 0 ? (
            <div className="empty-state">활성 긴급주문이 없습니다.</div>
          ) : (
            <>
              {activeOrders.length > 0 && (
                <div style={{ fontSize: 13, color: '#6b7280', marginBottom: 12 }}>
                  30초마다 자동 갱신됩니다.
                </div>
              )}
              {activeOrders.map(renderOrderCard)}
            </>
          )}
        </div>
      )}

      {/* Done/Cancelled Tab */}
      {activeTab === 'done' && (
        <div>
          {loading ? (
            <div className="empty-state">불러오는 중...</div>
          ) : doneOrders.length === 0 ? (
            <div className="empty-state">완료/취소된 주문이 없습니다.</div>
          ) : (
            doneOrders.map(renderOrderCard)
          )}
        </div>
      )}
    </div>
  );
}
