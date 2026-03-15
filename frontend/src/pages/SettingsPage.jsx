import { useState, useEffect, useCallback } from 'react';
import client from '../api/client';

const TABS = [
  { key: 'apikey', label: 'API 키' },
  { key: 'credentials', label: '도매 계정' },
  { key: 'telegram', label: '텔레그램' },
  { key: 'schedule', label: '스케줄' },
];

export default function SettingsPage() {
  const [activeTab, setActiveTab] = useState('apikey');

  return (
    <div>
      <div className="tabs">
        {TABS.map((tab) => (
          <button
            key={tab.key}
            className={`tab-btn ${activeTab === tab.key ? 'active' : ''}`}
            onClick={() => setActiveTab(tab.key)}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {activeTab === 'apikey' && <ApiKeyTab />}
      {activeTab === 'credentials' && <CredentialsTab />}
      {activeTab === 'telegram' && <TelegramTab />}
      {activeTab === 'schedule' && <ScheduleTab />}
    </div>
  );
}

/* ---------- API Key Tab ---------- */
function ApiKeyTab() {
  const [apiKey, setApiKey] = useState('');
  const [status, setStatus] = useState(null);

  const handleVerify = () => {
    // TODO: 실제 검증 로직 구현
    setStatus({ success: true, message: '검증 기능은 추후 구현됩니다.' });
  };

  return (
    <div className="card">
      <h3 style={{ marginBottom: '1rem' }}>API 키 설정</h3>
      <div style={{ display: 'flex', gap: '0.5rem', maxWidth: '500px' }}>
        <input
          type="text"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          placeholder="API 키를 입력하세요"
          style={{ flex: 1 }}
        />
        <button className="btn-primary" onClick={handleVerify}>검증</button>
      </div>
      {status && (
        <div className={status.success ? 'success-box' : 'error-box'} style={{ marginTop: '0.75rem', maxWidth: '500px' }}>
          {status.message}
        </div>
      )}
    </div>
  );
}

/* ---------- Credentials Tab ---------- */
function CredentialsTab() {
  const [credentials, setCredentials] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [editing, setEditing] = useState({});
  const [testStatus, setTestStatus] = useState({});
  const [saveStatus, setSaveStatus] = useState({});

  const fetchCredentials = useCallback(async () => {
    try {
      const { data } = await client.get('/settings/credentials');
      setCredentials(data.credentials || []);
    } catch (err) {
      setError('계정 목록을 불러올 수 없습니다.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchCredentials();
  }, [fetchCredentials]);

  const handleEdit = (supplier, field, value) => {
    setEditing((prev) => ({
      ...prev,
      [supplier]: { ...prev[supplier], [field]: value },
    }));
  };

  const handleSave = async (supplier) => {
    const edit = editing[supplier];
    if (!edit?.login_id && !edit?.login_pw) return;

    setSaveStatus((prev) => ({ ...prev, [supplier]: { loading: true } }));

    try {
      await client.put('/settings/credentials', {
        supplier,
        login_id: edit.login_id ?? '',
        login_pw: edit.login_pw ?? '',
      });
      setSaveStatus((prev) => ({ ...prev, [supplier]: { loading: false, success: true, message: '저장 완료' } }));
      setEditing((prev) => {
        const next = { ...prev };
        delete next[supplier];
        return next;
      });
      fetchCredentials();
    } catch {
      setSaveStatus((prev) => ({ ...prev, [supplier]: { loading: false, success: false, message: '저장 실패' } }));
    }
  };

  const handleTest = async (supplier) => {
    setTestStatus((prev) => ({ ...prev, [supplier]: { loading: true } }));

    try {
      const { data } = await client.post('/settings/credentials/test', { supplier });
      setTestStatus((prev) => ({
        ...prev,
        [supplier]: { loading: false, success: data.success, message: data.message },
      }));
    } catch (err) {
      setTestStatus((prev) => ({
        ...prev,
        [supplier]: { loading: false, success: false, message: err.response?.data?.detail || '테스트 실패' },
      }));
    }
  };

  if (loading) return <div className="empty-state"><span className="loading-spinner" /></div>;
  if (error) return <div className="error-box">{error}</div>;

  return (
    <div>
      <table>
        <thead>
          <tr>
            <th>도매상</th>
            <th>아이디</th>
            <th>비밀번호</th>
            <th>상태</th>
            <th>동작</th>
          </tr>
        </thead>
        <tbody>
          {credentials.map((cred) => {
            const edit = editing[cred.supplier];
            const test = testStatus[cred.supplier];
            const save = saveStatus[cred.supplier];
            return (
              <tr key={cred.supplier}>
                <td style={{ fontWeight: 500 }}>{cred.supplier}</td>
                <td>
                  <input
                    type="text"
                    defaultValue={cred.login_id}
                    onChange={(e) => handleEdit(cred.supplier, 'login_id', e.target.value)}
                    style={{ width: '10rem' }}
                  />
                </td>
                <td>
                  <input
                    type="password"
                    placeholder={cred.configured ? '(변경시 입력)' : '비밀번호'}
                    onChange={(e) => handleEdit(cred.supplier, 'login_pw', e.target.value)}
                    style={{ width: '10rem' }}
                  />
                </td>
                <td>
                  {cred.configured ? (
                    <span className="text-success">설정됨</span>
                  ) : (
                    <span className="text-secondary">미설정</span>
                  )}
                  {test && !test.loading && (
                    <span className={test.success ? 'text-success' : 'text-error'} style={{ marginLeft: '0.5rem', fontSize: '0.8rem' }}>
                      {test.message}
                    </span>
                  )}
                  {save && !save.loading && (
                    <span className={save.success ? 'text-success' : 'text-error'} style={{ marginLeft: '0.5rem', fontSize: '0.8rem' }}>
                      {save.message}
                    </span>
                  )}
                </td>
                <td>
                  <div style={{ display: 'flex', gap: '0.3rem' }}>
                    <button className="btn-primary btn-sm" onClick={() => handleSave(cred.supplier)} disabled={!edit}>
                      저장
                    </button>
                    <button className="btn-sm" onClick={() => handleTest(cred.supplier)} disabled={test?.loading}>
                      {test?.loading ? <span className="loading-spinner" /> : '테스트'}
                    </button>
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/* ---------- Telegram Tab ---------- */
function TelegramTab() {
  const [token, setToken] = useState('');
  const [chatId, setChatId] = useState('');
  const [loading, setLoading] = useState(true);
  const [status, setStatus] = useState(null);

  useEffect(() => {
    (async () => {
      try {
        const { data } = await client.get('/settings/telegram');
        setToken(data.token || '');
        setChatId(data.chat_id || '');
      } catch {
        // ignore - might not be configured yet
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  const handleSave = async () => {
    setStatus(null);
    try {
      await client.put('/settings/telegram', { token, chat_id: chatId });
      setStatus({ success: true, message: '저장 완료' });
    } catch {
      setStatus({ success: false, message: '저장 실패' });
    }
  };

  if (loading) return <div className="empty-state"><span className="loading-spinner" /></div>;

  return (
    <div className="card" style={{ maxWidth: '500px' }}>
      <h3 style={{ marginBottom: '1rem' }}>텔레그램 알림 설정</h3>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
        <div>
          <label style={{ display: 'block', marginBottom: '0.3rem', fontWeight: 500, fontSize: '0.85rem' }}>봇 토큰</label>
          <input type="text" value={token} onChange={(e) => setToken(e.target.value)} style={{ width: '100%' }} placeholder="123456:ABC..." />
        </div>
        <div>
          <label style={{ display: 'block', marginBottom: '0.3rem', fontWeight: 500, fontSize: '0.85rem' }}>Chat ID</label>
          <input type="text" value={chatId} onChange={(e) => setChatId(e.target.value)} style={{ width: '100%' }} placeholder="987654321" />
        </div>
        <div>
          <button className="btn-primary" onClick={handleSave}>저장</button>
        </div>
      </div>
      {status && (
        <div className={status.success ? 'success-box' : 'error-box'} style={{ marginTop: '0.75rem' }}>
          {status.message}
        </div>
      )}
    </div>
  );
}

/* ---------- Schedule Tab ---------- */
function ScheduleTab() {
  const [schedules, setSchedules] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [editing, setEditing] = useState({});
  const [saveStatus, setSaveStatus] = useState(null);

  useEffect(() => {
    (async () => {
      try {
        const { data } = await client.get('/settings/schedules');
        const list = data.schedules || data || [];
        setSchedules(Array.isArray(list) ? list : []);
      } catch {
        setError('스케줄을 불러올 수 없습니다.');
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  const handleChange = (index, field, value) => {
    setEditing((prev) => ({
      ...prev,
      [index]: { ...(prev[index] || schedules[index]), [field]: value },
    }));
  };

  const handleSave = async () => {
    const merged = schedules.map((s, i) => (editing[i] ? { ...s, ...editing[i] } : s));
    setSaveStatus(null);
    try {
      await client.put('/settings/schedules', { schedules: merged });
      setSaveStatus({ success: true, message: '저장 완료' });
      setSchedules(merged);
      setEditing({});
    } catch {
      setSaveStatus({ success: false, message: '저장 실패' });
    }
  };

  if (loading) return <div className="empty-state"><span className="loading-spinner" /></div>;
  if (error) return <div className="error-box">{error}</div>;

  if (schedules.length === 0) {
    return <div className="empty-state">등록된 스케줄이 없습니다.</div>;
  }

  return (
    <div>
      <table>
        <thead>
          <tr>
            <th>시간대</th>
            <th>간격(분)</th>
            <th>활성</th>
          </tr>
        </thead>
        <tbody>
          {schedules.map((sch, i) => {
            const current = editing[i] || sch;
            return (
              <tr key={i}>
                <td>{sch.time_range || sch.label || `${sch.start_hour || ''}~${sch.end_hour || ''}`}</td>
                <td>
                  <input
                    type="number"
                    min="1"
                    value={current.interval_minutes ?? current.interval ?? ''}
                    onChange={(e) => handleChange(i, 'interval_minutes', parseInt(e.target.value, 10))}
                    style={{ width: '5rem' }}
                  />
                </td>
                <td>
                  <input
                    type="checkbox"
                    checked={current.enabled ?? true}
                    onChange={(e) => handleChange(i, 'enabled', e.target.checked)}
                  />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <div style={{ marginTop: '0.75rem' }}>
        <button className="btn-primary" onClick={handleSave}>저장</button>
      </div>
      {saveStatus && (
        <div className={saveStatus.success ? 'success-box' : 'error-box'} style={{ marginTop: '0.75rem' }}>
          {saveStatus.message}
        </div>
      )}
    </div>
  );
}
