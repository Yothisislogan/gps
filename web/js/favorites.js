const KEY = 'nicanav.favorites';
export function favorites() {
  try { const value = JSON.parse(localStorage.getItem(KEY) || '{}'); return value && typeof value === 'object' ? value : {}; }
  catch { return {}; }
}
export function saveFavorite(kind, place) {
  if (!['home', 'work'].includes(kind) || !Number.isFinite(place.lat) || !Number.isFinite(place.lon)) return false;
  try { localStorage.setItem(KEY, JSON.stringify({ ...favorites(), [kind]: {
    name: place.name || '', lat: place.lat, lon: place.lon, confirmed: Boolean(place.confirmed),
  } })); return true; } catch { return false; }
}
