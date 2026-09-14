/**
 * The map, and the application's entry point.
 *
 * This module owns three things that must happen exactly once and in order:
 * registering the PMTiles protocol, constructing the MapLibre map, and wiring
 * the rest of the app to it.  It is also the file `index.html` loads, so the
 * bootstrap at the bottom is what starts everything.
 *
 * MapLibre GL JS v6 is ESM-only — there is no UMD build to drop in a script tag
 * — so it is imported dynamically from the URL in CONFIG.  That also gives us a
 * place to catch the two failures a mid-range Android actually produces: the
 * CDN not loading at all, and WebGL2 being unavailable.
 */

import { CONFIG, isNight } from './config.js';
import { el, icon, sheet, t, toast, getLanguage, onLanguageChange, setLanguage, clear } from './ui.js';
import { createSearch } from './search.js';
import { openPlaceCard, openPointCard, openDirections, openReport, choosePoint } from './poi.js';
import { parseDeepLink, writeDeepLink, openShareSheet } from './share.js';
import { setupUpdates } from './updates.js';
import { saveFavorite } from './favorites.js';

/** Source and layer ids this module owns; everything else belongs to the style. */
const ROUTE_SOURCE = 'nicanav-route';
const ROUTE_LAYERS = {
  alternates: 'nicanav-route-alternates',
  casing: 'nicanav-route-casing',
  line: 'nicanav-route-line',
};

/** `addProtocol` is global to MapLibre; registering it twice leaks handlers. */
let protocolRegistered = false;
/** @type {any} */
let activeProtocol = null;

/**
 * Register `pmtiles://` with MapLibre.  Must run before any Map is built.
 *
 * The pmtiles IIFE bundle (`window.pmtiles`) is loaded by a plain script tag in
 * index.html rather than as ESM, because the package's ESM entry leaves its
 * `fflate` dependency as a bare specifier that a no-bundler page cannot resolve.
 *
 * @param {any} maplibregl
 * @throws {Error} when the pmtiles bundle did not load
 */
function registerPmtilesProtocol(maplibregl) {
  if (protocolRegistered) return;
  const pmtiles = /** @type {any} */ (window).pmtiles;
  if (!pmtiles || typeof pmtiles.Protocol !== 'function') {
    throw new Error('pmtiles bundle missing');
  }
  // No `{metadata: true}`: it fills the source attribution but costs a blocking
  // HTTP request before the first tile, and our attribution is in the shell.
  const protocol = new pmtiles.Protocol();
  maplibregl.addProtocol('pmtiles', protocol.tile);
  activeProtocol = protocol;
  protocolRegistered = true;
}

/**
 * The registered PMTiles protocol, or null before the map exists.
 *
 * offline.js needs it to hand the protocol an archive backed by Cache Storage
 * instead of the network, which is how offline tiles work at all: a `.pmtiles`
 * served from a cached blob is indistinguishable to MapLibre from one served
 * over HTTP.
 * @returns {any}
 */
export function pmtilesProtocol() {
  return activeProtocol;
}

/**
 * Decode a Valhalla-style encoded polyline.
 *
 * Valhalla uses precision 6, not the precision 5 Google popularised.  Decoding
 * at the wrong one puts the route in the Gulf of Guinea, so `decodeShape`
 * below sanity-checks the result instead of trusting the precision blindly.
 *
 * @param {string} encoded
 * @param {number} [precision]
 * @returns {Array<[number, number]>} `[lon, lat]` pairs, GeoJSON order
 */
export function decodePolyline(encoded, precision = 6) {
  const factor = 10 ** precision;
  /** @type {Array<[number, number]>} */
  const coordinates = [];
  let index = 0;
  let lat = 0;
  let lon = 0;

  while (index < encoded.length) {
    for (let coordinateIndex = 0; coordinateIndex < 2; coordinateIndex += 1) {
      let shift = 0;
      let result = 0;
      let byte;
      do {
        if (index >= encoded.length) return coordinates; // truncated: keep the good prefix
        byte = encoded.charCodeAt(index) - 63;
        index += 1;
        result |= (byte & 0x1f) << shift;
        shift += 5;
      } while (byte >= 0x20);
      const delta = result & 1 ? ~(result >> 1) : result >> 1;
      if (coordinateIndex === 0) lat += delta;
      else lon += delta;
    }
    coordinates.push([lon / factor, lat / factor]);
  }
  return coordinates;
}

