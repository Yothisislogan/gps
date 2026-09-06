/**
 * Shared UI primitives: safe DOM building, the bottom sheet, toasts, spinners,
 * icons, Spanish/English strings and the formatters everything else reuses.
 *
 * Nothing in this file touches `innerHTML`.  Place names, addresses and report
 * notes all come from user or API data, and the only way to be sure none of it
 * is ever parsed as markup is to never hand markup to the parser.
 */

/* -------------------------------------------------------------------------- */
/* Language                                                                    */
/* -------------------------------------------------------------------------- */

const LANG_KEY = 'nicanav.lang';

/**
 * Spanish first, English behind a toggle.  The register is Nicaraguan: "vos"
 * where it reads naturally ("probá", "movelo"), never peninsular "vosotros".
 */
const STRINGS = {
  es: {
    'dir.closuresUnavailable': 'No pudimos comprobar los cierres de calles. Revisá las condiciones antes de salir.',
    'dir.avoidUnpaved': 'Evitar caminos de tierra',
    'search.placeholder': 'Buscar un lugar o una dirección',
    'search.cancel': 'Cancelar',
    'search.clear': 'Borrar',
    'search.recent': 'Búsquedas recientes',
    'search.recent.clear': 'Borrar historial',
    'search.places': 'Lugares',
    'search.addresses': 'Direcciones',
    'search.searching': 'Buscando…',
    'search.empty': 'No encontramos nada para «{q}».',
    'search.empty.hint':
      'Probá con el nombre del negocio, o con una dirección como «de la Rotonda El Güegüense, 2c al sur».',
    'search.offline': 'Estás sin conexión.',
    'search.offline.hint': 'Conectate a datos o wifi para buscar. El mapa sigue funcionando.',
    'search.failed': 'No pudimos buscar.',
    'search.chips': 'Categorías',
    'common.retry': 'Reintentar',
    'common.close': 'Cerrar',
    'common.cancel': 'Cancelar',
    'common.back': 'Atrás',
    'common.loading': 'Cargando…',
    'common.copy': 'Copiar',
    'common.copied': 'Copiado',
    'common.open': 'Abrir',
    'common.verified': 'Verificado',
    'common.verifiedOn': 'Verificado el {date}',
    'common.unverified': 'Sin verificar',
    'common.here': 'Acá',
    'common.km': 'km',
    'common.min': 'min',
    'common.confidence': '{n}% de confianza',
    'method.relative': 'Dirección de referencia',
    'method.kmpost': 'Kilómetro de carretera',
    'method.coordinate': 'Coordenadas',
    'method.index': 'Del buscador',
    'method.intersection': 'Esquina',
    'poi.directions': 'Cómo llegar',
    'poi.call': 'Llamar',
    'poi.whatsapp': 'WhatsApp',
    'poi.share': 'Compartir',
    'poi.report': 'Reportar un problema',
    'poi.website': 'Sitio web',
    'poi.facebook': 'Facebook',
    'poi.instagram': 'Instagram',
    'poi.notFound': 'No encontramos ese lugar.',
    'poi.closed': 'Cerrado permanentemente',
    'poi.photoCredit': 'Foto: {credit} · {license}',
    'poi.altNames': 'También conocido como',
    'poi.price': 'Precio',
    'hours.open': 'Abierto ahora',
    'hours.closed': 'Cerrado ahora',
    'hours.unknown': 'Horario no confirmado',
    'hours.raw': 'Horario: {raw}',
    'hours.until': 'cierra {time}',
    'hours.opens': 'abre {time}',
    'dir.title': 'Cómo llegar',
    'dir.from': 'Desde',
    'dir.to': 'Hasta',
    'dir.myLocation': 'Mi ubicación',
    'dir.start': 'Empezar',
    'dir.calculating': 'Calculando la ruta…',
    'dir.alternates': 'Otras rutas',
    'dir.steps': 'Indicaciones',
    'dir.route': 'Ruta {n}',
    'dir.fastest': 'Más rápida',
    'dir.arrive': 'Llegás {time}',
    'dir.noPosition': 'Necesitamos tu ubicación para trazar la ruta. Activá el GPS.',
    'dir.failed': 'No pudimos calcular la ruta.',
    'dir.navUnavailable': 'La navegación no está disponible en este dispositivo.',
    'dir.avoidUnpaved': 'Evitar caminos de tierra',
    'report.title': '¿Qué está pasando acá?',
    'report.via_cerrada': 'Vía cerrada',
    'report.bache': 'Bache',
    'report.sentido_incorrecto': 'Sentido incorrecto',
    'report.negocio_cerrado': 'Negocio cerrado',
    'report.note': 'Detalle (opcional)',
    'report.send': 'Enviar',
    'report.thanks': '¡Gracias! Lo vamos a revisar.',
    'report.failed': 'No pudimos enviar el reporte.',
    'report.noPosition': 'Necesitamos tu ubicación para enviar el reporte. Activá el GPS.',
    'share.title': 'Compartir ubicación',
    'share.link': 'Enlace',
    'share.coords': 'Coordenadas',
    'share.address': 'Dirección',
    'share.addressLoading': 'Buscando cómo se dice esta ubicación…',
    'share.addressFailed': 'No pudimos armar la dirección. Mandá las coordenadas.',
    'share.whatsapp': 'Enviar por WhatsApp',
    'share.system': 'Compartir',
    'share.copyAll': 'Copiar todo',
    'locate.title': 'Mi ubicación',
    'locate.denied': 'No pudimos ubicarte. Activá el GPS y dale permiso al navegador.',
    'locate.searching': 'Buscando tu ubicación…',
    'map.failedTitle': 'El mapa no pudo abrirse',
    'map.failedBody':
      'Tu navegador no pudo iniciar el mapa. Probá con Chrome actualizado, o cerrá otras pestañas y recargá.',
    'map.tilesFailed': 'Algunas partes del mapa no cargaron.',
    'map.reload': 'Recargar',
    'settings.title': 'Ajustes',
    'settings.language': 'Idioma',
    'settings.night': 'Modo noche',
    'settings.night.auto': 'Automático',
    'settings.night.day': 'Día',
    'settings.night.night': 'Noche',
    'settings.attribution': 'Datos y licencias',
  },
  en: {
    'dir.closuresUnavailable': 'Road closures could not be checked. Check conditions before leaving.',
    'dir.avoidUnpaved': 'Avoid unpaved roads',
    'search.placeholder': 'Search a place or an address',
    'search.cancel': 'Cancel',
    'search.clear': 'Clear',
    'search.recent': 'Recent searches',
    'search.recent.clear': 'Clear history',
    'search.places': 'Places',
    'search.addresses': 'Addresses',
    'search.searching': 'Searching…',
    'search.empty': 'Nothing found for “{q}”.',
    'search.empty.hint':
      'Try the business name, or an address like “de la Rotonda El Güegüense, 2c al sur”.',
    'search.offline': 'You are offline.',
    'search.offline.hint': 'Connect to data or wifi to search. The map still works.',
    'search.failed': 'Search failed.',
    'search.chips': 'Categories',
    'common.retry': 'Retry',
    'common.close': 'Close',
    'common.cancel': 'Cancel',
    'common.back': 'Back',
    'common.loading': 'Loading…',
    'common.copy': 'Copy',
    'common.copied': 'Copied',
    'common.open': 'Open',
    'common.verified': 'Verified',
    'common.verifiedOn': 'Verified on {date}',
    'common.unverified': 'Not verified',
    'common.here': 'Here',
    'common.km': 'km',
    'common.min': 'min',
    'common.confidence': '{n}% confidence',
    'method.relative': 'Landmark address',
    'method.kmpost': 'Highway kilometre',
    'method.coordinate': 'Coordinates',
    'method.index': 'From the index',
    'method.intersection': 'Intersection',
    'poi.directions': 'Directions',
    'poi.call': 'Call',
    'poi.whatsapp': 'WhatsApp',
    'poi.share': 'Share',
    'poi.report': 'Report a problem',
    'poi.website': 'Website',
    'poi.facebook': 'Facebook',
    'poi.instagram': 'Instagram',
    'poi.notFound': 'We could not find that place.',
    'poi.closed': 'Permanently closed',
    'poi.photoCredit': 'Photo: {credit} · {license}',
    'poi.altNames': 'Also known as',
    'poi.price': 'Price',
    'hours.open': 'Open now',
    'hours.closed': 'Closed now',
    'hours.unknown': 'Hours not confirmed',
    'hours.raw': 'Hours: {raw}',
    'hours.until': 'closes {time}',
    'hours.opens': 'opens {time}',
    'dir.title': 'Directions',
    'dir.from': 'From',
    'dir.to': 'To',
    'dir.myLocation': 'My location',
    'dir.start': 'Start',
    'dir.calculating': 'Calculating the route…',
    'dir.alternates': 'Other routes',
    'dir.steps': 'Steps',
    'dir.route': 'Route {n}',
    'dir.fastest': 'Fastest',
    'dir.arrive': 'Arrive {time}',
    'dir.noPosition': 'We need your location to build the route. Turn on GPS.',
    'dir.failed': 'We could not calculate the route.',
    'dir.navUnavailable': 'Navigation is not available on this device.',
    'dir.avoidUnpaved': 'Avoid dirt roads',
    'report.title': 'What is happening here?',
    'report.via_cerrada': 'Road closed',
    'report.bache': 'Pothole',
    'report.sentido_incorrecto': 'Wrong one-way',
    'report.negocio_cerrado': 'Business closed',
    'report.note': 'Detail (optional)',
    'report.send': 'Send',
    'report.thanks': 'Thank you! We will review it.',
    'report.failed': 'We could not send the report.',
    'report.noPosition': 'We need your location to send the report. Turn on GPS.',
    'share.title': 'Share location',
    'share.link': 'Link',
    'share.coords': 'Coordinates',
    'share.address': 'Address',
    'share.addressLoading': 'Working out how this place is described…',
    'share.addressFailed': 'We could not build the address. Send the coordinates.',
    'share.whatsapp': 'Send on WhatsApp',
    'share.system': 'Share',
    'share.copyAll': 'Copy everything',
    'locate.title': 'My location',
    'locate.denied': 'We could not locate you. Turn on GPS and allow the browser.',
    'locate.searching': 'Finding your location…',
    'map.failedTitle': 'The map could not start',
    'map.failedBody':
      'Your browser could not start the map. Try an up-to-date Chrome, or close other tabs and reload.',
    'map.tilesFailed': 'Some parts of the map did not load.',
    'map.reload': 'Reload',
    'settings.title': 'Settings',
    'settings.language': 'Language',
    'settings.night': 'Night mode',
    'settings.night.auto': 'Automatic',
    'settings.night.day': 'Day',
    'settings.night.night': 'Night',
    'settings.attribution': 'Data and licences',
  },
};

