/**
 * Runtime configuration and the category vocabulary.
 *
 * Deployment injects overrides through a plain `window.NICANAV_CONFIG` object
 * written by infra into `/config.js` (see infra/nginx.conf, which serves that
 * one file with `Cache-Control: no-cache`).  That indirection exists so the
 * same static bundle can be pointed at a different host, tile store or CDN
 * mirror without a rebuild — there is no build step to rebuild with.
 *
 * The taxonomy lives here too: it is dependency-free reference data that the
 * search list, the chips and the place card all need, and this is the only
 * module with no imports of its own.
 */

/** @typedef {{apiBase: string, tilesBase: string, styleUrl: string,
 *   styleUrlNight: string|null, center: [number, number], zoom: number,
 *   mgaCenter: [number, number], curationRadiusM: number,
 *   maplibreUrl: string, openingHoursUrl: string,
 *   searchDebounceMs: number, maxRecentSearches: number,
 *   chipRadiusM: number, maxZoom: number}} NicanavConfig */

const DEFAULTS = {
  apiBase: '/api',
  tilesBase: '/tiles',
  styleUrl: '/style/nicanav.json',

  //: A separate dark style is optional.  When it is absent the map canvas is
  //: dimmed in CSS instead — a washed-out map at night is a nuisance, a
  //: blinding one on a windshield mount is a hazard.
  styleUrlNight: null,

  //: Managua, roughly the Metrocentro / Rotonda Rubén Darío area — the point
  //: most first-time users are closest to.  [lon, lat], MapLibre order.
  center: [-86.2504, 12.115],
  zoom: 12.5,
  maxZoom: 19,

  //: Augusto C. Sandino (MGA); centre of the curated circle, see docs/SPEC.md.
  mgaCenter: [-86.1682, 12.1415],
  curationRadiusM: 48300,

  //: MapLibre GL JS v6 is ESM-only — there is no UMD build to <script> in, so
  //: it is imported dynamically and the URL stays overridable for self-hosting.
  //: Keep the <link rel="modulepreload"> in index.html pointing at the same URL.
  maplibreUrl: 'https://unpkg.com/maplibre-gl@6.7.0/dist/maplibre-gl.mjs',

  //: opening_hours.js is loaded only when a card actually carries an
  //: `opening_hours` string.  jsDelivr's `+esm` endpoint inlines the package's
  //: i18next/suncalc dependencies, which a bare ESM entry would leave as
  //: unresolvable bare specifiers.
  openingHoursUrl: 'https://cdn.jsdelivr.net/npm/opening_hours@3.14.0/+esm',

  searchDebounceMs: 150,
  maxRecentSearches: 8,

  //: Category chips search around the driver, not around the map centre.
  chipRadiusM: 5000,
};

/** @type {NicanavConfig} */
export const CONFIG = Object.freeze({
  ...DEFAULTS,
  ...(typeof window !== 'undefined' && window.NICANAV_CONFIG ? window.NICANAV_CONFIG : {}),
});

/**
 * True when the clock says the driver is heading into darkness.
 *
 * Nicaragua sits at 12°N: sunrise and sunset barely move across the year
 * (roughly 05:30 and 17:50), so a fixed local-hour window is accurate to within
 * about twenty minutes and costs nothing — no sun-position library, no network.
 *
 * @param {Date} [now]
 * @returns {boolean}
 */
export function isNight(now = new Date()) {
  const hour = now.getHours();
  return hour >= 18 || hour < 6;
}

/**
 * Canonical categories from docs/taxonomy.csv: `id -> [label_es, label_en, group]`.
 * The ids are stable and shared with the sprite sheet and the search synonyms.
 */
