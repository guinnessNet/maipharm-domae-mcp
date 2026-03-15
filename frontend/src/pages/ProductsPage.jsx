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
    marginBottom: 12,
    color: '#111',
  },
  section: {
    marginBottom: 32,
  },
  card: {
    border: '1px solid #e5e7eb',
    borderRadius: 8,
    padding: 16,
    background: '#fff',
    marginBottom: 12,
  },
  statusBar: {
    display: 'flex',
    alignItems: 'center',
    gap: 12,
    padding: '12px 16px',
    borderRadius: 8,
    border: '1px solid #e5e7eb',
    background: '#fff',
    marginBottom: 24,
    flexWrap: 'wrap',
  },
  statusDot: (running) => ({
    width: 10,
    height: 10,
    borderRadius: '50%',
    background: running ? '#16a34a' : '#9ca3af',
    flexShrink: 0,
  }),
  statusText: {
    fontSize: 14,
    color: '#374151',
    flex: 1,
  },
  btn: (color = '#2563eb') => ({
    padding: '8px 16px',
    borderRadius: 6,
    border: 'none',
    background: color,
    color: '#fff',
    fontSize: 14,
    fontWeight: 500,
    cursor: 'pointer',
  }),
  btnSmall: (color = '#dc2626') => ({
    padding: '4px 12px',
    borderRadius: 6,
    border: 'none',
    background: color,
    color: '#fff',
    fontSize: 12,
    fontWeight: 500,
    cursor: 'pointer',
  }),
  input: {
    padding: '8px 12px',
    borderRadius: 6,
    border: '1px solid #d1d5db',
    fontSize: 14,
    outline: 'none',
    boxSizing: 'border-box',
  },
  addForm: {
    display: 'flex',
    gap: 8,
    alignItems: 'center',
    marginBottom: 16,
    flexWrap: 'wrap',
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
  },
  th: {
    textAlign: 'left',
    padding: '10px 12px',
    borderBottom: '2px solid #e5e7eb',
    background: '#f9fafb',
    color: '#6b7280',
    fontWeight: 600,
    fontSize: 12,
  },
  td: {
    padding: '10px 12px',
    borderBottom: '1px solid #f3f4f6',
    color: '#111',
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
  success: {
    color: '#16a34a',
    fontSize: 14,
    padding: '8px 12px',
    background: '#f0fdf4',
    borderRadius: 6,
    marginBottom: 12,
  },
};

