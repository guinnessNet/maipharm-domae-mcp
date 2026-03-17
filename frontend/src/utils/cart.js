const CART_KEY = 'domae-cart';

export const getCart = () => JSON.parse(localStorage.getItem(CART_KEY) || '[]');

export const addToCart = (item) => {
  const cart = getCart();
  // Check if same supplier + productId already exists — if so, increase quantity
  const existing = cart.findIndex(
    (c) => c.supplier === item.supplier && c.productId === item.productId
  );
  if (existing >= 0) {
    cart[existing].quantity += item.quantity || 1;
  } else {
    cart.push({ ...item, addedAt: Date.now() });
  }
  localStorage.setItem(CART_KEY, JSON.stringify(cart));
  return cart;
};

export const removeFromCart = (index) => {
  const cart = getCart();
  cart.splice(index, 1);
  localStorage.setItem(CART_KEY, JSON.stringify(cart));
  return cart;
};

export const updateQuantity = (index, qty) => {
  const cart = getCart();
  if (cart[index]) {
    cart[index].quantity = qty;
  }
  localStorage.setItem(CART_KEY, JSON.stringify(cart));
  return cart;
};

export const clearCart = () => {
  localStorage.removeItem(CART_KEY);
  return [];
};

export const setCartItems = (items) => {
  localStorage.setItem(CART_KEY, JSON.stringify(items));
  return items;
};

export const getCartCount = () => getCart().length;