/**
 * Decode a shape whose precision we are not certain of.
 *
 * Valhalla's OSRM-format output is documented as polyline6, but plain OSRM is
 * polyline5 and the serializer is configurable.  Rather than guess, decode at 6
 * and check the first point lands somewhere in Nicaragua; if it does not, the
 * archive was polyline5 and the same string decodes correctly at 5.
 *
 * @param {string} encoded
 * @returns {Array<[number, number]>} `[lon, lat]` pairs
 */
export function decodeShape(encoded) {
  const six = decodePolyline(encoded, 6);
  if (six.length && looksLikeNicaragua(six[0])) return six;
  const five = decodePolyline(encoded, 5);
  if (five.length && looksLikeNicaragua(five[0])) return five;
  return six;
}

/**
 * @param {[number, number]} point `[lon, lat]`
 * @returns {boolean} inside the generous national bounding box
 */
function looksLikeNicaragua([lon, lat]) {
  return lon >= -87.75 && lon <= -82.6 && lat >= 10.68 && lat <= 15.05;
}

/**
 * Coerce whatever a caller has — an encoded polyline, a geometry, a feature or
 * a collection — into a FeatureCollection of LineStrings.
 *
 * @param {string|object} input
 * @param {Record<string, any>} [properties] merged into features built from a polyline
 * @returns {any} a GeoJSON FeatureCollection
 */
function toFeatureCollection(input, properties = {}) {
  if (typeof input === 'string') {
    return {
      type: 'FeatureCollection',
      features: [
        { type: 'Feature', properties, geometry: { type: 'LineString', coordinates: decodeShape(input) } },
      ],
    };
  }
  if (!input || typeof input !== 'object') {
    return { type: 'FeatureCollection', features: [] };
  }
  const value = /** @type {any} */ (input);
  if (value.type === 'FeatureCollection') return value;
  if (value.type === 'Feature') return { type: 'FeatureCollection', features: [value] };
  return {
    type: 'FeatureCollection',
    features: [{ type: 'Feature', properties, geometry: value }],
  };
}

/**
 * Every coordinate in a FeatureCollection of lines and points.
 * @param {any} collection
 * @returns {Array<[number, number]>}
 */
function collectCoordinates(collection) {
  /** @type {Array<[number, number]>} */
  const points = [];
  for (const feature of collection.features || []) {
    const geometry = feature && feature.geometry;
    if (!geometry) continue;
    if (geometry.type === 'LineString') points.push(...geometry.coordinates);
    else if (geometry.type === 'MultiLineString') for (const line of geometry.coordinates) points.push(...line);
    else if (geometry.type === 'Point') points.push(geometry.coordinates);
  }
  return points;
}

/**
 * @typedef {object} MapController
 * @property {any} map the raw MapLibre instance
 * @property {(shape: string|object, options?: {fit?: boolean, alternates?: Array<string|object>}) => void} showRoute
 * @property {() => void} clearRoute
 * @property {(id: string, lat: number|null, lon: number|null, options?: object) => any} setMarker
 * @property {(lat: number, lon: number, options?: object) => void} flyTo
 * @property {(on: boolean) => void} setNightMode
 * @property {(input: any, options?: object) => void} fitBounds
 * @property {() => {top: number, bottom: number, left: number, right: number}} padding
 */

/**
 * Build the map.
 *
 * Returns a promise because MapLibre itself is fetched at call time; awaiting it
 * is the only moment in the app that blocks on the network before first paint,
 * which is why nothing else is awaited alongside it.
 *
 * @param {{container: string|HTMLElement, onPoiClick?: (poi: any) => void,
 *          onMapClick?: (point: {lat: number, lon: number}) => void,
 *          onLongPress?: (point: {lat: number, lon: number}) => void,
 *          center?: [number, number], zoom?: number, night?: boolean}} options
 * @returns {Promise<MapController>}
 */