let language = readStoredLanguage();
const languageListeners = new Set();

/** @returns {'es'|'en'} */
function readStoredLanguage() {
  try {
    const stored = localStorage.getItem(LANG_KEY);
    if (stored === 'es' || stored === 'en') return stored;
  } catch {
    /* private mode / storage disabled — Spanish is the right default anyway */
  }
  return 'es';
}

/** @returns {'es'|'en'} the active UI language */
export function getLanguage() {
  return language;
}

/**
 * Switch the UI language and notify everything that renders strings.
 * @param {'es'|'en'} next
 */
export function setLanguage(next) {
  if (next !== 'es' && next !== 'en') return;
  language = next;
  try {
    localStorage.setItem(LANG_KEY, next);
  } catch {
    /* nothing to do: the choice just will not survive a reload */
  }
  document.documentElement.lang = next;
  for (const listener of languageListeners) listener(next);
}

/**
 * Subscribe to language changes.
 * @param {(lang: 'es'|'en') => void} listener
 * @returns {() => void} unsubscribe
 */
export function onLanguageChange(listener) {
  languageListeners.add(listener);
  return () => languageListeners.delete(listener);
}

/**
 * Translate a key, substituting `{name}` placeholders.
 * Unknown keys return the key itself, which is ugly on screen and therefore
 * gets noticed and fixed, unlike an empty string.
 *
 * @param {string} key
 * @param {Record<string, string|number>} [params]
 * @returns {string}
 */
