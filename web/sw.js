/**
 * Service worker: the app shell offline, and nothing dangerous cached.
 *
 * The rule that matters: **API responses are never served stale.** A cached
 * route or a cached "abierto ahora" is worse than an error message, because the
 * driver acts on it. Only the shell — HTML, CSS, JS, style, sprites, fonts — is
 * cached, and tiles are handled by offline.js through Cache Storage directly
 * (a `.pmtiles` archive is read with byte ranges, and a 206 cannot be cached).
 */

const VERSION = 'development-v5';
const SHELL_CACHE = `nicanav-shell-${VERSION}`;
const ASSET_CACHE = `nicanav-assets-${VERSION}`;

/** Everything needed to draw the app with no network at all. */
const SHELL = [
  '/',
  '/index.html',
  // Generated at deploy time by scripts/render_web_config.py, so it is not in
  // the repository — but it must be cached, or an offline start has no API URL.
  '/config.js',
  '/css/app.css',
  '/vendor/v1/maplibre-gl.mjs',
  '/vendor/v1/maplibre-gl-shared.mjs',
  '/vendor/v1/maplibre-gl-worker.mjs',
  '/vendor/v1/maplibre-gl.css',
  '/vendor/v1/pmtiles.js',
  '/vendor/v1/opening-hours.mjs',
  '/vendor/v1/noto-regular.woff2',
  '/vendor/v1/noto-bold.woff2',
  '/vendor/v1/noto-italic.woff2',

  '/js/api.js',
  '/js/config.js',
  '/js/map.js',
  '/js/updates.js',
  '/js/favorites.js',
  '/js/poi.js',
  '/js/search.js',
  '/js/share.js',
  '/js/ui.js',
  // Navigation is imported on demand, but it is precached anyway: the moment a
  // driver needs it is the moment they are least likely to have signal.
  '/js/nav.js',
  '/js/navmath.js',
  '/js/voice.js',
  // nav.js imports this statically, so leaving it out breaks offline
  // navigation with a module-resolution error rather than a missing feature.
  '/js/simulator.js',
  '/js/offline.js',
  '/js/offline-style.js',
  '/style/nicanav.json',
  '/style/nicanav-night.json',
  '/sprites/nicanav.json',
  '/sprites/nicanav.png',
  '/sprites/nicanav@2x.json',
  '/sprites/nicanav@2x.png',
  '/manifest.webmanifest',
  '/icons/favicon.svg',
  '/icons/apple-touch-icon.png',
  '/icons/app-192.png',
  '/icons/app-512.png',
  '/icons/app-maskable-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(SHELL_CACHE)
      // Keep the previous worker if this release cannot cache its full shell.
      .then((cache) => cache.addAll(SHELL.map((url) => new Request(url, { cache: 'reload' })))),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    self.clients.claim(),
  );
});

/** Only public, explicitly owned assets belong in offline storage. */
function isImmutableAsset(url) {
  return (
    url.pathname.startsWith('/fonts/') ||
    url.pathname.startsWith('/sprites/') ||
    url.pathname.startsWith('/icons/')
  );
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Basic-auth pages must never survive logout or become visible offline.
  // Bypass even existing caches; activation also removes the old broad cache.
  if (request.headers.has('authorization') ||
      /^\/(api|admin)(\/|$)/.test(url.pathname)) return;

  // Tiles are byte-range requests; the cache cannot hold their 206 responses,
  // and offline.js owns the whole-archive copy instead.
  if (url.pathname.startsWith('/tiles/')) return;

  const navigation = request.mode === 'navigate' &&
    (url.pathname === '/' || url.pathname === '/index.html' || url.pathname.startsWith('/@'));
  if (!navigation && !SHELL.includes(url.pathname) && !isImmutableAsset(url)) return;

  // Every installed shell belongs to one content-addressed release. A warm
  // start reads it immediately, even on a connection that is nominally online.
  const fallback = () => new Response('Sin conexión', { status: 503,
    headers: { 'Content-Type': 'text/plain; charset=utf-8' } });
  const path = navigation ? '/index.html' : url.pathname;
  event.respondWith(caches.open(SHELL_CACHE).then(async (cache) => {
    const hit = await cache.match(path);
    if (hit) return hit;
    let timer;
    const network = fetch(request).then(async (response) => {
      if (canCache(response)) await cache.put(path, response.clone()).catch(() => {});
      return response;
    }).catch(fallback);
    event.waitUntil(network.then(() => undefined));
    return Promise.race([network, new Promise(resolve => {
      timer = setTimeout(() => resolve(fallback()), 3000);
    })]).finally(() => clearTimeout(timer));
  }));
});

function canCache(response) {
  return response.status === 200 && !response.redirected &&
    !/no-store|private/i.test(response.headers.get('cache-control') || '');
}

// A waiting release checks every open tab; a trip in another tab blocks it.
self.addEventListener('message', (event) => {
  if (event.data !== 'activate-update') return;
  event.waitUntil((async () => {
    const clients = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    const states = await Promise.all(clients.map(client => new Promise(resolve => {
      const channel = new MessageChannel();
      const timer = setTimeout(() => { channel.port1.close(); resolve(true); }, 1500);
      channel.port1.onmessage = reply => { clearTimeout(timer); channel.port1.close(); resolve(reply.data?.busy !== false); };
      client.postMessage('update-check', [channel.port2]);
    })));
    if (!states.some(Boolean)) await self.skipWaiting();
  })());
});