export async function createMap(options) {
  const maplibregl = await import(CONFIG.maplibreUrl);
  registerPmtilesProtocol(maplibregl);

  const night = options.night ?? isNight();
  const styleUrl = night && CONFIG.styleUrlNight ? CONFIG.styleUrlNight : CONFIG.styleUrl;
  let initialStyle = styleUrl;
  let offlineModule = null;
  let onlineStyle = null;
  if (navigator.onLine === false) {
    offlineModule = await import('./offline.js');
    const response = await fetch(styleUrl);
    if (!response.ok) throw new Error('No está guardado el estilo del mapa');
    onlineStyle = await response.json();
    let available = false;
    try { available = await offlineModule.activateOffline(); } catch { /* storage unavailable */ }
    const { offlineStyle } = await import('./offline-style.js');
    initialStyle = offlineStyle(onlineStyle, available);
  }

  if (options.isCurrent && !options.isCurrent()) throw new Error('Map startup superseded');
  let map;
  try {
    map = new maplibregl.Map({
      container: options.container,
      style: initialStyle,
      center: options.center || CONFIG.center,
      zoom: options.zoom ?? CONFIG.zoom,
      maxZoom: CONFIG.maxZoom,
      pixelRatio: Math.min(window.devicePixelRatio || 1, CONFIG.maxPixelRatio || 2),
      maxTileCacheSize: 64,
      cancelPendingTileRequestsWhileZooming: true,
      // The shell draws its own always-visible attribution block, sized to a
      // 44 px tap target; MapLibre's would be a second, smaller copy.
      attributionControl: false,
      // Pitch and rotation are for navigation, which nav.js turns on; a driver
      // fat-fingering a two-finger rotate on a dash mount is pure nuisance.
      pitchWithRotate: false,
      dragRotate: false,
      // A tap that lands 2 px off is still a tap when the phone is on a mount.
      clickTolerance: 5,
      fadeDuration: 100,
    });
  } catch (error) {
    // v6 requires WebGL2 and throws GPUInitializationError instead of returning
    // a map when the device cannot provide it.
    throw new Error(`map init failed: ${error && error.message ? error.message : error}`);
  }

  if (offlineModule) offlineModule.rememberOnlineStyle(map, onlineStyle);

  /** @type {Map<string, any>} */
  const markers = new Map();
  /** @type {any} */
  let routeData = null;
  let nightMode = night;
  /** @type {string[]} */
  let poiLayerIds = [];
  let tileErrorReported = false;

  /** POI layers come from a style this module does not own, so find them by shape. */
  const findPoiLayers = () => {
    try {
      const style = map.getStyle();
      poiLayerIds = (style.layers || [])
        .filter((layer) => {
          if (layer.type !== 'symbol' && layer.type !== 'circle') return false;
          const haystack = `${layer.id} ${layer.source || ''} ${layer['source-layer'] || ''}`.toLowerCase();
          return haystack.includes('poi');
        })
        .map((layer) => layer.id);
    } catch {
      poiLayerIds = [];
    }
  };

  /** The id of the first symbol layer, so route lines slip under the labels. */
  const firstSymbolLayerId = () => {
    try {
      const layers = map.getStyle().layers || [];
      const symbol = layers.find((layer) => layer.type === 'symbol');
      return symbol ? symbol.id : undefined;
    } catch {
      return undefined;
    }
  };

  const routePaint = () => ({
    casing: nightMode ? '#0a2540' : '#0b3d91',
    line: nightMode ? '#5aa2ff' : '#2f7ef7',
    alternate: nightMode ? '#5c6672' : '#9aa5b1',
  });

  /** (Re)create the route source and layers.  Called again after a style swap. */
  const applyRoute = () => {
    if (!routeData) return;
    const colours = routePaint();
    if (map.getSource(ROUTE_SOURCE)) {
      map.getSource(ROUTE_SOURCE).setData(routeData);
    } else {
      map.addSource(ROUTE_SOURCE, { type: 'geojson', data: routeData });
    }
    const before = firstSymbolLayerId();

    if (!map.getLayer(ROUTE_LAYERS.alternates)) {
      map.addLayer(
        {
          id: ROUTE_LAYERS.alternates,
          type: 'line',
          source: ROUTE_SOURCE,
          filter: ['==', ['get', 'alternate'], true],
          layout: { 'line-join': 'round', 'line-cap': 'round' },
          paint: {
            'line-color': colours.alternate,
            'line-width': ['interpolate', ['linear'], ['zoom'], 10, 3, 16, 7],
            'line-opacity': 0.75,
          },
        },
        before,
      );
    }
    if (!map.getLayer(ROUTE_LAYERS.casing)) {
      map.addLayer(
        {
          id: ROUTE_LAYERS.casing,
          type: 'line',
          source: ROUTE_SOURCE,
          filter: ['!=', ['get', 'alternate'], true],
          layout: { 'line-join': 'round', 'line-cap': 'round' },
          paint: {
            'line-color': colours.casing,
            'line-width': ['interpolate', ['linear'], ['zoom'], 10, 8, 16, 16],
          },
        },
        before,
      );
    }
    if (!map.getLayer(ROUTE_LAYERS.line)) {
      map.addLayer(
        {
          id: ROUTE_LAYERS.line,
          type: 'line',
          source: ROUTE_SOURCE,
          filter: ['!=', ['get', 'alternate'], true],
          layout: { 'line-join': 'round', 'line-cap': 'round' },
          paint: {
            'line-color': colours.line,
            'line-width': ['interpolate', ['linear'], ['zoom'], 10, 5, 16, 11],
          },
        },
        before,
      );
    }
  };

  map.on('style.load', () => {
    findPoiLayers();
    applyRoute();
  });

  map.on('error', (event) => {
    // Tile and sprite failures are normal on a flaky mobile connection; report
    // once so the user knows the map is incomplete, then stay quiet.
    if (tileErrorReported) return;
    tileErrorReported = true;
    console.warn('map error', event && event.error);
    toast(t('map.tilesFailed'), { kind: 'error' });
  });

  map.on('click', (event) => {
    const point = { lat: event.lngLat.lat, lon: event.lngLat.lng };
    if (poiLayerIds.length && options.onPoiClick) {
      let features = [];
      try {
        features = map.queryRenderedFeatures(event.point, { layers: poiLayerIds });
      } catch {
        features = [];
      }
      const feature = features[0];
      if (feature) {
        const properties = feature.properties || {};
        const geometry = feature.geometry || {};
        const coordinates = geometry.type === 'Point' ? geometry.coordinates : [point.lon, point.lat];
        options.onPoiClick({
          id: properties.id || properties.poi_id || properties.osm_id || null,
          name: properties.name || properties['name:es'] || '',
          category: properties.category || null,
          lat: coordinates[1],
          lon: coordinates[0],
        });
        return;
      }
    }
    if (options.onMapClick) options.onMapClick(point);
  });

  // Long-press on a touch screen fires `contextmenu` in Chrome for Android; this
  // is the "what is here?" gesture that makes any point shareable.
  // UNVERIFIED: confirm the long-press duration and that it fires on a dash
  // mount with a screen protector, on a real Android device.
  map.on('contextmenu', (event) => {
    if (options.onLongPress) options.onLongPress({ lat: event.lngLat.lat, lon: event.lngLat.lng });
  });

  // The controls do not depend on tiles. Never hold the app behind map load.
  if (map.loaded()) options.onReady?.();
  else map.once('load', () => options.onReady?.());

  /**
   * Padding that keeps the route clear of the search bar and the bottom sheet.
   * @returns {{top: number, bottom: number, left: number, right: number}}
   */
  const padding = () => {
    const sheetNode = document.getElementById('sheet');
    const sheetHeight = sheetNode && !sheetNode.hidden ? sheetNode.getBoundingClientRect().height : 0;
    return {
      top: 120,
      bottom: Math.min(Math.round(window.innerHeight * 0.55), sheetHeight + 40) || 80,
      left: 40,
      right: 40,
    };
  };

  /** @type {MapController} */
  const controller = {
    map,

    showRoute(shape, showOptions = {}) {
      const main = toFeatureCollection(shape, { alternate: false });
      const features = [...main.features];
      for (const alternate of showOptions.alternates || []) {
        const collection = toFeatureCollection(alternate, { alternate: true });
        for (const feature of collection.features) {
          features.push({ ...feature, properties: { ...(feature.properties || {}), alternate: true } });
        }
      }
      routeData = { type: 'FeatureCollection', features };
      applyRoute();
      if (showOptions.fit !== false) controller.fitBounds(collectCoordinates(routeData));
    },

    clearRoute() {
      routeData = null;
      for (const id of Object.values(ROUTE_LAYERS)) {
        if (map.getLayer(id)) map.removeLayer(id);
      }
      if (map.getSource(ROUTE_SOURCE)) map.removeSource(ROUTE_SOURCE);
    },

    setMarker(id, lat, lon, markerOptions = {}) {
      const existing = markers.get(id);
      if (lat === null || lon === null || lat === undefined || lon === undefined) {
        if (existing) {
          existing.remove();
          markers.delete(id);
        }
        return null;
      }
      if (existing) {
        existing.setLngLat([lon, lat]);
        return existing;
      }
      const kind = markerOptions.kind || 'result';
      const element = el('div', { class: `marker marker--${kind}`, role: markerOptions.onClick ? 'button' : 'img', tabindex: markerOptions.onClick ? '0' : '-1' }, [
        el('span', { class: 'marker-dot', 'aria-hidden': 'true' }),
        markerOptions.label ? el('span', { class: 'marker-label', text: markerOptions.label }) : null,
      ]);
      element.setAttribute('aria-label', markerOptions.title || markerOptions.label || t('locate.title'));
      if (markerOptions.onClick) {
        element.addEventListener('keydown', (event) => {
          if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); markerOptions.onClick(); }
        });
        element.addEventListener('click', (event) => {
          event.stopPropagation();
          markerOptions.onClick();
        });
      }
      const marker = new maplibregl.Marker({ element, anchor: kind === 'user' ? 'center' : 'bottom' })
        .setLngLat([lon, lat])
        .addTo(map);
      markers.set(id, marker);
      return marker;
    },

    flyTo(lat, lon, flyOptions = {}) {
      map.flyTo({
        center: [lon, lat],
        zoom: flyOptions.zoom ?? Math.max(map.getZoom(), 16),
        padding: flyOptions.padding || padding(),
        // A slow cinematic fly is charming once and irritating while driving.
        duration: window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 0 : (flyOptions.duration ?? 350),
      });
    },

    fitBounds(input, fitOptions = {}) {
      /** @type {Array<[number, number]>} */
      let points = [];
      if (Array.isArray(input)) {
        for (const item of input) {
          if (Array.isArray(item) && item.length === 2) points.push([Number(item[0]), Number(item[1])]);
          else if (item && typeof item === 'object' && 'lat' in item) points.push([Number(item.lon), Number(item.lat)]);
        }
      } else if (input && typeof input === 'object') {
        points = collectCoordinates(toFeatureCollection(input));
      }
      if (points.length < 2) {
        if (points.length === 1) controller.flyTo(points[0][1], points[0][0], fitOptions);
        return;
      }
      let [west, south] = points[0];
      let [east, north] = points[0];
      for (const [lon, lat] of points) {
        west = Math.min(west, lon);
        east = Math.max(east, lon);
        south = Math.min(south, lat);
        north = Math.max(north, lat);
      }
      map.fitBounds(
        [
          [west, south],
          [east, north],
        ],
        { padding: fitOptions.padding || padding(), duration: fitOptions.duration ?? 500, maxZoom: 16 },
      );
    },

    setNightMode(on) {
      const changed = nightMode !== Boolean(on);
      nightMode = Boolean(on);
      document.documentElement.classList.toggle('night', nightMode);
      document.documentElement.dataset.theme = nightMode ? 'night' : 'day';
      const container = map.getContainer();
      if (CONFIG.styleUrlNight) {
        if (changed) map.setStyle(nightMode ? CONFIG.styleUrlNight : CONFIG.styleUrl);
        container.classList.remove('map--dimmed');
      } else {
        // No dark style shipped: dim the canvas instead.  A full-brightness map
        // at night on a windshield mount is genuinely dangerous.
        container.classList.toggle('map--dimmed', nightMode);
      }
      for (const id of [ROUTE_LAYERS.casing, ROUTE_LAYERS.line, ROUTE_LAYERS.alternates]) {
        if (!map.getLayer(id)) continue;
        const colours = routePaint();
        const colour =
          id === ROUTE_LAYERS.casing ? colours.casing : id === ROUTE_LAYERS.line ? colours.line : colours.alternate;
        map.setPaintProperty(id, 'line-color', colour);
      }
    },

    padding,
  };

  controller.setNightMode(nightMode);
  return controller;
}

