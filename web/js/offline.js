/**
 * Offline tiles for the curated circle.
 *
 * The constraint that shapes this whole module: **Cache Storage cannot store a
 * 206 response**. PMTiles is read with byte ranges, so the obvious approach —
 * let the service worker cache tile requests as they happen — cannot work. Each
 * ranged request either misses the cache forever or, worse, is cached as a
 * partial response the browser then refuses to serve.
 *
 * So the archive is downloaded **whole**, as one 200 response, and the ranges
 * are served locally by giving pmtiles a custom `Source` that slices the cached
 * blob. That is why the pipeline builds `circle.pmtiles` separately: the
 * country-wide `base.pmtiles` is far too large to hold on a phone, while the
 * 48.3 km circle is the area people actually drive and the area that gets field
 * verified.
 *
 * Download size is shown before anything starts. This is a metered mobile
 * connection in a country where data costs real money.
 */

import { CONFIG } from './config.js';
import { pmtilesProtocol } from './map.js';
import { offlineStyle } from './offline-style.js';

const onlineStyles = new WeakMap();
export function rememberOnlineStyle(map, style) { onlineStyles.set(map, style); }

const CACHE_NAME = 'nicanav-offline-v1';
/** The key MapLibre styles use for the offline archive. */
export const OFFLINE_URL = 'nicanav://circle.pmtiles';

/**
 * A pmtiles `Source` backed by one cached Response.
 *
 * The pmtiles library asks for byte ranges; this slices them out of the blob
 * that Cache Storage holds. Two methods is the whole contract.
 */
class CachedArchiveSource {
  /** @param {Blob} blob @param {string} key */
  constructor(blob, key) {
    this.blob = blob;
    this.key = key;
  }

  getKey() {
    return this.key;
  }

  /**
   * @param {number} offset
   * @param {number} length
   * @returns {Promise<{data: ArrayBuffer, etag?: string, cacheControl?: string, expires?: string}>}
   */
  async getBytes(offset, length) {
    const slice = this.blob.slice(offset, offset + length);
    return { data: await slice.arrayBuffer() };
  }
}

async function openCache() {
  if (!('caches' in window)) throw new Error('Este navegador no puede guardar el mapa sin conexión');
  return caches.open(CACHE_NAME);
}

function archiveUrl() {
  return CONFIG.offlineArchive || `${CONFIG.tilesBase}/circle.pmtiles`;
}

/**
 * What is stored, if anything.
 * @returns {Promise<{present: boolean, bytes: number, storedAt: string|null}>}
 */
export async function offlineStatus() {
  try {
    const cache = await openCache();
    const response = await cache.match(archiveUrl());
    if (!response) return { present: false, bytes: 0, storedAt: null };
    const blob = await response.blob();
    return {
      present: true,
      bytes: blob.size,
      storedAt: response.headers.get('x-nicanav-stored-at'),
    };
  } catch {
    return { present: false, bytes: 0, storedAt: null };
  }
}

/**
 * How big the download is, without starting it.
 * @returns {Promise<number>} bytes, or 0 when the server will not say
 */
export async function offlineSize() {
  try {
    const response = await fetch(archiveUrl(), { method: 'HEAD' });
    return Number(response.headers.get('content-length')) || 0;
  } catch {
    return 0;
  }
}

/**
 * Download the circle archive into Cache Storage.
 *
 * Streams so progress is real rather than a spinner, and so a cancelled
 * download frees its memory instead of holding a hundred megabytes hostage.
 *
 * @param {(fraction: number, received: number, total: number) => void} [onProgress]
 * @param {AbortSignal} [signal]
 */
