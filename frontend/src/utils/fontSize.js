const FONT_KEY = 'domae-font-size';

// 'normal' = 16px base (current), 'large' = 20px base
export const getFontSize = () => localStorage.getItem(FONT_KEY) || null; // null = not set yet

export const setFontSize = (size) => {
  localStorage.setItem(FONT_KEY, size);
  applyFontSize(size);
};

export const applyFontSize = (size) => {
  const root = document.documentElement;
  if (size === 'large') {
    root.style.fontSize = '20px';
  } else {
    root.style.fontSize = '16px'; // normal
  }
};
