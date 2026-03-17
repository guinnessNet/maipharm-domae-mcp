import { useState, useEffect, useCallback } from 'react';
import { Routes, Route, NavLink, Navigate, useLocation } from 'react-router-dom';
import client from './api/client';
import SearchPage from './pages/SearchPage';
import CartPage from './pages/CartPage';
import SettingsPage from './pages/SettingsPage';
import SetupPage from './pages/SetupPage';
import UrgentPage from './pages/UrgentPage';
import HistoryPage from './pages/HistoryPage';
import ProductsPage from './pages/ProductsPage';
import MonitorAlertsPage from './pages/MonitorAlertsPage';
import { getCartCount } from './utils/cart';

export default function App() {
  const [setupDone, setSetupDone] = useState(null); // null=로딩, true/false
  const [cartCount, setCartCount] = useState(0);
  const location = useLocation();

  const refreshCartCount = useCallback(() => {
    setCartCount(getCartCount());
  }, []);

  useEffect(() => {
    refreshCartCount();
    window.addEventListener('cart-updated', refreshCartCount);
    return () => window.removeEventListener('cart-updated', refreshCartCount);
  }, [refreshCartCount]);

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
            <NavLink to="/cart" className={({ isActive }) => isActive ? 'active' : ''}>
              장바구니{cartCount > 0 && <span className="cart-badge">{cartCount}</span>}
            </NavLink>
            <NavLink to="/urgent">긴급주문</NavLink>
            <NavLink to="/history">주문이력</NavLink>
            <NavLink to="/products">모니터링</NavLink>
            <NavLink to="/alerts">변동내역</NavLink>
            <NavLink to="/settings">설정</NavLink>
          </nav>
        </div>
      </header>
      <main className="app-main">
        <Routes>
          <Route path="/setup" element={<SetupPage />} />
          <Route path="/" element={<SearchPage />} />
          <Route path="/cart" element={<CartPage />} />
          <Route path="/urgent" element={<UrgentPage />} />
          <Route path="/history" element={<HistoryPage />} />
          <Route path="/products" element={<ProductsPage />} />
          <Route path="/alerts" element={<MonitorAlertsPage />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Routes>
      </main>
    </>
  );
}
