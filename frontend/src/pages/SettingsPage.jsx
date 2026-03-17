import { useState, useEffect, useCallback } from 'react';
import client from '../api/client';
import { getFontSize, setFontSize } from '../utils/fontSize';

const TABS = [
  { key: 'apikey', label: 'API 키' },
  { key: 'credentials', label: '도매 계정' },
  { key: 'telegram', label: '텔레그램' },
  { key: 'schedule', label: '스케줄' },
];

export default function SettingsPage() {
  const [activeTab, setActiveTab] = useState('apikey');
  const [currentFontSize, setCurrentFontSize] = useState(getFontSize() || 'normal');

  const handleFontChange = (size) => {
    setFontSize(size);
    setCurrentFontSize(size);
  };

  return (
    <div>
      {/* 글씨 크기 설정 */}
      <div className="card" style={{ marginBottom: '1.25rem', maxWidth: '500px' }}>
        <h3 style={{ marginBottom: '0.5rem' }}>글씨 크기</h3>
        <div style={{ display: 'flex', gap: '0.5rem' }}>
          <button
            className={currentFontSize === 'normal' ? 'btn-primary' : ''}
            onClick={() => handleFontChange('normal')}
          >
            보통
          </button>
          <button
            className={currentFontSize === 'large' ? 'btn-primary' : ''}
            onClick={() => handleFontChange('large')}
          >
            큰 글씨
          </button>
          <button
            className={currentFontSize === 'xlarge' ? 'btn-primary' : ''}
            onClick={() => handleFontChange('xlarge')}
          >
            매우 큰 글씨
          </button>
        </div>
      </div>

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
  const [loading, setLoading] = useState(false);
  const [status, setStatus] = useState(null);
  const [info, setInfo] = useState(null);

  useEffect(() => {
    (async () => {
      try {
        const { data } = await client.get('/settings/setup-status');
        if (data.api_key_set) {
          setApiKey(data.api_key_prefix);
          setInfo({
            crawlerCount: data.crawler_count,
            credentialsConfigured: data.credentials_configured,
          });
        }
      } catch {}
    })();
  }, []);

  const handleVerify = async () => {
    if (!apiKey.trim()) return;
    setLoading(true);
    setStatus(null);
    try {
      const { data } = await client.post('/settings/api-key/verify', { api_key: apiKey.trim() });
      setStatus(data);
      if (data.valid) {
        setInfo({ crawlerCount: data.crawler_count });
      }
    } catch (err) {
      setStatus({ valid: false, message: err.response?.data?.detail || '서버 연결 실패' });
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="card">
      <h3 style={{ marginBottom: '1rem' }}>API 키 설정</h3>

      {info && info.crawlerCount > 0 && (
        <div className="success-box" style={{ marginBottom: '1rem', maxWidth: '500px' }}>
          크롤러 {info.crawlerCount}개 활성
          {info.credentialsConfigured !== undefined && ` | 계정 ${info.credentialsConfigured}개 설정됨`}
        </div>
      )}

      <div style={{ display: 'flex', gap: '0.5rem', maxWidth: '500px' }}>
        <input
          type="text"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          placeholder="dmk_free_xxxxxxxxxxxxxxxx"
          style={{ flex: 1, fontFamily: 'monospace', fontSize: '0.85rem' }}
          onKeyDown={(e) => e.key === 'Enter' && handleVerify()}
        />
        <button className="btn-primary" onClick={handleVerify} disabled={loading || !apiKey.trim()}>
          {loading ? <span className="loading-spinner" /> : '검증 및 저장'}
        </button>
      </div>
      {status && (
        <div className={status.valid ? 'success-box' : 'error-box'} style={{ marginTop: '0.75rem', maxWidth: '500px' }}>
          <div>{status.message}</div>
          {status.valid && status.tier && (
            <div style={{ fontSize: '0.8rem', marginTop: '0.3rem' }}>
              등급: {status.tier} | 약국: {status.pharmacy_name || '-'}
            </div>
          )}
        </div>
      )}

      <p className="text-secondary" style={{ fontSize: '0.8rem', marginTop: '0.75rem', maxWidth: '500px' }}>
        API 키를 변경하면 기존 크롤러 캐시가 무효화되고 새로 다운로드됩니다.
      </p>
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
  const [showGuide, setShowGuide] = useState(false);

  useEffect(() => {
    (async () => {
      try {
        const { data } = await client.get('/settings/telegram');
        setToken(data.token || '');
        setChatId(data.chat_id || '');
      } catch {
        // ignore
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
    <div className="card" style={{ maxWidth: '550px' }}>
      <h3 style={{ marginBottom: '0.5rem' }}>텔레그램 알림 설정</h3>
      <p className="text-secondary" style={{ fontSize: '0.8rem', marginBottom: '1rem' }}>
        가격 변동, 긴급주문 체결 등의 알림을 텔레그램으로 받을 수 있습니다.
      </p>

      {/* 설정 가이드 토글 */}
      <button
        onClick={() => setShowGuide(!showGuide)}
        style={{ fontSize: '0.8rem', color: 'var(--color-primary, #3b82f6)', background: 'none', border: 'none', cursor: 'pointer', marginBottom: '0.75rem', padding: 0 }}
      >
        {showGuide ? '안내 닫기' : '처음이신가요? 설정 방법 보기'}
      </button>

      {showGuide && (
        <div style={{ background: 'var(--color-bg-secondary, #f8fafc)', padding: '1rem', borderRadius: '0.5rem', marginBottom: '1rem', fontSize: '0.8rem', lineHeight: 1.7 }}>
          <strong>1단계: 텔레그램 봇 만들기</strong>
          <ol style={{ paddingLeft: '1.2rem', margin: '0.3rem 0 0.75rem' }}>
            <li>텔레그램 앱에서 <strong>@BotFather</strong> 검색 → 대화 시작</li>
            <li><code>/newbot</code> 입력 → 봇 이름, 사용자명 지정</li>
            <li>발급된 <strong>봇 토큰</strong>을 아래에 입력</li>
          </ol>
          <strong>2단계: Chat ID 알아내기</strong>
          <ol style={{ paddingLeft: '1.2rem', margin: '0.3rem 0 0' }}>
            <li>방금 만든 봇에게 아무 메시지 보내기 (예: "안녕")</li>
            <li>브라우저에서 아래 주소 접속 (토큰 부분 교체):</li>
            <li style={{ wordBreak: 'break-all' }}>
              <code>https://api.telegram.org/bot[토큰]/getUpdates</code>
            </li>
            <li>응답에서 <code>"chat":{'"'}id":숫자{'}'}</code> 부분의 숫자가 Chat ID</li>
          </ol>
        </div>
      )}

      <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
        <div>
          <label style={{ display: 'block', marginBottom: '0.3rem', fontWeight: 500, fontSize: '0.85rem' }}>봇 토큰</label>
          <input type="text" value={token} onChange={(e) => setToken(e.target.value)} style={{ width: '100%', fontFamily: 'monospace', fontSize: '0.85rem' }} placeholder="7123456789:AAHxxxxxxxxxxxxxxxxxxxxxxxx" />
        </div>
        <div>
          <label style={{ display: 'block', marginBottom: '0.3rem', fontWeight: 500, fontSize: '0.85rem' }}>Chat ID</label>
          <input type="text" value={chatId} onChange={(e) => setChatId(e.target.value)} style={{ width: '100%', fontFamily: 'monospace', fontSize: '0.85rem' }} placeholder="987654321" />
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
                <td style={{ opacity: (current.enabled ?? true) ? 1 : 0.4 }}>
                  {`${String(sch.start_hour).padStart(2, '0')}:00 ~ ${String(sch.end_hour).padStart(2, '0')}:00`}
                </td>
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
