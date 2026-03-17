import { useState, useEffect, useCallback } from 'react';
import client from '../api/client';
import { getCart, removeFromCart, updateQuantity, clearCart, setCartItems } from '../utils/cart';

export default function CartPage() {
  const [cart, setCart] = useState([]);
  const [checkoutProgress, setCheckoutProgress] = useState(null); // { current, total, results }

  useEffect(() => {
    setCart(getCart());
  }, []);

  const handleUpdateQty = useCallback((index, delta) => {
    const item = cart[index];
    if (!item) return;
    const newQty = Math.max(1, (item.quantity || 1) + delta);
    setCart(updateQuantity(index, newQty));
  }, [cart]);

  const handleQtyInput = useCallback((index, value) => {
    const qty = parseInt(value, 10);
    if (isNaN(qty) || qty < 1) return;
    setCart(updateQuantity(index, qty));
  }, []);

  const handleRemove = useCallback((index) => {
    setCart(removeFromCart(index));
    window.dispatchEvent(new Event('cart-updated'));
  }, []);

  const handleClear = useCallback(() => {
    if (!window.confirm('장바구니를 비우시겠습니까?')) return;
    setCart(clearCart());
    window.dispatchEvent(new Event('cart-updated'));
  }, []);

  const handleCheckout = async () => {
    if (cart.length === 0) return;
    if (!window.confirm(`${cart.length}건의 주문을 일괄 실행하시겠습니까?`)) return;

    setCheckoutProgress({ current: 0, total: cart.length, results: [] });

    // Group by supplier
    const groups = {};
    cart.forEach((item) => {
      const key = item.supplier || '알 수 없음';
      if (!groups[key]) groups[key] = [];
      groups[key].push(item);
    });

    let completed = 0;
    const allResults = [];

    // Parallel by supplier, sequential within supplier
    const promises = Object.entries(groups).map(async ([, items]) => {
      for (const item of items) {
        let result;
        try {
          const { data } = await client.post('/orders', {
            supplier: item.supplier,
            product_id: item.productId,
            product_name: item.productName,
            quantity: item.quantity || 1,
          });
          result = { ...item, success: true, message: data.message || '주문 완료' };
        } catch (err) {
          result = { ...item, success: false, message: err.response?.data?.detail || '주문 실패' };
        }
        completed++;
        allResults.push(result);
        setCheckoutProgress((prev) => ({ ...prev, current: completed, results: [...allResults] }));
      }
    });

    await Promise.allSettled(promises);

    // Only remove successful items; keep failed items in cart for retry
    const failedItems = allResults.filter((r) => !r.success).map((r) => {
      // Strip result-specific fields before saving back to cart
      const { success, message, ...cartItem } = r;
      return cartItem;
    });
    setCart(setCartItems(failedItems));
    window.dispatchEvent(new Event('cart-updated'));
    setCheckoutProgress((prev) => ({ ...prev, current: cart.length, results: [...allResults] }));
  };

  // Group items by supplier
  const grouped = cart.reduce((acc, item, index) => {
    const key = item.supplier || '알 수 없음';
    if (!acc[key]) acc[key] = [];
    acc[key].push({ ...item, _index: index });
    return acc;
  }, {});

  const totalPrice = cart.reduce((sum, item) => {
    if (item.price != null) {
      return sum + item.price * (item.quantity || 1);
    }
    return sum;
  }, 0);

  // Checkout result view
  if (checkoutProgress && checkoutProgress.current === checkoutProgress.total && checkoutProgress.results.length > 0) {
    const successCount = checkoutProgress.results.filter((r) => r.success).length;
    const failCount = checkoutProgress.results.length - successCount;

    return (
      <div>
        <h2 style={{ marginBottom: '1rem' }}>주문 결과</h2>
        <div className={successCount === checkoutProgress.total ? 'success-box' : 'error-box'} style={{ marginBottom: '1rem' }}>
          총 {checkoutProgress.total}건 중 {successCount}건 성공{failCount > 0 ? `, ${failCount}건 실패` : ''}
        </div>
        {failCount > 0 && (
          <div className="error-box" style={{ marginBottom: '1rem', fontSize: '0.875rem' }}>
            실패한 {failCount}건은 장바구니에 남아있습니다.
          </div>
        )}
        <div className="table-wrapper"><table>
          <thead>
            <tr>
              <th>도매상</th>
              <th>제품명</th>
              <th className="text-right">수량</th>
              <th>결과</th>
            </tr>
          </thead>
          <tbody>
            {checkoutProgress.results.map((r, i) => (
              <tr key={i}>
                <td>{r.supplier}</td>
                <td>{r.productName}</td>
                <td className="text-right">{r.quantity || 1}</td>
                <td>
                  {r.success ? (
                    <span className="text-success">{r.message}</span>
                  ) : (
                    <span className="text-error">{r.message}</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table></div>
        <div style={{ marginTop: '1rem', display: 'flex', gap: '0.5rem' }}>
          {failCount > 0 && (
            <button className="btn-primary" onClick={() => {
              setCart(getCart());
              setCheckoutProgress(null);
            }}>
              실패 건 재주문 ({failCount}건)
            </button>
          )}
          <button className={failCount > 0 ? 'btn-sm' : 'btn-primary'} onClick={() => setCheckoutProgress(null)}>
            돌아가기
          </button>
        </div>
      </div>
    );
  }

  // Checkout in progress
  if (checkoutProgress && checkoutProgress.current < checkoutProgress.total) {
    return (
      <div className="empty-state">
        <span className="loading-spinner" style={{ width: '2rem', height: '2rem' }} />
        <p style={{ marginTop: '0.75rem' }}>
          주문 처리 중... ({checkoutProgress.current}/{checkoutProgress.total})
        </p>
        <div className="progress" style={{ maxWidth: '300px', margin: '1rem auto 0' }}>
          <div
            className="progress-bar"
            style={{ width: `${(checkoutProgress.current / checkoutProgress.total) * 100}%` }}
          />
        </div>
      </div>
    );
  }

  // Empty cart
  if (cart.length === 0) {
    return (
      <div className="empty-state">
        <p>장바구니가 비어 있습니다.</p>
        <p className="text-secondary" style={{ marginTop: '0.5rem', fontSize: '0.875rem' }}>
          통합검색에서 제품을 담아보세요.
        </p>
      </div>
    );
  }

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1rem' }}>
        <h2 style={{ fontSize: '1.1rem', fontWeight: 700 }}>장바구니 ({cart.length}건)</h2>
        <button className="btn-sm" style={{ color: 'var(--color-error)' }} onClick={handleClear}>
          비우기
        </button>
      </div>

      {Object.entries(grouped).map(([supplier, items]) => (
        <div key={supplier} style={{ marginBottom: '1.25rem' }}>
          <div style={{
            padding: '0.5rem 0.75rem',
            background: '#f1f5f9',
            borderRadius: 'var(--radius) var(--radius) 0 0',
            fontWeight: 600,
            fontSize: '0.875rem',
            border: '1px solid var(--color-border)',
            borderBottom: 'none',
          }}>
            {supplier}
          </div>
          <div className="table-wrapper"><table style={{ borderRadius: '0 0 var(--radius) var(--radius)' }}>
            <thead>
              <tr>
                <th>제품명</th>
                <th>단위</th>
                <th className="text-right">단가</th>
                <th style={{ textAlign: 'center' }}>수량</th>
                <th className="text-right">소계</th>
                <th style={{ width: '3rem' }}></th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item._index}>
                  <td>
                    {item.productName}
                    {item.maker && (
                      <span className="text-secondary" style={{ fontSize: '0.78rem', display: 'block' }}>
                        {item.maker}
                      </span>
                    )}
                  </td>
                  <td>{item.unit || '-'}</td>
                  <td className="text-right">
                    {item.price != null ? item.price.toLocaleString() + '원' : '-'}
                  </td>
                  <td>
                    <div className="cart-qty-controls">
                      <button
                        className="cart-qty-btn"
                        onClick={() => handleUpdateQty(item._index, -1)}
                        disabled={(item.quantity || 1) <= 1}
                      >
                        -
                      </button>
                      <input
                        type="number"
                        className="cart-qty-input"
                        value={item.quantity || 1}
                        min="1"
                        onChange={(e) => handleQtyInput(item._index, e.target.value)}
                      />
                      <button
                        className="cart-qty-btn"
                        onClick={() => handleUpdateQty(item._index, 1)}
                      >
                        +
                      </button>
                    </div>
                  </td>
                  <td className="text-right" style={{ fontWeight: 600 }}>
                    {item.price != null
                      ? (item.price * (item.quantity || 1)).toLocaleString() + '원'
                      : '-'}
                  </td>
                  <td style={{ textAlign: 'center' }}>
                    <button
                      className="cart-remove-btn"
                      onClick={() => handleRemove(item._index)}
                      title="삭제"
                    >
                      &times;
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table></div>
        </div>
      ))}

      <div className="cart-footer">
        <div className="cart-total">
          합계: <strong>{totalPrice.toLocaleString()}원</strong>
        </div>
        <button className="btn-primary" onClick={handleCheckout}>
          일괄 주문 ({cart.length}건)
        </button>
      </div>
    </div>
  );
}
