import { useState, useEffect, useCallback, useRef } from 'react';
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
  statusText: {
    fontSize: 14,
    color: '#374151',
    flex: 1,
  },
  addForm: {
    display: 'flex',
    gap: 8,
    alignItems: 'center',
    marginBottom: 16,
    flexWrap: 'wrap',
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
  const pollRef = useRef(null);

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

  // Auto-poll monitor status when running (15s)
  useEffect(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    if (monitorStatus.running) {
      pollRef.current = setInterval(() => {
        fetchMonitorStatus();
      }, 15000);
    }
    return () => {
      if (pollRef.current) {
        clearInterval(pollRef.current);
      }
    };
  }, [monitorStatus.running, fetchMonitorStatus]);

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

      {error && <div className="error-box" style={{ marginBottom: '0.75rem' }}>{error}</div>}
      {message && <div className="success-box" style={{ marginBottom: '0.75rem' }}>{message}</div>}

      {/* Monitor Status */}
      <div style={styles.statusBar}>
        <span className={`status-dot ${monitorStatus.running ? 'status-dot-active' : 'status-dot-inactive'}`} />
        <span style={styles.statusText}>
          모니터링 {monitorStatus.running ? '실행 중' : '중지됨'}
          {monitorStatus.running && (
            <span className="text-secondary" style={{ marginLeft: 8, fontSize: 12 }}>
              (15초마다 상태 갱신)
            </span>
          )}
          {monitorStatus.last_run && (
            <span style={{ color: '#9ca3af', marginLeft: 12, fontSize: 13 }}>
              마지막 실행: {new Date(monitorStatus.last_run).toLocaleString('ko-KR')}
            </span>
          )}
        </span>
        {monitorStatus.running ? (
          <button
            className="btn-danger"
            onClick={handleMonitorStop}
            disabled={monitorLoading}
          >
            {monitorLoading ? '처리 중...' : '모니터링 중지'}
          </button>
        ) : (
          <button
            className="btn-success"
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
            type="text"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder="보험코드 또는 제품명 입력"
            style={{ flex: 1, minWidth: 200 }}
          />
          <button type="submit" className="btn-primary" disabled={adding}>
            {adding ? '추가 중...' : '추가'}
          </button>
        </form>
      </div>

      {/* Product List */}
      <div style={styles.section}>
        <h2 style={styles.h2}>제품 목록</h2>
        {loading ? (
          <div className="empty-state">불러오는 중...</div>
        ) : products.length === 0 ? (
          <div className="empty-state">등록된 모니터링 제품이 없습니다.</div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>ID</th>
                <th>제품명 / 보험코드</th>
                <th>설명</th>
                <th style={{ textAlign: 'center' }}>관리</th>
              </tr>
            </thead>
            <tbody>
              {products.map((product) => (
                <tr key={product.id}>
                  <td className="text-secondary" style={{ width: 60 }}>{product.id}</td>
                  <td>{product.name}</td>
                  <td className="text-secondary">
                    {product.description || '-'}
                  </td>
                  <td style={{ textAlign: 'center', width: 80 }}>
                    <button
                      className="btn-danger btn-sm"
                      onClick={() => handleDelete(product.id)}
                    >
                      삭제
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
