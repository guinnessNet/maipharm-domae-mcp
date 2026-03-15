import { useState, useEffect } from 'react';
import { Routes, Route, NavLink, Navigate, useLocation } from 'react-router-dom';
import client from './api/client';
import SearchPage from './pages/SearchPage';
import SettingsPage from './pages/SettingsPage';
import SetupPage from './pages/SetupPage';

function Placeholder({ title }) {
  return <div className="empty-state"><h2>{title}</h2><p>준비 중입니다.</p></div>;
}

export default function App() {
  const [setupDone, setSetupDone] = useState(null); // null=로딩, true/false
  const location = useLocation();

  useEffect(() => {
    (async () => {
      try {
        const { data } = await client.get('/settings/setup-status');
        setSetupDone(data.api_key_set && data.crawler_count > 0);
      } catch {
        setSetupDone(true); // 서버 오류 시 셋업 건너뛰기
      }
    })();
  }, [location.pathname]);

  // 로딩 중
  if (setupDone === null) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: '100vh' }}>
        <span className="loading-spinner" />
      </div>
    );
  }

  // 셋업 미완료 시 /setup으로 리다이렉트
  if (!setupDone && location.pathname !== '/setup') {
    return <Navigate to="/setup" replace />;
  }

  return (
    <>
      <header className="app-header">
        <div className="app-header-inner">
          <NavLink to="/" className="app-logo">도매 통합검색</NavLink>
          <nav className="app-nav">
            <NavLink to="/" end>통합검색</NavLink>
            <NavLink to="/urgent">긴급주문</NavLink>
            <NavLink to="/history">주문이력</NavLink>
            <NavLink to="/products">모니터링</NavLink>
            <NavLink to="/settings">설정</NavLink>
          </nav>
        </div>
      </header>
      <main className="app-main">
        <Routes>
          <Route path="/setup" element={<SetupPage />} />
          <Route path="/" element={<SearchPage />} />
          <Route path="/urgent" element={<Placeholder title="긴급주문" />} />
          <Route path="/history" element={<Placeholder title="주문이력" />} />
          <Route path="/products" element={<Placeholder title="모니터링 제품" />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Routes>
      </main>
    </>
  );
}