export function t(key, params) {
  const table = STRINGS[language] || STRINGS.es;
  let value = table[key] ?? STRINGS.es[key] ?? key;
  if (params) {
    for (const [name, replacement] of Object.entries(params)) {
      value = value.split(`{${name}}`).join(String(replacement));
    }
  }
  return value;
}

/* -------------------------------------------------------------------------- */
/* DOM                                                                         */
/* -------------------------------------------------------------------------- */

/**
 * Create a text node.  Exported so callers never reach for `innerHTML`.
 * @param {string|number} value
 * @returns {Text}
 */
export function text(value) {
  return document.createTextNode(String(value ?? ''));
}

/**
 * Build an element.
 *
 * Property names are used as DOM properties when one exists (`className`,
 * `href`, `disabled`, `value`), as event listeners when they look like
 * `onClick`, and as attributes otherwise (`aria-label`, `data-id`, `role`).
 *
 * @param {string} tag
 * @param {Record<string, any>|null} [props]
 * @param {any} [children] a node, a string, or a nested array of either
 * @returns {HTMLElement}
 */
export function el(tag, props = null, children = null) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') {
      node.className = String(value);
    } else if (key === 'text') {
      node.textContent = String(value);
    } else if (key === 'dataset') {
      Object.assign(node.dataset, value);
    } else if (key === 'style' && typeof value === 'object') {
      Object.assign(node.style, value);
    } else if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key.includes('-') || key === 'role') {
      node.setAttribute(key, String(value));
    } else if (key in node) {
      // @ts-ignore - deliberate dynamic property assignment
      node[key] = value;
    } else {
      node.setAttribute(key, String(value));
    }
  }
  append(node, children);
  return node;
}

