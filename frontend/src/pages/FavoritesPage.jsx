import { useState, useEffect } from 'react';
import { getFavorites, removeFavorite, clearFavorites, updateFavoriteQuantity, updateFavoritePrice } from '../utils/favorites';
import { addToCart } from '../utils/cart';
import client from '../api/client';

export default function FavoritesPage() {
  const [favorites, setFavorites] = useState([]);
  const [quantities, setQuantities] = useState({});
  const [selected, setSelected] = useState(new Set());
  const [toast, setToast] = useState(null);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshProgress, setRefreshProgress] = useState('');
  const [previousPrices, setPreviousPrices] = useState({});

  useEffect(() => {
    const favs = getFavorites();
    setFavorites(favs);
    // Initialize quantities from persisted data
    const savedQty = {};
    favs.forEach((fav, i) => {
      if (fav.quantity) savedQty[i] = String(fav.quantity);
    });
    setQuantities(savedQty);
  }, []);

  const showToast = (msg) => {
    setToast(msg);
    setTimeout(() => setToast(null), 3000);
  };

  const handleQuantityChange = (index, value) => {
    setQuantities((prev) => ({ ...prev, [index]: value }));
    const parsed = parseInt(value, 10);
    if (parsed > 0) {
      updateFavoriteQuantity(index, parsed);
    }
  };

  const getQty = (index, fav) => {
    const val = parseInt(quantities[index], 10);
    return val > 0 ? val : (fav.quantity || fav.defaultQuantity || 1);
  };

  const handleToggleSelect = (index) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  };

  const handleSelectAll = () => {
    if (selected.size === favorites.length) {
      setSelected(new Set());
    } else {
      setSelected(new Set(favorites.map((_, i) => i)));
    }
  };

  const addItemToCart = (fav, index) => {
    addToCart({
      supplier: fav.supplier,
      productName: fav.productName,
      maker: fav.maker,
      unit: fav.unit,
      insuranceCode: fav.insuranceCode,
      productId: fav.productId,
      price: fav.price,
      quantity: getQty(index, fav),
    });
    window.dispatchEvent(new Event('cart-updated'));
  };

  const handleAddOne = (index) => {
    addItemToCart(favorites[index], index);
    showToast('장바구니에 추가됨');
  };

  const handleAddAll = () => {
    if (favorites.length === 0) return;
    favorites.forEach((fav, i) => addItemToCart(fav, i));
    showToast(`${favorites.length}개 장바구니에 추가됨`);
  };

  const handleAddSelected = () => {
    if (selected.size === 0) return;
    selected.forEach((i) => addItemToCart(favorites[i], i));
    showToast(`${selected.size}개 장바구니에 추가됨`);
  };

  const handleRemove = (index) => {
    const updated = removeFavorite(index);
    setFavorites(updated);
    // Adjust selected indices
    setSelected((prev) => {
      const next = new Set();
      prev.forEach((i) => {
        if (i < index) next.add(i);
        else if (i > index) next.add(i - 1);
      });
      return next;
    });
    // Re-build quantities and previousPrices for shifted indices
    setQuantities((prev) => {
      const next = {};
      Object.entries(prev).forEach(([k, v]) => {
        const ki = parseInt(k, 10);
        if (ki < index) next[ki] = v;
        else if (ki > index) next[ki - 1] = v;
      });
      return next;
    });
    setPreviousPrices((prev) => {
      const next = {};
      Object.entries(prev).forEach(([k, v]) => {
        const ki = parseInt(k, 10);
        if (ki < index) next[ki] = v;
        else if (ki > index) next[ki - 1] = v;
      });
      return next;
    });
  };

  const handleClearAll = () => {
    if (!window.confirm('모든 상비약 목록을 삭제하시겠습니까?')) return;
    clearFavorites();
    setFavorites([]);
    setSelected(new Set());
    setQuantities({});
    setPreviousPrices({});
  };

  const handleRefreshPrices = async () => {
    if (refreshing || favorites.length === 0) return;
    setRefreshing(true);
    setRefreshProgress('가격 조회 준비 중...');

    // Save current prices for comparison
    const oldPrices = {};
    favorites.forEach((fav, i) => {
      oldPrices[i] = fav.price;
    });

    // Group favorites by productName to minimize API calls
    const groups = {};
    favorites.forEach((fav, i) => {
      const key = fav.productName;
      if (!groups[key]) groups[key] = [];
      groups[key].push({ index: i, fav });
    });

    const keywords = Object.keys(groups);
    let updated = 0;
    let increased = 0;
    let decreased = 0;
    let errors = 0;

    for (let ki = 0; ki < keywords.length; ki++) {
      const keyword = keywords[ki];
      setRefreshProgress(`조회 중... (${ki + 1}/${keywords.length}) ${keyword}`);

      try {
        const { data } = await client.get('/search', { params: { keyword } });
        const results = data.results || [];

        for (const entry of groups[keyword]) {
          const { index, fav } = entry;
          for (const result of results) {
            if (result.product_name !== fav.productName) continue;
            const sup = (result.suppliers || []).find((s) => s.name === fav.supplier);
            if (sup && sup.price != null) {
              const oldPrice = fav.price;
              const newPrice = sup.price;
              updateFavoritePrice(index, newPrice);
              updated++;
              if (oldPrice != null && newPrice > oldPrice) increased++;
              if (oldPrice != null && newPrice < oldPrice) decreased++;
              break;
            }
          }
        }
      } catch {
        errors++;
      }
    }

    // Reload favorites from storage
    const refreshed = getFavorites();
    setFavorites(refreshed);
    setPreviousPrices(oldPrices);

    const parts = [`${updated}건 갱신`];
    const changed = increased + decreased;
    if (changed > 0) {
      const details = [];
      if (increased > 0) details.push(`\u2191${increased}`);
      if (decreased > 0) details.push(`\u2193${decreased}`);
      parts.push(`${changed}건 가격 변동 (${details.join(', ')})`);
    }
    if (errors > 0) parts.push(`${errors}건 조회 실패`);

    setRefreshing(false);
    setRefreshProgress('');
    showToast(parts.join(', '));
  };

  const renderPriceBadge = (fav, index) => {
    if (!fav.priceUpdatedAt) return null;
    const oldPrice = previousPrices[index];
    if (oldPrice == null || oldPrice === fav.price) return null;

    if (fav.price > oldPrice) {
      return (
        <span style={{ color: '#e53e3e', fontSize: '0.75rem', fontWeight: 600, marginLeft: '0.3rem' }}>
          {'\u2191'}
        </span>
      );
    }
    if (fav.price < oldPrice) {
      return (
        <span style={{ color: '#3182ce', fontSize: '0.75rem', fontWeight: 600, marginLeft: '0.3rem' }}>
          {'\u2193'}
        </span>
      );
    }
    return null;
  };

  if (favorites.length === 0) {
    return (
      <div className="empty-state">
        <p>등록된 상비약이 없습니다.</p>
        <p className="text-secondary" style={{ marginTop: '0.5rem' }}>
          검색 결과에서 ★ 버튼으로 추가해주세요.
        </p>
      </div>
    );
  }

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1rem' }}>
        <h2 style={{ fontSize: '1.1rem', fontWeight: 700 }}>
          상비약 목록 <span className="text-secondary" style={{ fontWeight: 400, fontSize: '0.9rem' }}>({favorites.length})</span>
        </h2>
        <div style={{ display: 'flex', gap: '0.5rem' }}>
          <button
            className="btn-sm btn-primary"
            onClick={handleRefreshPrices}
            disabled={refreshing}
            style={{ opacity: refreshing ? 0.6 : 1 }}
          >
            {refreshing ? '갱신 중...' : '가격 갱신'}
          </button>
          <button className="btn-sm btn-success" onClick={handleAddAll}>
            전체 장바구니 추가
          </button>
          <button
            className="btn-sm btn-primary"
            onClick={handleAddSelected}
            disabled={selected.size === 0}
          >
            선택 추가 ({selected.size})
          </button>
          <button className="btn-sm btn-danger" onClick={handleClearAll}>
            전체 삭제
          </button>
        </div>
      </div>

      {refreshProgress && (
        <div style={{ marginBottom: '0.75rem', fontSize: '0.85rem', color: '#666' }}>
          {refreshProgress}
        </div>
      )}

      <div className="table-wrapper"><table>
        <thead>
          <tr>
            <th style={{ width: '2.5rem' }}>
              <input
                type="checkbox"
                className="fav-checkbox"
                checked={selected.size === favorites.length}
                onChange={handleSelectAll}
              />
            </th>
            <th>도매상</th>
            <th>제품명</th>
            <th>단위</th>
            <th className="text-right">단가</th>
            <th>수량</th>
            <th>액션</th>
          </tr>
        </thead>
        <tbody>
          {favorites.map((fav, index) => (
            <tr key={`${fav.supplier}-${fav.productId}-${index}`}>
              <td>
                <input
                  type="checkbox"
                  className="fav-checkbox"
                  checked={selected.has(index)}
                  onChange={() => handleToggleSelect(index)}
                />
              </td>
              <td>{fav.supplier}</td>
              <td>
                {fav.productName}
                {fav.maker && (
                  <span className="text-secondary" style={{ fontSize: '0.85rem', display: 'block' }}>
                    {fav.maker}
                  </span>
                )}
              </td>
              <td>{fav.unit}</td>
              <td className="text-right">
                {fav.price != null ? fav.price.toLocaleString() + '원' : '-'}
                {renderPriceBadge(fav, index)}
              </td>
              <td>
                <input
                  type="number"
                  min="1"
                  value={quantities[index] || ''}
                  onChange={(e) => handleQuantityChange(index, e.target.value)}
                  placeholder={fav.quantity || fav.defaultQuantity || '1'}
                  style={{ width: '4rem' }}
                />
              </td>
              <td style={{ display: 'flex', gap: '0.4rem', alignItems: 'center' }}>
                <button className="btn-sm btn-fav-add" onClick={() => handleAddOne(index)}>
                  담기
                </button>
                <button className="btn-sm cart-remove-btn" onClick={() => handleRemove(index)}>
                  ✕
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table></div>

      {toast && (
        <div className="cart-toast">{toast}</div>
      )}
    </div>
  );
}
