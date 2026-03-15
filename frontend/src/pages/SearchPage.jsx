import { useState } from 'react';
import client from '../api/client';

export default function SearchPage() {
  const [keyword, setKeyword] = useState('');
  const [results, setResults] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [orderStatus, setOrderStatus] = useState({});
  const [quantities, setQuantities] = useState({});

  const handleSearch = async (e) => {
    e.preventDefault();
    const q = keyword.trim();
    if (!q) return;

    setLoading(true);
    setError('');
    setResults([]);
    setOrderStatus({});
    setQuantities({});

    try {
      const { data } = await client.get('/search', { params: { keyword: q } });
      setResults(data.results || []);
    } catch (err) {
      setError(err.response?.data?.detail || '검색 중 오류가 발생했습니다.');
    } finally {
      setLoading(false);
    }
  };

  // Flatten results: one row per product-supplier, sorted by price ascending
  const rows = results
    .flatMap((item) =>
      (item.suppliers || []).map((sup) => ({
        productName: item.product_name,
        maker: item.maker,
        unit: item.unit,
        insuranceCode: item.insurance_code,
        supplier: sup.name,
        quantity: sup.quantity,
        price: sup.price,
        productId: sup.product_id,
      }))
    )
    .sort((a, b) => a.price - b.price);

  const handleQuantityChange = (key, value) => {
    setQuantities((prev) => ({ ...prev, [key]: value }));
  };

  const handleOrder = async (row) => {
    const key = `${row.supplier}-${row.productId}`;
    const qty = parseInt(quantities[key], 10) || 1;

    setOrderStatus((prev) => ({ ...prev, [key]: { loading: true } }));

    try {
      const { data } = await client.post('/orders', {
        supplier: row.supplier,
        product_id: row.productId,
        product_name: row.productName,
        quantity: qty,
      });

      setOrderStatus((prev) => ({
        ...prev,
        [key]: { loading: false, success: true, message: data.message || '주문 완료' },
      }));
    } catch (err) {
      setOrderStatus((prev) => ({
        ...prev,
        [key]: {
          loading: false,
          success: false,
          message: err.response?.data?.detail || '주문 실패',
        },
      }));
    }
  };

  return (
    <div>
      <form onSubmit={handleSearch} style={{ display: 'flex', gap: '0.5rem', marginBottom: '1.25rem' }}>
        <input
          type="search"
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          placeholder="제품명 또는 보험코드 검색"
          style={{ flex: 1 }}
        />
        <button type="submit" className="btn-primary" disabled={loading}>
          {loading ? <span className="loading-spinner" /> : '검색'}
        </button>
      </form>

      {error && <div className="error-box" style={{ marginBottom: '1rem' }}>{error}</div>}

      {loading && (
        <div className="empty-state">
          <span className="loading-spinner" style={{ width: '2rem', height: '2rem' }} />
          <p style={{ marginTop: '0.75rem' }}>도매상 검색 중...</p>
        </div>
      )}

      {!loading && rows.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>제품명</th>
              <th>단위</th>
              <th>보험코드</th>
              <th>도매상</th>
              <th className="text-right">재고</th>
              <th className="text-right">가격</th>
              <th>수량</th>
              <th>주문</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const key = `${row.supplier}-${row.productId}`;
              const status = orderStatus[key];
              return (
                <tr key={key}>
                  <td>
                    {row.productName}
                    {row.maker && (
                      <span className="text-secondary" style={{ fontSize: '0.78rem', display: 'block' }}>
                        {row.maker}
                      </span>
                    )}
                  </td>
                  <td>{row.unit}</td>
                  <td>{row.insuranceCode}</td>
                  <td>{row.supplier}</td>
                  <td className="text-right">{row.quantity != null ? row.quantity : '-'}</td>
                  <td className="text-right">{row.price != null ? row.price.toLocaleString() + '원' : '-'}</td>
                  <td>
                    <input
                      type="number"
                      min="1"
                      value={quantities[key] || ''}
                      onChange={(e) => handleQuantityChange(key, e.target.value)}
                      placeholder="1"
                      style={{ width: '4rem' }}
                    />
                  </td>
                  <td>
                    {status?.loading ? (
                      <span className="loading-spinner" />
                    ) : status?.success === true ? (
                      <span className="text-success">{status.message}</span>
                    ) : status?.success === false ? (
                      <span className="text-error">{status.message}</span>
                    ) : (
                      <button className="btn-primary btn-sm" onClick={() => handleOrder(row)}>
                        주문
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      {!loading && !error && results.length === 0 && keyword && (
        <div className="empty-state">검색 결과가 없습니다.</div>
      )}

      {!loading && !keyword && results.length === 0 && (
        <div className="empty-state">
          <p>제품명 또는 보험코드로 도매상 재고를 검색하세요.</p>
        </div>
      )}
    </div>
  );
}
