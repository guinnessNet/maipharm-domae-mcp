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
  filterBar: {
    display: 'flex',
    gap: 8,
    alignItems: 'center',
    marginBottom: 16,
    flexWrap: 'wrap',
  },
  changePositive: {
    color: '#dc2626',
    fontWeight: 600,
  },
  changeNegative: {
    color: '#2563eb',
    fontWeight: 600,
  },
  changeStock: {
    color: '#16a34a',
    fontWeight: 600,
  },
  refreshInfo: {
    fontSize: 12,
    color: '#9ca3af',
    marginLeft: 'auto',
  },
};

const PERIOD_OPTIONS = [
  { label: '오늘', value: 1 },
  { label: '1주', value: 7 },
  { label: '1달', value: 30 },
  { label: '전체', value: 365 },
];

const TYPE_OPTIONS = [
  { label: '전체', value: '' },
  { label: '가격', value: 'price' },
  { label: '재고', value: 'stock' },
];

function formatValue(alertType, value) {
  if (value == null) return '-';
  if (alertType === 'price') {
    return value.toLocaleString('ko-KR') + '원';
  }
  return value.toLocaleString('ko-KR') + '개';
}

function formatChange(alertType, oldVal, newVal) {
  if (oldVal == null || newVal == null) return '-';
  const diff = newVal - oldVal;
  if (diff === 0) return '-';
  const prefix = diff > 0 ? '+' : '';
  if (alertType === 'price') {
    return prefix + diff.toLocaleString('ko-KR') + '원';
  }
  return prefix + diff.toLocaleString('ko-KR') + '개';
}

function getChangeStyle(alertType, oldVal, newVal) {
  if (alertType === 'stock') return styles.changeStock;
  if (oldVal == null || newVal == null) return {};
  if (newVal > oldVal) return styles.changePositive;
  if (newVal < oldVal) return styles.changeNegative;
  return {};
}

function getAlertBadge(alertType) {
  if (alertType === 'price') {
    return <span className="badge badge-price">가격</span>;
  }
  return <span className="badge badge-stock">재고</span>;
}

export default function MonitorAlertsPage() {
  const [alerts, setAlerts] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [alertType, setAlertType] = useState('');
  const [days, setDays] = useState(30);
  const intervalRef = useRef(null);

  const fetchAlerts = useCallback(async () => {
    try {
      setLoading(true);
      const params = { days, limit: 200 };
      if (alertType) params.alert_type = alertType;
      const res = await client.get('/monitor/alerts', { params });
      setAlerts(Array.isArray(res.data) ? res.data : []);
      setError('');
    } catch {
      setError('변동 이력을 불러올 수 없습니다.');
    } finally {
      setLoading(false);
    }
  }, [alertType, days]);

  useEffect(() => {
    fetchAlerts();
  }, [fetchAlerts]);

  // Auto-refresh every 60 seconds
  useEffect(() => {
    intervalRef.current = setInterval(() => {
      fetchAlerts();
    }, 60000);
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, [fetchAlerts]);

  return (
    <div style={styles.container}>
      <h1 style={styles.h1}>변동내역</h1>

      {error && <div className="error-box" style={{ marginBottom: '0.75rem' }}>{error}</div>}

      {/* Filters */}
      <div style={styles.filterBar}>
        {TYPE_OPTIONS.map((opt) => (
          <button
            key={opt.value}
            className={`btn-sm ${alertType === opt.value ? 'btn-primary' : ''}`}
            onClick={() => setAlertType(opt.value)}
          >
            {opt.label}
          </button>
        ))}
        <span style={{ width: 1, height: 20, background: '#e2e8f0', margin: '0 4px' }} />
        {PERIOD_OPTIONS.map((opt) => (
          <button
            key={opt.value}
            className={`btn-sm ${days === opt.value ? 'btn-primary' : ''}`}
            onClick={() => setDays(opt.value)}
          >
            {opt.label}
          </button>
        ))}
        <span style={styles.refreshInfo}>60초마다 자동 갱신</span>
      </div>

      {/* Table */}
      {loading ? (
        <div className="empty-state">불러오는 중...</div>
      ) : alerts.length === 0 ? (
        <div className="empty-state">
          조회 기간 내 변동 이력이 없습니다.<br />
          <span style={{ fontSize: 13, color: '#9ca3af' }}>
            모니터링이 실행 중일 때 가격/재고 변동이 감지되면 여기에 표시됩니다.
          </span>
        </div>
      ) : (
        <table>
          <thead>
            <tr>
              <th>시간</th>
              <th>제품명</th>
              <th>도매상</th>
              <th>유형</th>
              <th style={{ textAlign: 'right' }}>이전값</th>
              <th style={{ textAlign: 'right' }}>현재값</th>
              <th style={{ textAlign: 'right' }}>변동폭</th>
            </tr>
          </thead>
          <tbody>
            {alerts.map((a) => (
              <tr key={a.id}>
                <td className="text-secondary" style={{ whiteSpace: 'nowrap', fontSize: 13 }}>
                  {new Date(a.created_at).toLocaleString('ko-KR', {
                    month: '2-digit',
                    day: '2-digit',
                    hour: '2-digit',
                    minute: '2-digit',
                  })}
                </td>
                <td>{a.product_name}</td>
                <td>{a.supplier}</td>
                <td>{getAlertBadge(a.alert_type)}</td>
                <td style={{ textAlign: 'right' }}>
                  {formatValue(a.alert_type, a.old_value)}
                </td>
                <td style={{ textAlign: 'right' }}>
                  {formatValue(a.alert_type, a.new_value)}
                </td>
                <td style={{ textAlign: 'right', ...getChangeStyle(a.alert_type, a.old_value, a.new_value) }}>
                  {formatChange(a.alert_type, a.old_value, a.new_value)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
