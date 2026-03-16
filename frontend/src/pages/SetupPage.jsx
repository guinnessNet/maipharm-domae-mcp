import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import client from '../api/client';

const STEPS = [
  { key: 'apikey', label: 'API 키 등록', number: 1 },
  { key: 'credentials', label: '도매 계정 설정', number: 2 },
  { key: 'done', label: '완료', number: 3 },
];

export default function SetupPage() {
  const [step, setStep] = useState(0);
  const navigate = useNavigate();

  return (
    <div style={{ maxWidth: '600px', margin: '2rem auto', padding: '0 1rem' }}>
      <h2 style={{ marginBottom: '0.5rem' }}>초기 설정</h2>
      <p className="text-secondary" style={{ marginBottom: '1.5rem' }}>
        도매 통합검색을 사용하려면 API 키와 도매 계정이 필요합니다.
      </p>

      {/* Progress Bar */}
      <div style={{ display: 'flex', gap: '0.5rem', marginBottom: '2rem' }}>
        {STEPS.map((s, i) => (
          <div key={s.key} style={{ flex: 1 }}>
            <div style={{
              height: '4px',
              borderRadius: '2px',
              background: i <= step ? 'var(--color-primary, #3b82f6)' : 'var(--color-border, #e2e8f0)',
              transition: 'background 0.3s',
            }} />
            <div style={{
              fontSize: '0.75rem',
              marginTop: '0.3rem',
              color: i <= step ? 'var(--color-primary, #3b82f6)' : 'var(--color-text-secondary, #94a3b8)',
              fontWeight: i === step ? 600 : 400,
            }}>
              {s.number}. {s.label}
            </div>
          </div>
        ))}
      </div>

      {step === 0 && <ApiKeyStep onNext={() => setStep(1)} />}
      {step === 1 && <CredentialsStep onNext={() => setStep(2)} onBack={() => setStep(0)} />}
      {step === 2 && <DoneStep onFinish={() => navigate('/')} />}
    </div>
  );
}

