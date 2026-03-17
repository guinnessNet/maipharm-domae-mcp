import { useState, useEffect } from 'react';
import { getFavorites, removeFavorite, clearFavorites } from '../utils/favorites';
import { addToCart } from '../utils/cart';

export default function FavoritesPage() {
  const [favorites, setFavorites] = useState([]);
  const [quantities, setQuantities] = useState({});
  const [selected, setSelected] = useState(new Set());
  const [toast, setToast] = useState(null);

  useEffect(() => {
    setFavorites(getFavorites());
  }, []);

  const showToast = (msg) => {
    setToast(msg);
    setTimeout(() => setToast(null), 2000);
  };

  const handleQuantityChange = (index, value) => {
    setQuantities((prev) => ({ ...prev, [index]: value }));
  };

  const getQty = (index, fav) => {
    const val = parseInt(quantities[index], 10);
    return val > 0 ? val : (fav.defaultQuantity || 1);
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
  };

  const handleClearAll = () => {
    if (!window.confirm('모든 상비약 목록을 삭제하시겠습니까?')) return;
    clearFavorites();
    setFavorites([]);
    setSelected(new Set());
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

      <table>
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
              </td>
              <td>
                <input
                  type="number"
                  min="1"
                  value={quantities[index] || ''}
                  onChange={(e) => handleQuantityChange(index, e.target.value)}
                  placeholder={fav.defaultQuantity || '1'}
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
      </table>

      {toast && (
        <div className="cart-toast">{toast}</div>
      )}
    </div>
  );
}