/* -------------------------------------------------------------------------- */
/* Bootstrap                                                                   */
/* -------------------------------------------------------------------------- */

const THEME_KEY = 'nicanav.theme';

/** @returns {'auto'|'day'|'night'} */
function readThemePreference() {
  try {
    const stored = localStorage.getItem(THEME_KEY);
    if (stored === 'auto' || stored === 'day' || stored === 'night') return stored;
  } catch {
    /* storage disabled; automatic is a fine default */
  }
  return 'auto';
}

/**
 * Live position, shared by search (chips search around the driver), directions
 * and reports.  Kept in this module so exactly one `watchPosition` runs.
 */
const positionState = {
  /** @type {{lat: number, lon: number, accuracy: number, heading: number|null, speed: number|null, at: number}|null} */
  fix: null,
  watchId: 0,
};

/**
 * Start the single geolocation watch.
 * @param {MapController} controller
 */
function startPositionWatch(controller) {
  if (!('geolocation' in navigator) || positionState.watchId) return;
  positionState.watchId = navigator.geolocation.watchPosition(
    (position) => {
      positionState.fix = {
        lat: position.coords.latitude,
        lon: position.coords.longitude,
        accuracy: position.coords.accuracy,
        heading: Number.isFinite(position.coords.heading) ? position.coords.heading : null,
        speed: Number.isFinite(position.coords.speed) ? position.coords.speed : null,
        at: Date.now(),
      };
      controller.setMarker('user', positionState.fix.lat, positionState.fix.lon, {
        kind: 'user',
        title: t('locate.title'),
      });
    },
    () => {
      // Errors are deliberately silent here.  The Locate button is where the
      // user asks for a position, so that is where the explanation belongs; a
      // toast on page load would fire in every tunnel and parking garage.
    },
    { enableHighAccuracy: true, timeout: 10000, maximumAge: 5000 },
  );
}

