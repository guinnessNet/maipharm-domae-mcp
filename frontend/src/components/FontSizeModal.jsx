import { setFontSize } from '../utils/fontSize';

export default function FontSizeModal({ onClose }) {
  const handleSelect = (size) => {
    setFontSize(size);
    onClose();
  };

  return (
    <div className="font-modal-overlay">
      <div className="font-modal-content">
        <h2 className="font-modal-title">글씨 크기를 선택해주세요</h2>
        <p className="font-modal-subtitle">설정에서 언제든 변경할 수 있습니다</p>

        <div className="font-modal-options">
          <button
            className="font-option"
            onClick={() => handleSelect('normal')}
          >
            <span className="font-option-label">보통 글씨</span>
            <span className="font-preview" style={{ fontSize: '16px' }}>
              의약품 통합검색 도매 주문
            </span>
          </button>

          <button
            className="font-option"
            onClick={() => handleSelect('large')}
          >
            <span className="font-option-label">큰 글씨</span>
            <span className="font-preview" style={{ fontSize: '20px' }}>
              의약품 통합검색 도매 주문
            </span>
          </button>
        </div>
      </div>
    </div>
  );
}
