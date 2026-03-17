const FAV_KEY = 'domae-favorites';

export const getFavorites = () => JSON.parse(localStorage.getItem(FAV_KEY) || '[]');

export const addFavorite = (item) => {
  const favs = getFavorites();
  // Deduplicate by supplier + productId
  const exists = favs.some(f => f.supplier === item.supplier && f.productId === item.productId);
  if (exists) return favs;
  favs.push({ ...item, addedAt: Date.now() });
  localStorage.setItem(FAV_KEY, JSON.stringify(favs));
  return favs;
};

export const removeFavorite = (index) => {
  const favs = getFavorites();
  favs.splice(index, 1);
  localStorage.setItem(FAV_KEY, JSON.stringify(favs));
  return favs;
};

export const updateFavoriteQuantity = (index, quantity) => {
  const favs = getFavorites();
  if (favs[index]) {
    favs[index].quantity = quantity;
    localStorage.setItem(FAV_KEY, JSON.stringify(favs));
  }
  return favs;
};

export const updateFavoritePrice = (index, price) => {
  const favs = getFavorites();
  if (favs[index]) {
    favs[index].price = price;
    favs[index].priceUpdatedAt = Date.now();
    localStorage.setItem(FAV_KEY, JSON.stringify(favs));
  }
  return favs;
};

export const clearFavorites = () => {
  localStorage.removeItem(FAV_KEY);
  return [];
};