/**
 * Append a node, a string, or a nested array of them, skipping empties.
 * @param {Node} parent
 * @param {any} children
 */
export function append(parent, children) {
  if (children === null || children === undefined || children === false) return;
  if (Array.isArray(children)) {
    for (const child of children) append(parent, child);
    return;
  }
  parent.appendChild(children instanceof Node ? children : text(children));
}

/**
 * Remove every child of a node.
 * @param {Element} node
 * @returns {Element} the same node, for chaining
 */
export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

/* -------------------------------------------------------------------------- */
/* Icons                                                                       */
/* -------------------------------------------------------------------------- */

/**
 * 24×24 stroke paths.  Stroked rather than filled so one set works on the light
 * map and on the night theme by inheriting `currentColor`, and so they stay
 * legible at the 22 px the list rows use.
 */
const ICON_PATHS = {
  search: ['M11 4a7 7 0 100 14 7 7 0 000-14z', 'M16.2 16.2L21 21'],
  close: ['M6 6l12 12', 'M18 6L6 18'],
  back: ['M20 12H4', 'M10 6l-6 6 6 6'],
  locate: ['M12 8a4 4 0 100 8 4 4 0 000-8z', 'M12 2v3', 'M12 19v3', 'M2 12h3', 'M19 12h3'],
  directions: ['M3 11.5L21 3l-8.5 18-2.2-7.3z'],
  phone: [
    'M6.4 3h3l1.5 4-2 1.4a12.5 12.5 0 006.7 6.7l1.4-2 4 1.5v3a2 2 0 01-2.2 2A16.6 16.6 0 014.4 5.2 2 2 0 016.4 3z',
  ],
  whatsapp: ['M12 3a9 9 0 00-7.7 13.6L3 21l4.5-1.3A9 9 0 1012 3z', 'M8.8 8.6c.6 2.6 2.7 4.7 5.3 5.3l1-1.4 2 .9v1.4c-3.2.6-7.2-3.4-6.6-6.6h1.4l.9 2z'],
  share: ['M4 12v7a2 2 0 002 2h12a2 2 0 002-2v-7', 'M12 3v13', 'M8 7l4-4 4 4'],
  report: ['M12 3.2L21.4 20H2.6L12 3.2z', 'M12 9.5v4.5', 'M12 17.2v.4'],
  clock: ['M12 3a9 9 0 100 18 9 9 0 000-18z', 'M12 7.2V12l3.4 2'],
  check: ['M4 12.5l5.2 5.2L20 6.9'],
  chevron: ['M9 5l7 7-7 7'],
  globe: ['M12 3a9 9 0 100 18 9 9 0 000-18z', 'M3 12h18', 'M12 3c3.2 3.6 3.2 14.4 0 18', 'M12 3c-3.2 3.6-3.2 14.4 0 18'],
  facebook: ['M15 3h-2.6A4.4 4.4 0 008 7.4V10H5.5v4H8v7h4v-7h3l1-4h-4V8a1 1 0 011-1h2V3z'],
  instagram: [
    'M7.2 3h9.6A4.2 4.2 0 0121 7.2v9.6A4.2 4.2 0 0116.8 21H7.2A4.2 4.2 0 013 16.8V7.2A4.2 4.2 0 017.2 3z',
    'M12 8.4a3.6 3.6 0 100 7.2 3.6 3.6 0 000-7.2z',
    'M17.3 6.6v.4',
  ],
  settings: [
    'M12 9.4a2.6 2.6 0 100 5.2 2.6 2.6 0 000-5.2z',
    'M12 2.5v2.6', 'M12 18.9v2.6', 'M2.5 12h2.6', 'M18.9 12h2.6',
    'M5.2 5.2l1.9 1.9', 'M16.9 16.9l1.9 1.9', 'M18.8 5.2l-1.9 1.9', 'M7.1 16.9l-1.9 1.9',
  ],
  night: ['M20.5 14.6A8.6 8.6 0 019.4 3.5a8.6 8.6 0 1011.1 11.1z'],
  pin: ['M12 21.4S19 14.9 19 10a7 7 0 10-14 0c0 4.9 7 11.4 7 11.4z', 'M12 7.6a2.5 2.5 0 100 5 2.5 2.5 0 000-5z'],
  // Taxonomy groups, keyed by docs/taxonomy.csv `group`.
  comida: ['M6.5 3v6a2 2 0 004 0V3', 'M8.5 11v10', 'M16.5 3c-1.7 1.4-2.3 3.2-2.3 4.9 0 1.7 1 2.6 2.3 2.6V21'],
  compras: ['M5.5 8h13l-1.2 12.5H6.7L5.5 8z', 'M9 8V6.2a3 3 0 016 0V8'],
  auto: [
    'M3.2 13.5l1.9-5.2A2.4 2.4 0 017.4 6.7h9.2a2.4 2.4 0 012.3 1.6l1.9 5.2',
    'M3.2 13.5h17.6v4.2H3.2z', 'M6.4 17.7v1.9', 'M17.6 17.7v1.9', 'M6.9 15.6h.4', 'M16.7 15.6h.4',
  ],
  dinero: ['M3 6.8h18v10.4H3z', 'M12 9.6a2.4 2.4 0 100 4.8 2.4 2.4 0 000-4.8z'],
  salud: ['M9.8 3h4.4v6.8H21v4.4h-6.8V21H9.8v-6.8H3V9.8h6.8z'],
  turismo: ['M2.5 19.5l6.2-9.2 4.1 5.6 2.6-3.6 6.1 7.2z', 'M8 6.6a1.6 1.6 0 100 3.2 1.6 1.6 0 000-3.2z'],
  servicios: null, // resolved to `settings` below
  transporte: ['M5 4.5h14v10.5H5z', 'M5 15v3.5h3.2V15', 'M15.8 15v3.5H19V15', 'M5 10.2h14', 'M8.5 12.6h.3', 'M15.2 12.6h.3'],
  referencia: null, // resolved to `pin` below
  otro: ['M12 4.2a7.8 7.8 0 100 15.6 7.8 7.8 0 000-15.6z', 'M12 11.4v.4'],
};
ICON_PATHS.servicios = ICON_PATHS.settings;
ICON_PATHS.referencia = ICON_PATHS.pin;