/* ---------- Step 1: API Key ---------- */
function ApiKeyStep({ onNext }) {
  const [apiKey, setApiKey] = useState('');
  const [loading, setLoading] = useState(false);
  const [status, setStatus] = useState(null);

  // 기존 API 키 불러오기
  useEffect(() => {
    (async () => {
      try {
        const { data } = await client.get('/settings/setup-status');
        if (data.api_key_set && data.api_key_prefix) {
          setApiKey(data.api_key_prefix);
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
        setTimeout(onNext, 1500);
      }
    } catch (err) {
      setStatus({ valid: false, message: err.response?.data?.detail || '서버 연결 실패' });
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="card">
      <h3 style={{ marginBottom: '0.5rem' }}>API 키 등록</h3>
      <p className="text-secondary" style={{ fontSize: '0.85rem', marginBottom: '0.5rem' }}>
        팜스퀘어에서 무료 회원가입 후 API 키를 발급받으세요.
      </p>
      <a
        href="https://pharmsq.com/settings"
        target="_blank"
        rel="noopener noreferrer"
        style={{ display: 'inline-block', fontSize: '0.85rem', marginBottom: '1rem', color: 'var(--color-primary, #3b82f6)' }}
      >
        pharmsq.com에서 API 키 발급받기 →
      </a>

      <div style={{ display: 'flex', gap: '0.5rem' }}>
        <input
          type="text"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          placeholder="dmk_free_xxxxxxxxxxxxxxxx"
          style={{ flex: 1, fontFamily: 'monospace', fontSize: '0.85rem' }}
          onKeyDown={(e) => e.key === 'Enter' && handleVerify()}
        />
        <button className="btn-primary" onClick={handleVerify} disabled={loading || !apiKey.trim()}>
          {loading ? <span className="loading-spinner" /> : '검증'}
        </button>
      </div>

      {status && (
        <div className={status.valid ? 'success-box' : 'error-box'} style={{ marginTop: '0.75rem' }}>
          <div>{status.message}</div>
          {status.valid && status.tier && (
            <div style={{ fontSize: '0.8rem', marginTop: '0.3rem' }}>
              등급: {status.tier} | 약국: {status.pharmacy_name || '-'} | 크롤러: {status.crawler_count}개
            </div>
          )}
        </div>
      )}

      <div style={{ marginTop: '1rem', fontSize: '0.8rem', color: 'var(--color-text-secondary, #94a3b8)' }}>
        API 키가 없으면 크롤러를 다운로드할 수 없어 검색/주문이 불가합니다.
      </div>
    </div>
  );
}

/* ---------- Step 2: Credentials ---------- */
function CredentialsStep({ onNext, onBack }) {
  const [credentials, setCredentials] = useState([]);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState({});
  const [saveStatus, setSaveStatus] = useState({});

  useEffect(() => {
    (async () => {
      try {
        const { data } = await client.get('/settings/credentials');
        setCredentials(data.credentials || []);
      } catch {}
      setLoading(false);
    })();
  }, []);

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
      setSaveStatus((prev) => ({ ...prev, [supplier]: { success: true, message: '저장됨' } }));
    } catch {
      setSaveStatus((prev) => ({ ...prev, [supplier]: { success: false, message: '실패' } }));
    }
  };

  if (loading) return <div className="empty-state"><span className="loading-spinner" /></div>;

  const configuredCount = credentials.filter((c) => c.configured || saveStatus[c.supplier]?.success).length;

  return (
    <div>
      <div className="card" style={{ marginBottom: '1rem' }}>
        <h3 style={{ marginBottom: '0.5rem' }}>도매 계정 설정</h3>
        <p className="text-secondary" style={{ fontSize: '0.85rem', marginBottom: '1rem' }}>
          사용하는 도매상의 계정을 등록하세요. 나중에 설정 페이지에서도 변경할 수 있습니다.
        </p>

        <table>
          <thead>
            <tr><th>도매상</th><th>아이디</th><th>비밀번호</th><th></th></tr>
          </thead>
          <tbody>
            {credentials.map((cred) => {
              const edit = editing[cred.supplier];
              const save = saveStatus[cred.supplier];
              return (
                <tr key={cred.supplier}>
                  <td style={{ fontWeight: 500, whiteSpace: 'nowrap' }}>
                    {cred.supplier}
                    {(cred.configured || save?.success) && (
                      <span className="text-success" style={{ marginLeft: '0.3rem', fontSize: '0.75rem' }}>●</span>
                    )}
                  </td>
                  <td>
                    <input
                      type="text"
                      defaultValue={cred.login_id}
                      onChange={(e) => handleEdit(cred.supplier, 'login_id', e.target.value)}
                      style={{ width: '8rem', fontSize: '0.85rem' }}
                    />
                  </td>
                  <td>
                    <input
                      type="password"
                      placeholder={cred.configured ? '(변경시 입력)' : '비밀번호'}
                      onChange={(e) => handleEdit(cred.supplier, 'login_pw', e.target.value)}
                      style={{ width: '8rem', fontSize: '0.85rem' }}
                    />
                  </td>
                  <td>
                    <button className="btn-primary btn-sm" onClick={() => handleSave(cred.supplier)} disabled={!edit}>
                      저장
                    </button>
                    {save && !save.loading && (
                      <span className={save.success ? 'text-success' : 'text-error'} style={{ marginLeft: '0.3rem', fontSize: '0.75rem' }}>
                        {save.message}
                      </span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div style={{ display: 'flex', justifyContent: 'space-between' }}>
        <button onClick={onBack}>이전</button>
        <button className="btn-primary" onClick={onNext}>
          {configuredCount > 0 ? '다음' : '건너뛰기'}
        </button>
      </div>
    </div>
  );
}

/* ---------- Step 3: Done ---------- */
function DoneStep({ onFinish }) {
  const [status, setStatus] = useState(null);

  useEffect(() => {
    (async () => {
      try {
        const { data } = await client.get('/settings/setup-status');
        setStatus(data);
      } catch {}
    })();
  }, []);

  return (
    <div className="card" style={{ textAlign: 'center' }}>
      <h3 style={{ marginBottom: '1rem' }}>설정 완료!</h3>

      {status && (
        <div style={{ marginBottom: '1.5rem', fontSize: '0.9rem', textAlign: 'left' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', padding: '0.4rem 0', borderBottom: '1px solid var(--color-border, #e2e8f0)' }}>
            <span>API 키</span>
            <span className={status.api_key_set ? 'text-success' : 'text-error'}>
              {status.api_key_set ? '등록됨' : '미등록'}
            </span>
          </div>
          <div style={{ display: 'flex', justifyContent: 'space-between', padding: '0.4rem 0', borderBottom: '1px solid var(--color-border, #e2e8f0)' }}>
            <span>크롤러</span>
            <span>{status.crawler_count}개 로드됨</span>
          </div>
          <div style={{ display: 'flex', justifyContent: 'space-between', padding: '0.4rem 0', borderBottom: '1px solid var(--color-border, #e2e8f0)' }}>
            <span>도매 계정</span>
            <span>{status.credentials_configured}/{status.credentials_total}개 설정됨</span>
          </div>
          <div style={{ display: 'flex', justifyContent: 'space-between', padding: '0.4rem 0' }}>
            <span>텔레그램</span>
            <span className={status.telegram_set ? 'text-success' : 'text-secondary'}>
              {status.telegram_set ? '설정됨' : '미설정 (선택사항)'}
            </span>
          </div>
        </div>
      )}

      <p className="text-secondary" style={{ fontSize: '0.85rem', marginBottom: '1rem' }}>
        텔레그램 알림, 스케줄 설정은 메뉴 &gt; 설정에서 추가로 구성할 수 있습니다.
      </p>

      {/* MCP 연결 가이드 (접이식) */}
      <McpGuide />

      <button className="btn-primary" onClick={onFinish} style={{ padding: '0.6rem 2rem' }}>
        검색 시작하기
      </button>
    </div>
  );
}

/* ---------- MCP Guide (접이식) ---------- */
function McpGuide() {
  const [open, setOpen] = useState(false);

  return (
    <div style={{ textAlign: 'left', marginBottom: '1.5rem' }}>
      <button
        onClick={() => setOpen(!open)}
        style={{ fontSize: '0.8rem', color: 'var(--color-text-secondary, #94a3b8)', background: 'none', border: 'none', cursor: 'pointer', padding: 0 }}
      >
        {open ? '▾' : '▸'} 고급: Claude AI와 연결하기 (개발자용)
      </button>

      {open && (
        <div style={{ marginTop: '0.5rem', padding: '1rem', background: 'var(--color-bg-secondary, #f8fafc)', borderRadius: '0.5rem' }}>
          <p className="text-secondary" style={{ fontSize: '0.8rem', marginBottom: '0.5rem' }}>
            Claude Desktop 앱에서 AI에게 말로 검색/주문을 시킬 수 있습니다.
          </p>
          <p className="text-secondary" style={{ fontSize: '0.8rem', marginBottom: '0.3rem' }}>
            Claude Desktop 설정 파일에 아래를 추가하세요:
          </p>
          <pre style={{ background: '#1e293b', color: '#e2e8f0', padding: '0.75rem', borderRadius: '0.4rem', fontSize: '0.75rem', overflow: 'auto', lineHeight: 1.5 }}>
{`{
  "mcpServers": {
    "domae": {
      "command": "python",
      "args": ["-m", "domae_mcp", "--mcp"]
    }
  }
}`}
          </pre>
          <p className="text-secondary" style={{ fontSize: '0.7rem', marginTop: '0.5rem' }}>
            파일 위치: <code>%APPDATA%\Claude\claude_desktop_config.json</code>
          </p>
        </div>
      )}
    </div>
  );
}