/** @returns {{lat: number, lon: number, accuracy: number, heading: number|null, speed: number|null, at: number}|null} */
function getPosition() {
  return positionState.fix && Date.now() - positionState.fix.at < 30000 ? positionState.fix : null;
}

/**
 * Ask for a fix right now, for the Locate button and for "route from here".
 * @returns {Promise<{lat: number, lon: number}>}
 */
function requestPosition() {
  const recent = positionState.fix && Date.now() - positionState.fix.at < 30000 ? positionState.fix : null;
  if (recent) return Promise.resolve(recent);
  return new Promise((resolve, reject) => {
    if (!('geolocation' in navigator)) {
      reject(new Error('geolocation unavailable'));
      return;
    }
    navigator.geolocation.getCurrentPosition(
      (position) => {
        positionState.fix = { lat: position.coords.latitude, lon: position.coords.longitude, at: Date.now() };
        resolve(positionState.fix);
      },
      (error) => reject(error),
      { enableHighAccuracy: true, timeout: 12000, maximumAge: 0 },
    );
  });
}

/** Replace the map with a legible Spanish explanation instead of a white screen. */
function showMapFallback() {
  const fallback = document.getElementById('map-fallback');
  const container = document.getElementById('map');
  if (container) container.hidden = true;
  if (!fallback) return;
  fallback.hidden = false;
  clear(fallback);
  fallback.appendChild(
    el('div', { class: 'fallback-card' }, [
      el('h1', { text: t('map.failedTitle') }),
      el('p', { text: t('map.failedBody') }),
      el('button', {
        class: 'btn btn--primary',
        type: 'button',
        text: t('map.reload'),
        onClick: () => window.location.reload(),
      }),
    ]),
  );
}

