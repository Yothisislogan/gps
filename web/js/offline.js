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

const STORED_ARCHIVE = '/offline/current-map';

function archiveUrl() {
  return CONFIG.offlineArchive || `${CONFIG.tilesBase}/circle.pmtiles`;
}

/**
 * What is stored, if anything.
 * @returns {Promise<{present: boolean, bytes: number, storedAt: string|null}>}
 */
async function storedArchive() {
  const cache = await openCache();
  const response = await cache.match(STORED_ARCHIVE) || await cache.match(archiveUrl()) || await cache.match('/tiles/circle.pmtiles');
  if (!response) return null;
  if ((response.headers.get('content-type') || '').includes('application/json')) {
    const meta = await response.json();
    if (meta.file) {
      const directory = await navigator.storage.getDirectory();
      const file = await (await directory.getFileHandle(meta.file)).getFile();
      return { meta, blob: () => Promise.resolve(file) };
    }
    const stored = await cache.match(meta.key);
    if (!stored) return null;
    return { meta, blob: () => stored.blob() };
  }
  return { meta: { bytes: Number(response.headers.get('content-length')), storedAt: response.headers.get('x-nicanav-stored-at') }, blob: () => response.blob() };
}

export async function offlineStatus() {
  try {
    const stored = await storedArchive();
    return stored ? { present: true, ...stored.meta } : { present: false, bytes: 0, storedAt: null };
  } catch { return { present: false, bytes: 0, storedAt: null }; }
}

/**
 * How big the download is, without starting it.
 * @returns {Promise<number>} bytes, or 0 when the server will not say
 */