const SVG_NS = 'http://www.w3.org/2000/svg';

/**
 * An inline SVG icon that inherits `currentColor`.
 *
 * Inline SVG rather than the map sprite sheet: the sprite is a PNG atlas built
 * for MapLibre and reading it from CSS would mean fetching and parsing its JSON
 * before the first list row could render.
 *
 * @param {string} name a key of ICON_PATHS; unknown names fall back to `otro`
 * @param {number} [size]
 * @returns {SVGElement}
 */
export function icon(name, size = 22) {
  const paths = ICON_PATHS[name] || ICON_PATHS.otro;
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', String(size));
  svg.setAttribute('height', String(size));
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.7');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  for (const definition of paths) {
    const path = document.createElementNS(SVG_NS, 'path');
    path.setAttribute('d', definition);
    svg.appendChild(path);
  }
  return svg;
}

/* -------------------------------------------------------------------------- */
/* Formatting                                                                  */
/* -------------------------------------------------------------------------- */

/**
 * Distance as a driver reads it: rounded metres up to a kilometre, then km.
 * @param {number|null|undefined} metres
 * @returns {string} e.g. `"350 m"`, `"1,4 km"`, `"12 km"`
 */
export function formatDistance(metres) {
  if (metres === null || metres === undefined || !Number.isFinite(metres)) return '';
  if (metres < 950) {
    const step = metres < 100 ? 10 : 50;
    return `${Math.max(step, Math.round(metres / step) * step)} m`;
  }
  const km = metres / 1000;
  const digits = km < 10 ? 1 : 0;
  return `${formatNumber(km, digits)} ${t('common.km')}`;
}