/** Settings sheet: language, night mode, and where the data comes from. */
/**
 * The offline-map section of the settings sheet.
 *
 * Built empty and filled once offline.js reports what is stored, so opening
 * settings never waits on Cache Storage. The size is shown before the download
 * starts: this is a metered mobile connection in a country where data costs
 * real money, and a silent hundred-megabyte fetch is a betrayal.
 */
function offlineRow(controller) {
  const status = el('span', { class: 'muted', text: '…' });
  const bar = el('div', { class: 'progress__bar', style: { width: '0%' } });
  const progress = el('div', { class: 'progress', hidden: true }, [bar]);
  const buttons = el('div', { class: 'segmented' });

  const row = el('div', { class: 'setting setting--stacked' }, [
    el('span', { class: 'setting-label', text: 'Mapa sin conexión' }),
    el('p', { class: 'muted', text: 'El mapa descargado permite ver calles sin señal. Buscar lugares, consultar cierres y calcular rutas nuevas requiere conexión.' }),
    status,
    progress,
    buttons,
  ]);

  import('./offline.js')
    .then(async (offline) => {
      const refresh = async () => {
        const state = await offline.offlineStatus();
        clear(buttons);
        if (state.present) {
          status.textContent = `Descargado: ${(state.bytes / 1e6).toFixed(0)} MB · ${state.storedAt ? new Date(state.storedAt).toLocaleDateString('es-NI') : ''}`;
          buttons.appendChild(
            el('button', {
              class: 'segment',
              type: 'button',
              text: 'Borrar',
              onClick: async () => {
                await offline.clearOffline();
                offline.useOfflineSource(controller?.map, false);
                refresh();
              },
            }),
          );
        } else {
          const size = await offline.offlineSize();
          status.textContent = size
            ? `Managua, Masaya, Granada y Carazo — ${(size / 1e6).toFixed(0)} MB`
            : 'Managua, Masaya, Granada y Carazo';
        }
        buttons.appendChild(
          el('button', {
            class: 'segment segment--on',
            type: 'button',
            text: state.present ? 'Actualizar' : 'Descargar',
            onClick: async (event) => {
              const button = event.currentTarget;
              button.disabled = true;
              progress.hidden = false;
              const abort = new AbortController();
              const cancel = el('button', { type: 'button', class: 'segment', text: t('common.cancel'), onClick: () => abort.abort() });
              buttons.appendChild(cancel);
              try {
                await offline.downloadCircle((fraction) => {
                  bar.style.width = `${Math.round(fraction * 100)}%`;
                  status.textContent = `${Math.round(fraction * 100)}%`;
                }, abort.signal);
                const registration = await navigator.serviceWorker?.getRegistration();
                toast(registration?.active ? 'Mapa descargado' : 'Mapa guardado; la aplicación aún no está lista sin conexión');
                refresh();
              } catch (error) {
                toast(String((error && error.message) || error), { kind: 'error' });
              } finally {
                cancel.remove();
                button.disabled = false;
                progress.hidden = true;
              }
            },
          }),
        );
      };
      await refresh();
    })
    .catch(() => {
      status.textContent = 'No disponible en este navegador';
    });

  return row;
}