export async function downloadCircle(onProgress, signal) {
  const cache = await openCache();
  const url = archiveUrl();
  const response = await fetch(url, { signal });
  if (!response.ok) throw new Error(`No se pudo descargar el mapa (${response.status})`);

  const total = Number(response.headers.get('content-length')) || 0;
  const chunks = [];
  let received = 0;

  if (response.body && typeof response.body.getReader === 'function') {
    const reader = response.body.getReader();
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value);
      received += value.byteLength;
      if (onProgress) onProgress(total ? received / total : 0, received, total);
    }
  } else {
    // Old WebViews have no streams; the download still works, just blind.
    const buffer = await response.arrayBuffer();
    chunks.push(new Uint8Array(buffer));
    received = buffer.byteLength;
    if (onProgress) onProgress(1, received, received || total);
  }

  const blob = new Blob(chunks, { type: 'application/octet-stream' });
  // A captive portal can return HTTP 200 HTML. Validate before replacing the
  // previous download, so "downloaded" means a readable PMTiles header.
  const pmtiles = window.pmtiles;
  if (!pmtiles) throw new Error('El lector del mapa no está disponible');
  await new pmtiles.PMTiles(new CachedArchiveSource(blob, 'nicanav-download-check')).getHeader();
  if (total && received !== total) throw new Error('La descarga del mapa está incompleta');
  await cache.put(
    url,
    new Response(blob, {
      status: 200,
      headers: {
        'Content-Type': 'application/octet-stream',
        'Content-Length': String(blob.size),
        'x-nicanav-stored-at': new Date().toISOString(),
      },
    }),
  );

  await activateOffline();
  if (onProgress) onProgress(1, received, total || received);
  return { bytes: blob.size };
}

/** Forget the stored archive. */
export async function clearOffline() {
  try {
    const cache = await openCache();
    await cache.delete(archiveUrl());
    return true;
  } catch {
    return false;
  }
}

/**
 * Register the cached archive with the PMTiles protocol.
 *
 * After this, a style source pointed at {@link OFFLINE_URL} reads from the
 * phone rather than the network — which is the whole point, and which also
 * makes the offline map indistinguishable from the online one to MapLibre.
 */
export async function activateOffline() {
  const protocol = pmtilesProtocol();
  const pmtiles = /** @type {any} */ (window).pmtiles;
  if (!protocol || !pmtiles) return false;

  const cache = await openCache();
  const response = await cache.match(archiveUrl());
  if (!response) return false;

  const blob = await response.blob();
  const archive = new pmtiles.PMTiles(new CachedArchiveSource(blob, OFFLINE_URL));
  protocol.add(archive);
  return true;
}

/**
 * Point a live map at the offline archive (or back at the network).
 *
 * Swapping the source URL rather than reloading the style keeps the current
 * view, which matters if this happens because signal dropped mid-drive.
 *
 * @param {any} map a MapLibre map
 * @param {boolean} useOffline
 */
export function useOfflineSource(map, useOffline) {
  if (!map || typeof map.getStyle !== 'function') return;
  const style = map.getStyle();
  if (!style || !style.sources) return;
  if (useOffline) {
    if (style.sources.base?.url === `pmtiles://${OFFLINE_URL}`) return;
    onlineStyles.set(map, structuredClone(style));
    map.setStyle(offlineStyle(style, true), { diff: true });
  } else if (onlineStyles.has(map)) {
    const original = onlineStyles.get(map);
    onlineStyles.delete(map);
    map.setStyle(original, { diff: true });
  }

}

/**
 * Use the stored archive automatically when the network is gone.
 *
 * Losing signal on the Carretera Sur should change nothing the driver can see.
 * @param {any} map
 */
export function autoSwitchOnConnectivity(map) {
  const update = async () => {
    const status = await offlineStatus();
    if (!navigator.onLine && !status.present) return;
    if (!navigator.onLine) {
      await activateOffline();
      useOfflineSource(map, true);
    } else {
      useOfflineSource(map, false);
    }
  };
  window.addEventListener('online', update);
  window.addEventListener('offline', update);
  update();
  return () => {
    window.removeEventListener('online', update);
    window.removeEventListener('offline', update);
  };
}