export async function offlineSize() {
  try {
    const response = await fetch(archiveUrl(), { method: 'HEAD', signal: AbortSignal.timeout(8000) });
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
async function removeStored(meta) {
  if (meta?.file) {
    const directory = await navigator.storage.getDirectory();
    await directory.removeEntry(meta.file).catch(() => {});
  } else if (meta?.key) await (await openCache()).delete(meta.key);
}

let downloading = false;
export async function downloadCircle(onProgress, signal) {
  if (downloading) throw new Error('Ya hay una descarga en curso');
  const run = async () => {
    downloading = true;
    try { return await streamDownload(onProgress, signal); }
    finally { downloading = false; }
  };
  if (typeof navigator !== 'undefined' && navigator.locks) {
    return navigator.locks.request('nicanav-map-download', { ifAvailable: true }, lock => {
      if (!lock) throw new Error('Ya hay una descarga en otra pestaña');
      return run();
    });
  }
  return run();
}

async function streamDownload(onProgress, signal) {
  const cache = await openCache();
  const previous = await storedArchive().catch(() => null);
  const abort = new AbortController();
  const cancel = () => abort.abort(signal?.reason);
  signal?.addEventListener('abort', cancel, { once: true });
  if (signal?.aborted) cancel();
  let deadline;
  const pulse = () => { clearTimeout(deadline); deadline = setTimeout(() => abort.abort(new Error('La descarga se detuvo. Reintentá cuando vuelva la señal.')), 30000); };
  let meta = {}, writer, reader;
  try {
    pulse();
    const response = await fetch(archiveUrl(), { signal: abort.signal });
    if (response.status !== 200 || !response.body) throw new Error('No se pudo descargar el mapa completo');
    const total = Number(response.headers.get('content-length')) || 0;
    const storage = typeof navigator === 'undefined' ? null : navigator.storage;
    const estimate = await storage?.estimate?.();
    if (total && estimate?.quota && estimate.quota - estimate.usage < total * 1.15) throw new Error('No hay espacio suficiente. Liberá espacio antes de descargar.');
    const id = crypto.randomUUID();
    meta = { bytes: total, storedAt: new Date().toISOString(), etag: response.headers.get('etag') };
    let received = 0, headerLength = 0;
    const header = new Uint8Array(127);
    const inspect = value => {
      abort.signal.throwIfAborted(); pulse();
      if (headerLength < header.length) {
        const part = value.subarray(0, header.length - headerLength);
        header.set(part, headerLength); headerLength += part.length;
        if (headerLength >= 8 && (new TextDecoder().decode(header.subarray(0, 7)) !== 'PMTiles' || header[7] !== 3)) throw new Error('La descarga no es un mapa válido');
      }
      received += value.byteLength;
      if (total && received > total) throw new Error('El tamaño del mapa no coincide');
      if (onProgress) onProgress(total ? received / total : 0, received, total);
    };
    const validate = () => {
      abort.signal.throwIfAborted();
      if (headerLength < 127 || (total && received !== total)) throw new Error('La descarga del mapa está incompleta');
      const view = new DataView(header.buffer);
      const end = Number(view.getBigUint64(56, true) + view.getBigUint64(64, true));
      if (!Number.isSafeInteger(end) || end > received) throw new Error('El mapa está truncado');
      meta.bytes = received;
    };
    let blob;
    if (storage?.getDirectory) {
      const directory = await storage.getDirectory();
      meta.file = `nicanav-map-${id}.pmtiles`;
      const handle = await directory.getFileHandle(meta.file, { create: true });
      writer = await handle.createWritable();
      reader = response.body.getReader();
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        inspect(value);
        await writer.write(value); // Backpressure: never accumulate the archive in JS memory.
      }
      validate();
      await writer.close(); writer = null;
      blob = await handle.getFile();
    } else {
      meta.key = new URL(`/offline/nicanav-${id}.pmtiles`, location.origin).href;
      const stream = response.body.pipeThrough(new TransformStream({
        transform(value, target) { inspect(value); target.enqueue(value); }, flush: validate,
      }));
      await cache.put(meta.key, new Response(stream, { headers: { 'Content-Type': 'application/octet-stream' } }));
      blob = await (await cache.match(meta.key)).blob();
    }
    if (!window.pmtiles) throw new Error('El lector del mapa no está disponible');
    await new window.pmtiles.PMTiles(new CachedArchiveSource(blob, `check-${id}`)).getHeader();
    abort.signal.throwIfAborted();
    // A single pointer replacement commits the validated download. The old
    // archive survives cancellation, storage failure and captive portals.
    await cache.put(STORED_ARCHIVE, new Response(JSON.stringify(meta), { headers: { 'Content-Type': 'application/json' } }));
    await removeStored(previous?.meta).catch(() => {});
    await activateOffline().catch(() => {});
    if (onProgress) onProgress(1, received, received);
    return { bytes: received };
  } catch (error) {
    abort.abort();
    await reader?.cancel().catch(() => {});
    await writer?.abort().catch(() => {});
    await removeStored(meta).catch(() => {});
    throw error;
  } finally {
    clearTimeout(deadline);
    signal?.removeEventListener('abort', cancel);
  }
}

/** Forget the stored archive after its pointer is removed. */
export async function clearOffline() {
  try {
    const stored = await storedArchive();
    const cache = await openCache();
    for (const key of new Set([STORED_ARCHIVE, archiveUrl(), '/tiles/circle.pmtiles'])) await cache.delete(key);
    await removeStored(stored?.meta);
    return true;
  } catch { return false; }
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

  const stored = await storedArchive();
  if (!stored) return false;
  const blob = await stored.blob();
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
  let disposed = false;
  const update = async () => {
    const status = await offlineStatus();
    if (disposed || (!navigator.onLine && !status.present)) return;
    if (!navigator.onLine) {
      await activateOffline();
      if (!disposed) useOfflineSource(map, true);
    } else {
      useOfflineSource(map, false);
    }
  };
  window.addEventListener('online', update);
  window.addEventListener('offline', update);
  update().catch(() => {});
  return () => {
    disposed = true;
    window.removeEventListener('online', update);
    window.removeEventListener('offline', update);
  };
}