function openSettings(controller, applyTheme) {
  const themePreference = readThemePreference();
  const store = (value) => {
    try {
      localStorage.setItem(THEME_KEY, value);
    } catch {
      /* not persisted; the session still honours the choice */
    }
    applyTheme(value);
  };

  const languageRow = el('div', { class: 'setting' }, [
    el('span', { class: 'setting-label', text: t('settings.language') }),
    el('div', { class: 'segmented' }, [
      el('button', {
        class: `segment${getLanguage() === 'es' ? ' segment--on' : ''}`,
        type: 'button',
        text: 'Español',
        onClick: () => setLanguage('es'),
      }),
      el('button', {
        class: `segment${getLanguage() === 'en' ? ' segment--on' : ''}`,
        type: 'button',
        text: 'English',
        onClick: () => setLanguage('en'),
      }),
    ]),
  ]);

  const themeRow = el('div', { class: 'setting' }, [
    el('span', { class: 'setting-label', text: t('settings.night') }),
    el(
      'div',
      { class: 'segmented' },
      ['auto', 'day', 'night'].map((value) =>
        el('button', {
          class: `segment${themePreference === value ? ' segment--on' : ''}`,
          type: 'button',
          text: t(`settings.night.${value}`),
          onClick: () => {
            store(value);
            openSettings(controller, applyTheme);
          },
        }),
      ),
    ),
  ]);

  sheet.open(
    el('div', { class: 'settings' }, [
      languageRow,
      themeRow,
      offlineRow(controller),
      el('div', { class: 'setting setting--stacked' }, [
        el('span', { class: 'setting-label', text: t('settings.attribution') }),
        el('p', { class: 'muted', text: '© OpenStreetMap contributors (ODbL) · © Overture Maps Foundation · Fotos: Mapillary (CC BY-SA)' }),
      ]),
    ]),
    { title: t('settings.title') },
  );
}

