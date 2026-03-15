import { Routes, Route, NavLink } from 'react-router-dom';
import SearchPage from './pages/SearchPage';
import SettingsPage from './pages/SettingsPage';

function Placeholder({ title }) {
  return <div className="empty-state"><h2>{title}</h2><p>준비 중입니다.</p></div>;
}

export default function App() {
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
