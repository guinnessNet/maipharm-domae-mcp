import { useState, useEffect, useCallback } from 'react';
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
  h2: {
    fontSize: 18,
    fontWeight: 600,
    marginBottom: 16,
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
  badge: (active, filled) => ({
    display: 'inline-block',
    padding: '2px 10px',
    borderRadius: 12,
    fontSize: 12,
    fontWeight: 600,
    color: '#fff',
    background: !active ? '#6b7280' : filled ? '#16a34a' : '#2563eb',
  }),
  btn: (color = '#2563eb') => ({
    padding: '6px 14px',
    borderRadius: 6,
    border: 'none',
    background: color,
    color: '#fff',
    fontSize: 13,
    fontWeight: 500,
    cursor: 'pointer',
  }),
  btnOutline: {
    padding: '6px 14px',
    borderRadius: 6,
    border: '1px solid #d1d5db',
    background: '#fff',
    color: '#374151',
    fontSize: 13,
    cursor: 'pointer',
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
  logTable: {
    width: '100%',
    borderCollapse: 'collapse',
    fontSize: 13,
    marginTop: 8,
  },
  th: {
    textAlign: 'left',
    padding: '6px 8px',
    borderBottom: '2px solid #e5e7eb',
    color: '#6b7280',
    fontWeight: 600,
    fontSize: 12,
  },
  td: {
    padding: '6px 8px',
    borderBottom: '1px solid #f3f4f6',
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
  section: {
    marginBottom: 32,
  },
  progress: (filled, total) => ({
    height: 6,
    borderRadius: 3,
    background: '#e5e7eb',
    flex: 1,
    position: 'relative',
    overflow: 'hidden',
  }),
  progressBar: (filled, total) => ({
    height: '100%',
    borderRadius: 3,
    background: filled >= total ? '#16a34a' : '#2563eb',
    width: `${total > 0 ? Math.min((filled / total) * 100, 100) : 0}%`,
    transition: 'width 0.3s',
  }),
  error: {
    color: '#dc2626',
    fontSize: 14,
    padding: '8px 12px',
    background: '#fef2f2',
    borderRadius: 6,
    marginBottom: 12,
  },
  empty: {
    textAlign: 'center',
    color: '#9ca3af',
    padding: 40,
    fontSize: 14,
  },
};

const statusLabel = (order) => {
  if (!order.active && order.filled_quantity >= order.total_quantity) return '완료';
  if (!order.active) return '취소';
  return '활성';
};

export default function UrgentPage() {
  const [orders, setOrders] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [expanded, setExpanded] = useState({});

  // Form state
  const [form, setForm] = useState({
    product_name: '',
    unit: '',
    insurance_code: '',
    total_quantity: '',
  });
  const [suppliers, setSuppliers] = useState([{ supplier: '', product_id: '', price: '' }]);
  const [submitting, setSubmitting] = useState(false);

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
    } catch (e) {
      setError(e.response?.data?.message || '등록에 실패했습니다.');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div style={styles.container}>
      <h1 style={styles.h1}>긴급주문</h1>

      {error && <div style={styles.error}>{error}</div>}

      {/* Registration Form */}
      <div style={{ ...styles.section }}>
        <h2 style={styles.h2}>긴급주문 등록</h2>
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
              <button type="button" style={styles.btnOutline} onClick={addSupplier}>
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
                      style={{ ...styles.btn('#dc2626'), flexShrink: 0 }}
                      onClick={() => removeSupplier(idx)}
                    >
                      삭제
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>

          <button type="submit" style={styles.btn('#2563eb')} disabled={submitting}>
            {submitting ? '등록 중...' : '긴급주문 등록'}
          </button>
        </form>
      </div>

      {/* Order List */}
      <div style={styles.section}>
        <h2 style={styles.h2}>긴급주문 목록</h2>
        {loading ? (
          <div style={styles.empty}>불러오는 중...</div>
        ) : orders.length === 0 ? (
          <div style={styles.empty}>등록된 긴급주문이 없습니다.</div>
        ) : (
          orders.map((order) => {
            const status = statusLabel(order);
            const isExpanded = expanded[order.id];
            return (
              <div key={order.id} style={styles.card}>
                <div style={{ ...styles.row, marginBottom: 8 }}>
                  <strong style={{ fontSize: 15 }}>{order.product_name}</strong>
                  {order.unit && (
                    <span style={{ color: '#6b7280', fontSize: 13 }}>{order.unit}</span>
                  )}
                  <span
                    style={styles.badge(
                      order.active,
                      order.filled_quantity >= order.total_quantity
                    )}
                  >
                    {status}
                  </span>
                </div>

                <div style={{ ...styles.row, marginBottom: 8 }}>
                  <span style={{ fontSize: 13, color: '#6b7280' }}>
                    {order.filled_quantity} / {order.total_quantity}
                  </span>
                  <div style={styles.progress(order.filled_quantity, order.total_quantity)}>
                    <div
                      style={styles.progressBar(order.filled_quantity, order.total_quantity)}
                    />
                  </div>
                </div>

                <div style={{ marginBottom: 8 }}>
                  {(order.suppliers || []).map((s, i) => (
                    <span key={i} style={styles.supplierTag}>
                      {s.supplier}
                      {s.price ? ` (${s.price.toLocaleString()}원)` : ''}
                    </span>
                  ))}
                </div>

                <div style={{ ...styles.row, gap: 8 }}>
                  <button style={styles.btnOutline} onClick={() => toggleExpand(order.id)}>
                    {isExpanded ? '로그 접기' : '주문 로그'}
                  </button>
                  {order.active ? (
                    <button
                      style={styles.btn('#6b7280')}
                      onClick={() => handleCancel(order.id)}
                    >
                      취소
                    </button>
                  ) : (
                    <button
                      style={styles.btn('#2563eb')}
                      onClick={() => handleReactivate(order.id)}
                    >
                      재활성화
                    </button>
                  )}
                  <button
                    style={styles.btn('#dc2626')}
                    onClick={() => handleDelete(order.id)}
                  >
                    삭제
                  </button>
                </div>

                {isExpanded && (
                  <div style={{ marginTop: 12 }}>
                    {(order.logs || []).length === 0 ? (
                      <div style={{ color: '#9ca3af', fontSize: 13 }}>주문 로그가 없습니다.</div>
                    ) : (
                      <table style={styles.logTable}>
                        <thead>
                          <tr>
                            <th style={styles.th}>도매상</th>
                            <th style={styles.th}>수량</th>
                            <th style={styles.th}>성공</th>
                            <th style={styles.th}>시간</th>
                          </tr>
                        </thead>
                        <tbody>
                          {order.logs.map((log, i) => (
                            <tr key={i}>
                              <td style={styles.td}>{log.supplier}</td>
                              <td style={styles.td}>{log.ordered_quantity}</td>
                              <td style={styles.td}>
                                <span
                                  style={{
                                    color: log.success ? '#16a34a' : '#dc2626',
                                    fontWeight: 600,
                                  }}
                                >
                                  {log.success ? '성공' : '실패'}
                                </span>
                              </td>
                              <td style={styles.td}>
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
          })
        )}
      </div>
    </div>
  );
}