/**
 * Duration in whole minutes, or hours and minutes past an hour.
 * @param {number|null|undefined} seconds
 * @returns {string}
 */
export function formatDuration(seconds) {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return '';
  const total = Math.max(1, Math.round(seconds / 60));
  if (total < 60) return `${total} ${t('common.min')}`;
  const hours = Math.floor(total / 60);
  const minutes = total % 60;
  return minutes ? `${hours} h ${minutes} ${t('common.min')}` : `${hours} h`;
}

/**
 * A decimal with the separator the active locale uses (Spanish uses a comma).
 * @param {number} value
 * @param {number} digits
 * @returns {string}
 */
export function formatNumber(value, digits = 1) {
  try {
    return value.toLocaleString(language === 'en' ? 'en-US' : 'es-NI', {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
  } catch {
    return value.toFixed(digits);
  }
}

/**
 * Wall-clock time, 24-hour, as every Nicaraguan schedule is written.
 * @param {Date} date
 * @returns {string} e.g. `"17:30"`
 */
export function formatClock(date) {
  const hours = String(date.getHours()).padStart(2, '0');
  const minutes = String(date.getMinutes()).padStart(2, '0');
  return `${hours}:${minutes}`;
}

/**
 * A date for the "verificado" badge.
 * @param {string|Date|null|undefined} value ISO string or Date
 * @returns {string} empty string when the value is unusable
 */
export function formatDate(value) {
  if (!value) return '';
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  try {
    return date.toLocaleDateString(language === 'en' ? 'en-GB' : 'es-NI', {
      day: 'numeric',
      month: 'short',
      year: 'numeric',
    });
  } catch {
    return date.toISOString().slice(0, 10);
  }
}

/** @returns {boolean} the browser's own opinion, which is optimistic but free */
export function isOffline() {
  return typeof navigator !== 'undefined' && navigator.onLine === false;
}

/* -------------------------------------------------------------------------- */
/* Toasts                                                                      */
/* -------------------------------------------------------------------------- */

/**
 * A transient message at the bottom of the screen, optionally with one action.
 *
 * Errors always get one of these: a failure that only reaches the console is a
 * blank screen to a driver on the Carretera a Masaya.
 *
 * @param {string} message
 * @param {{kind?: 'info'|'error'|'success', actionLabel?: string,
 *          onAction?: () => void, durationMs?: number}} [options]
 * @returns {() => void} dismisses the toast early
 */
export function toast(message, options = {}) {
  const host = document.getElementById('toasts');
  if (!host) return () => {};
  const { kind = 'info', actionLabel, onAction, durationMs = kind === 'error' ? 7000 : 3500 } = options;

  // More than two stacked toasts is noise; drop the oldest.
  while (host.children.length >= 2) host.removeChild(host.firstChild);

  let timer = 0;
  const node = el('div', { class: `toast toast--${kind}`, role: 'status', 'aria-live': 'polite' }, [
    el('span', { class: 'toast-text', text: message }),
    actionLabel && onAction
      ? el('button', {
          class: 'toast-action',
          type: 'button',
          text: actionLabel,
          onClick: () => {
            dismiss();
            onAction();
          },
        })
      : null,
  ]);

  const dismiss = () => {
    clearTimeout(timer);
    if (node.parentNode) node.parentNode.removeChild(node);
  };

  host.appendChild(node);
  timer = setTimeout(dismiss, durationMs);
  return dismiss;
}

/**
 * A labelled spinner block for panels that are waiting on the network.
 * @param {string} [label]
 * @returns {HTMLElement}
 */
export function spinner(label = t('common.loading')) {
  return el('div', { class: 'spinner', role: 'status', 'aria-live': 'polite' }, [
    el('span', { class: 'spinner-ring', 'aria-hidden': 'true' }),
    el('span', { class: 'spinner-label', text: label }),
  ]);
}

/**
 * The standard "it broke, here is what to do" block.
 * @param {string} message
 * @param {(() => void)|null} [onRetry]
 * @param {string} [hint]
 * @returns {HTMLElement}
 */
export function errorBlock(message, onRetry = null, hint = '') {
  return el('div', { class: 'error-block', role: 'alert' }, [
    el('p', { class: 'error-message', text: message }),
    hint ? el('p', { class: 'error-hint', text: hint }) : null,
    onRetry
      ? el('button', { class: 'btn btn--primary', type: 'button', text: t('common.retry'), onClick: onRetry })
      : null,
  ]);
}

/* -------------------------------------------------------------------------- */
/* Bottom sheet                                                                */
/* -------------------------------------------------------------------------- */

/**
 * The one bottom sheet in the app.
 *
 * Deliberately *not* modal: the map has to stay visible and pannable while a
 * place card is open, because the first thing anyone does with a result is look
 * at where it is.  It closes on the grip, on Escape, on a downward drag, and on
 * a tap on the map.
 */
class Sheet {
  constructor() {
    /** @type {HTMLElement|null} */
    this.root = null;
    /** @type {HTMLElement|null} */
    this.body = null;
    /** @type {HTMLElement|null} */
    this.titleNode = null;
    /** @type {(() => void)|null} */
    this.closeHandler = null;
    this.dragStartY = 0;
    this.dragOffset = 0;
    this.dragging = false;
  }

  /** Bind to the markup in index.html.  Safe to call more than once. */
  mount() {
    if (this.root) return;
    const root = document.getElementById('sheet');
    if (!root) return;
    this.root = root;
    this.body = root.querySelector('.sheet-body');
    this.titleNode = root.querySelector('.sheet-title');

    const closeButton = root.querySelector('.sheet-close');
    if (closeButton) closeButton.addEventListener('click', () => this.close());

    const grip = root.querySelector('.sheet-grip');
    if (grip) {
      grip.addEventListener('pointerdown', (event) => this.onDragStart(event));
      grip.addEventListener('pointermove', (event) => this.onDragMove(event));
      grip.addEventListener('pointerup', (event) => this.onDragEnd(event));
      grip.addEventListener('pointercancel', (event) => this.onDragEnd(event));
    }

    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && this.isOpen()) this.close();
    });
  }

  /** @returns {boolean} */
  isOpen() {
    return Boolean(this.root && !this.root.hidden);
  }

  /**
   * Show content in the sheet, replacing whatever was there.
   * @param {Node} content
   * @param {{title?: string, expanded?: boolean, onClose?: () => void}} [options]
   */
  open(content, options = {}) {
    this.mount();
    if (!this.root || !this.body) return;
    // A previous card's onClose must not fire because a new card replaced it.
    this.closeHandler = options.onClose || null;
    if (this.titleNode) this.titleNode.textContent = options.title || '';
    clear(this.body);
    this.body.appendChild(content);
    this.body.scrollTop = 0;
    this.root.classList.toggle('sheet--full', Boolean(options.expanded));
    this.root.style.transform = '';
    this.root.hidden = false;
    document.body.classList.add('has-sheet');
  }

  /**
   * Replace the sheet's contents without reopening it (used while a card loads).
   * @param {Node} content
   * @param {string} [title]
   */
  setContent(content, title) {
    if (!this.root || !this.body) return this.open(content, { title });
    if (title !== undefined && this.titleNode) this.titleNode.textContent = title;
    clear(this.body);
    this.body.appendChild(content);
    return undefined;
  }

  close() {
    if (!this.root || this.root.hidden) return;
    this.root.hidden = true;
    this.root.style.transform = '';
    this.root.classList.remove('sheet--full');
    if (this.body) clear(this.body);
    document.body.classList.remove('has-sheet');
    const handler = this.closeHandler;
    this.closeHandler = null;
    if (handler) handler();
  }

  /** @param {PointerEvent} event */
  onDragStart(event) {
    if (!this.root) return;
    this.dragging = true;
    this.dragStartY = event.clientY;
    this.dragOffset = 0;
    this.root.classList.add('sheet--dragging');
    /** @type {Element} */ (event.currentTarget).setPointerCapture(event.pointerId);
  }

  /** @param {PointerEvent} event */
  onDragMove(event) {
    if (!this.dragging || !this.root) return;
    this.dragOffset = event.clientY - this.dragStartY;
    // Upward drag expands rather than stretching the sheet past its top.
    const shift = this.dragOffset > 0 ? this.dragOffset : this.dragOffset / 6;
    this.root.style.transform = `translateY(${shift}px)`;
  }

  /** @param {PointerEvent} event */
  onDragEnd(event) {
    if (!this.dragging || !this.root) return;
    this.dragging = false;
    this.root.classList.remove('sheet--dragging');
    this.root.style.transform = '';
    try {
      /** @type {Element} */ (event.currentTarget).releasePointerCapture(event.pointerId);
    } catch {
      /* the pointer was already released */
    }
    if (this.dragOffset > 90) this.close();
    else if (this.dragOffset < -40) this.root.classList.add('sheet--full');
    else if (this.dragOffset > 40) this.root.classList.remove('sheet--full');
  }
}

/** The application's single bottom sheet. */
export const sheet = new Sheet();

/**
 * A row of large tap targets, used by the place card and the report dialog.
 * @param {Array<{icon: string, label: string, onClick: () => void, primary?: boolean,
 *                href?: string, disabled?: boolean}>} actions
 * @returns {HTMLElement}
 */
export function actionRow(actions) {
  return el(
    'div',
    { class: 'action-row' },
    actions.filter(Boolean).map((action) => {
      const children = [icon(action.icon, 22), el('span', { text: action.label })];
      if (action.href) {
        return el(
          'a',
          {
            class: `action${action.primary ? ' action--primary' : ''}`,
            href: action.href,
            rel: 'noopener noreferrer',
            target: action.href.startsWith('http') ? '_blank' : null,
          },
          children,
        );
      }
      return el(
        'button',
        {
          class: `action${action.primary ? ' action--primary' : ''}`,
          type: 'button',
          disabled: Boolean(action.disabled),
          onClick: action.onClick,
        },
        children,
      );
    }),
  );
}