export default function ProductsPage() {
  const [products, setProducts] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [newName, setNewName] = useState('');
  const [adding, setAdding] = useState(false);

  // Monitor status
  const [monitorStatus, setMonitorStatus] = useState({ running: false, last_run: null });
  const [monitorLoading, setMonitorLoading] = useState(false);

  const fetchProducts = useCallback(async () => {
    try {
      setLoading(true);
      const res = await client.get('/products');
      const data = res.data;
      setProducts(Array.isArray(data) ? data : data.products || []);
      setError('');
    } catch {
      setError('제품 목록을 불러올 수 없습니다.');
    } finally {
      setLoading(false);
    }
  }, []);

  const fetchMonitorStatus = useCallback(async () => {
    try {
      const res = await client.get('/monitor/status');
      setMonitorStatus(res.data);
    } catch {
      // silent
    }
  }, []);

  useEffect(() => {
    fetchProducts();
    fetchMonitorStatus();
  }, [fetchProducts, fetchMonitorStatus]);

  const handleAdd = async (e) => {
    e.preventDefault();
    if (!newName.trim()) return;
    try {
      setAdding(true);
      setError('');
      setMessage('');
      await client.post('/products', { name: newName.trim(), description: '' });
      setNewName('');
      setMessage('제품이 추가되었습니다.');
      fetchProducts();
    } catch (e) {
      setError(e.response?.data?.message || '제품 추가에 실패했습니다.');
    } finally {
      setAdding(false);
    }
  };

  const handleDelete = async (id) => {
    if (!confirm('정말 삭제하시겠습니까?')) return;
    try {
      setError('');
      setMessage('');
      await client.delete(`/products/${id}`);
      setMessage('제품이 삭제되었습니다.');
      fetchProducts();
    } catch {
      setError('삭제에 실패했습니다.');
    }
  };

  const handleMonitorStart = async () => {
    try {
      setMonitorLoading(true);
      setError('');
      setMessage('');
      await client.post('/monitor/start');
      setMessage('모니터링이 시작되었습니다.');
      fetchMonitorStatus();
    } catch {
      setError('모니터링 시작에 실패했습니다.');
    } finally {
      setMonitorLoading(false);
    }
  };

  const handleMonitorStop = async () => {
    try {
      setMonitorLoading(true);
      setError('');
      setMessage('');
      await client.post('/monitor/stop');
      setMessage('모니터링이 중지되었습니다.');
      fetchMonitorStatus();
    } catch {
      setError('모니터링 중지에 실패했습니다.');
    } finally {
      setMonitorLoading(false);
    }
  };

  return (
    <div style={styles.container}>
      <h1 style={styles.h1}>모니터링 제품 관리</h1>

      {error && <div style={styles.error}>{error}</div>}
      {message && <div style={styles.success}>{message}</div>}

      {/* Monitor Status */}
      <div style={styles.statusBar}>
        <div style={styles.statusDot(monitorStatus.running)} />
        <span style={styles.statusText}>
          모니터링 {monitorStatus.running ? '실행 중' : '중지됨'}
          {monitorStatus.last_run && (
            <span style={{ color: '#9ca3af', marginLeft: 12, fontSize: 13 }}>
              마지막 실행: {new Date(monitorStatus.last_run).toLocaleString('ko-KR')}
            </span>
          )}
        </span>
        {monitorStatus.running ? (
          <button
            style={styles.btn('#dc2626')}
            onClick={handleMonitorStop}
            disabled={monitorLoading}
          >
            {monitorLoading ? '처리 중...' : '모니터링 중지'}
          </button>
        ) : (
          <button
            style={styles.btn('#16a34a')}
            onClick={handleMonitorStart}
            disabled={monitorLoading}
          >
            {monitorLoading ? '처리 중...' : '모니터링 시작'}
          </button>
        )}
      </div>

      {/* Add Product */}
      <div style={styles.section}>
        <h2 style={styles.h2}>제품 추가</h2>
        <form onSubmit={handleAdd} style={styles.addForm}>
          <input
            style={{ ...styles.input, flex: 1, minWidth: 200 }}
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder="보험코드 또는 제품명 입력"
          />
          <button type="submit" style={styles.btn('#2563eb')} disabled={adding}>
            {adding ? '추가 중...' : '추가'}
          </button>
        </form>
      </div>

      {/* Product List */}
      <div style={styles.section}>
        <h2 style={styles.h2}>제품 목록</h2>
        {loading ? (
          <div style={styles.empty}>불러오는 중...</div>
        ) : products.length === 0 ? (
          <div style={styles.empty}>등록된 모니터링 제품이 없습니다.</div>
        ) : (
          <div style={styles.tableWrap}>
            <table style={styles.table}>
              <thead>
                <tr>
                  <th style={styles.th}>ID</th>
                  <th style={styles.th}>제품명 / 보험코드</th>
                  <th style={styles.th}>설명</th>
                  <th style={{ ...styles.th, textAlign: 'center' }}>관리</th>
                </tr>
              </thead>
              <tbody>
                {products.map((product) => (
                  <tr key={product.id}>
                    <td style={{ ...styles.td, color: '#9ca3af', width: 60 }}>{product.id}</td>
                    <td style={styles.td}>{product.name}</td>
                    <td style={{ ...styles.td, color: '#6b7280' }}>
                      {product.description || '-'}
                    </td>
                    <td style={{ ...styles.td, textAlign: 'center', width: 80 }}>
                      <button
                        style={styles.btnSmall('#dc2626')}
                        onClick={() => handleDelete(product.id)}
                      >
                        삭제
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