/** Wire the shell once the map exists. */
export async function bootstrap() {
  const container = document.getElementById('map');
  if (!container) return;
  sheet.mount();
  document.documentElement.lang = getLanguage();
  let controller = null;
  const context = {
    map: null, getPosition, requestPosition,
    openPlaceCard: (id) => openPlaceCard(id, context),
    openPointCard: (point) => openPointCard(point, context),
    openDirections: (destination) => openDirections(destination, context),
    openShare: (point) => openShareSheet(point, context),
    openReport: () => openReport(context),
    choosePoint: () => choosePoint(context, null, point => openPointCard(point, context)),
    chooseFavorite: kind => choosePoint(context, null, point => { saveFavorite(kind, point); toast(t('poi.saved')); }),
    showResults: (hits, onSelect) => {
      for (let i = 0; i < 12; i++) {
        const hit = hits[i];
        controller?.setMarker(`result-${i}`, hit?.lat ?? null, hit?.lon ?? null, {
          title: hit?.name, label: String(i + 1), onClick: () => onSelect(hit),
        });
      }
    },
  };
  const applyTheme = (preference) => {
    const night = preference === 'night' || (preference === 'auto' && isNight());
    document.documentElement.classList.toggle('night', night);
    controller?.setNightMode(night);
  };
  applyTheme(readThemePreference());
  const search = createSearch(context);
  applyStaticStrings();
  for (const [id, name] of [['locate','locate'], ['settings-button','settings'],
    ['share-button','share'], ['report-button','report'], ['search-clear','close'], ['search-back','back']]) {
    document.getElementById(id)?.replaceChildren(icon(name));
  }
  performance.mark?.('nicanav-shell-ready');
  setupUpdates();
  const viewport = window.visualViewport;
  const resize = () => {
    // A keyboard resizes the usable surface; zooming text must stay possible.
    const height = viewport && viewport.scale === 1 ? viewport.height : window.innerHeight;
    document.documentElement.style.setProperty('--app-height', `${height}px`);
    controller?.map.resize();
  };
  viewport?.addEventListener('resize', resize);
  window.addEventListener('resize', resize);
  resize();
  document.getElementById('settings-button')?.addEventListener('click', () => openSettings(controller, applyTheme));
  document.getElementById('report-button')?.addEventListener('click', () => openReport(context));
  document.getElementById('share-button')?.addEventListener('click', () => {
    const centre = controller?.map.getCenter();
    if (centre) openShareSheet({ lat: centre.lat, lon: centre.lng }, context);
    else toast(t('map.loading'));
  });
  document.getElementById('locate')?.addEventListener('click', async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const fix = await requestPosition();
      controller?.setMarker('user', fix.lat, fix.lon, { kind: 'user', title: t('locate.title') });
      controller?.flyTo(fix.lat, fix.lon, { zoom: 16.5 });
    } catch { toast(t('locate.denied'), { kind: 'error' }); }
    finally { button.disabled = false; }
  });
  onLanguageChange(() => { document.documentElement.lang = getLanguage(); applyStaticStrings(); sheet.close(); });
  const link = parseDeepLink(window.location.pathname + window.location.hash);
  const status = document.getElementById('map-status');
  let bootId = 0, stopConnectivity = null, linkTimer, loadTimer;
  async function bootMap() {
    const owner = ++bootId;
    const current = () => owner === bootId;
    clearTimeout(loadTimer); clearTimeout(linkTimer);
    stopConnectivity?.(); stopConnectivity = null;
    controller?.map.remove(); controller = context.map = null;
    if (status) { status.hidden = false; status.textContent = t('map.loading'); }
    const timer = loadTimer = setTimeout(() => {
      if (current() && status) status.replaceChildren(el('span', { text: t('map.slow') }),
        el('button', { type: 'button', class: 'btn', text: t('common.retry'), onClick: bootMap }));
    }, 8000);
    try {
      const created = await createMap({ container, isCurrent: current,
        center: link ? [link.lon, link.lat] : undefined, zoom: link?.zoom,
        night: document.documentElement.classList.contains('night'),
        onReady: () => { if (!current()) return; clearTimeout(timer); if (status) status.hidden = true; performance.mark?.('nicanav-map-ready'); },
        onPoiClick: (hit) => hit.id ? openPlaceCard(hit.id, context) : openPointCard(hit, context),
        onMapClick: () => { if (context.pickPoint) return; if (sheet.isOpen()) sheet.close(); search?.show(false); },
        onLongPress: (point) => openPointCard(point, context),
      });
      if (!current()) { created.map.remove(); return; }
      controller = context.map = created;
      if (context.selected) {
        controller.setMarker('selected', context.selected.lat, context.selected.lon, { title: context.selected.name });
        controller.flyTo(context.selected.lat, context.selected.lon);
      }
      if (link) controller.setMarker('shared', link.lat, link.lon, { title: t('share.title') });
      created.map.on('moveend', () => { clearTimeout(linkTimer); linkTimer = setTimeout(() => { if (current()) writeDeepLink(created.map); }, 700); });
      const offline = await import('./offline.js');
      await offline.activateOffline();
      if (current()) stopConnectivity = offline.autoSwitchOnConnectivity(created.map);
    } catch {
      if (!current()) return;
      controller?.map.remove(); controller = context.map = null;
      clearTimeout(timer);
      if (status) status.replaceChildren(el('span', { text: t('map.failedTitle') }), el('button', {
        type: 'button', class: 'btn', text: t('common.retry'), onClick: bootMap,
      }));
    }
  }
  bootMap();
}

/** Re-label the parts of the shell that live in index.html, after a language switch. */
function applyStaticStrings() {
  const input = /** @type {HTMLInputElement|null} */ (document.getElementById('search-input'));
  if (input) {
    input.placeholder = t('search.placeholder');
    input.setAttribute('aria-label', t('search.placeholder'));
  }
  const labels = {
    locate: t('locate.title'),
    'report-button': t('poi.report'),
    'settings-button': t('settings.title'),
    'share-button': t('poi.share'),
    'search-clear': t('search.clear'),
  };
  for (const [id, label] of Object.entries(labels)) {
    const node = document.getElementById(id);
    if (node) node.setAttribute('aria-label', label);
  }
}

// index.html loads this module; run the app unless it was imported for its
// exports (a page with no #map, such as a unit-test harness).
if (typeof document !== 'undefined' && document.getElementById('map')) {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bootstrap, { once: true });
  } else {
    bootstrap();
  }
}

export { getPosition, requestPosition };