export const CATEGORIES = Object.freeze({
  restaurante: ['Restaurante', 'Restaurant', 'comida'],
  comida_rapida: ['Comida rápida', 'Fast food', 'comida'],
  cafe: ['Café', 'Coffee shop', 'comida'],
  bar: ['Bar', 'Bar', 'comida'],
  comedor: ['Comedor', 'Home-style eatery', 'comida'],
  buffet: ['Buffet', 'Buffet', 'comida'],
  fritanga: ['Fritanga', 'Street grill', 'comida'],
  cafetin: ['Cafetín', 'Snack bar', 'comida'],
  pizzeria: ['Pizzería', 'Pizzeria', 'comida'],
  heladeria: ['Heladería', 'Ice cream shop', 'comida'],
  panaderia: ['Panadería', 'Bakery', 'comida'],
  reposteria: ['Repostería', 'Pastry shop', 'comida'],
  discoteca: ['Discoteca', 'Nightclub', 'comida'],
  pulperia: ['Pulpería', 'Corner store', 'compras'],
  supermercado: ['Supermercado', 'Supermarket', 'compras'],
  mercado: ['Mercado', 'Public market', 'compras'],
  tienda: ['Tienda', 'General store', 'compras'],
  ferreteria: ['Ferretería', 'Hardware store', 'compras'],
  distribuidora: ['Distribuidora', 'Wholesale distributor', 'compras'],
  centro_comercial: ['Centro comercial', 'Shopping mall', 'compras'],
  libreria: ['Librería', 'Bookshop and stationery', 'compras'],
  gasolinera: ['Gasolinera', 'Petrol station', 'auto'],
  taller_mecanico: ['Taller mecánico', 'Car repair shop', 'auto'],
  vulcanizacion: ['Vulcanización', 'Tyre repair', 'auto'],
  lavado_autos: ['Lavado de autos', 'Car wash', 'auto'],
  repuestos: ['Repuestos', 'Auto parts', 'auto'],
  parqueo: ['Parqueo', 'Parking', 'auto'],
  banco: ['Banco', 'Bank', 'dinero'],
  cajero: ['Cajero automático', 'ATM', 'dinero'],
  casa_de_cambio: ['Casa de cambio', 'Currency exchange', 'dinero'],
  remesas: ['Remesas', 'Money transfer', 'dinero'],
  hospital: ['Hospital', 'Hospital', 'salud'],
  clinica: ['Clínica', 'Clinic', 'salud'],
  farmacia: ['Farmacia', 'Pharmacy', 'salud'],
  dentista: ['Dentista', 'Dentist', 'salud'],
  veterinaria: ['Veterinaria', 'Veterinary clinic', 'salud'],
  hotel: ['Hotel', 'Hotel', 'turismo'],
  hostal: ['Hostal', 'Hostel', 'turismo'],
  playa: ['Playa', 'Beach', 'turismo'],
  mirador: ['Mirador', 'Viewpoint', 'turismo'],
  volcan: ['Volcán', 'Volcano', 'turismo'],
  laguna: ['Laguna', 'Crater lagoon', 'turismo'],
  museo: ['Museo', 'Museum', 'turismo'],
  iglesia: ['Iglesia', 'Church', 'turismo'],
  parque: ['Parque', 'Park', 'turismo'],
  sitio_turistico: ['Sitio turístico', 'Tourist attraction', 'turismo'],
  policia: ['Policía', 'Police station', 'servicios'],
  bomberos: ['Bomberos', 'Fire station', 'servicios'],
  correo: ['Correo', 'Post office', 'servicios'],
  embajada: ['Embajada', 'Embassy', 'servicios'],
  universidad: ['Universidad', 'University', 'servicios'],
  escuela: ['Escuela', 'School', 'servicios'],
  gimnasio: ['Gimnasio', 'Gym', 'servicios'],
  salon_belleza: ['Salón de belleza', 'Beauty salon', 'servicios'],
  lavanderia: ['Lavandería', 'Laundry', 'servicios'],
  hotel_paso: ['Hotel de paso', 'Short-stay motel', 'servicios'],
  terminal_buses: ['Terminal de buses', 'Bus terminal', 'transporte'],
  parada_bus: ['Parada de bus', 'Bus stop', 'transporte'],
  aeropuerto: ['Aeropuerto', 'Airport', 'transporte'],
  puerto: ['Puerto', 'Port', 'transporte'],
  taxi: ['Taxi', 'Taxi stand', 'transporte'],
  rotonda: ['Rotonda', 'Roundabout', 'referencia'],
  semaforo: ['Semáforo', 'Traffic light', 'referencia'],
  puente: ['Puente', 'Bridge', 'referencia'],
  monumento: ['Monumento', 'Monument', 'referencia'],
  estadio: ['Estadio', 'Stadium', 'referencia'],
  cementerio: ['Cementerio', 'Cemetery', 'referencia'],
  otro: ['Otro', 'Other', 'otro'],
});

/**
 * Human label for a canonical category id.
 *
 * Unknown ids are echoed back with underscores turned into spaces rather than
 * dropped: the pipeline can add a category before the client knows about it,
 * and showing "car_wash" beats showing nothing.
 *
 * @param {string|null|undefined} id
 * @param {'es'|'en'} [lang]
 * @returns {string}
 */
export function categoryLabel(id, lang = 'es') {
  if (!id) return '';
  const row = CATEGORIES[id];
  if (!row) return String(id).replace(/_/g, ' ');
  return lang === 'en' ? row[1] : row[0];
}

/**
 * The taxonomy group a category belongs to, used to pick a list icon.
 * @param {string|null|undefined} id
 * @returns {string}
 */
export function categoryGroup(id) {
  if (!id) return 'otro';
  const row = CATEGORIES[id];
  return row ? row[2] : 'otro';
}

/**
 * The category chips above the map.
 *
 * `category` is the canonical id sent to `/api/search`.  "Comida" has none:
 * the API filters by a single category id and there is no `group` parameter,
 * so the food chip runs a plain query instead of a filtered one.  If a `group`
 * filter is ever added to /api/search this is the first place to use it.
 */
export const CHIPS = Object.freeze([
  { id: 'comida', es: 'Comida', en: 'Food', query: 'comida', category: null, group: 'comida' },
  { id: 'gasolinera', es: 'Gasolina', en: 'Fuel', query: 'gasolinera', category: 'gasolinera', group: 'auto' },
  { id: 'cajero', es: 'Cajero', en: 'ATM', query: 'cajero', category: 'cajero', group: 'dinero' },
  { id: 'farmacia', es: 'Farmacia', en: 'Pharmacy', query: 'farmacia', category: 'farmacia', group: 'salud' },
  { id: 'hotel', es: 'Hotel', en: 'Hotel', query: 'hotel', category: 'hotel', group: 'turismo' },
  { id: 'vulcanizacion', es: 'Vulcanización', en: 'Tyre repair', query: 'vulcanización', category: 'vulcanizacion', group: 'auto' },
  { id: 'hospital', es: 'Hospital', en: 'Hospital', query: 'hospital', category: 'hospital', group: 'salud' },
  { id: 'supermercado', es: 'Supermercado', en: 'Supermarket', query: 'supermercado', category: 'supermercado', group: 'compras' },
]);
