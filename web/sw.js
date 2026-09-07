/**
 * Service worker: the app shell offline, and nothing dangerous cached.
 *
 * The rule that matters: **API responses are never served stale.** A cached
 * route or a cached "abierto ahora" is worse than an error message, because the
 * driver acts on it. Only the shell — HTML, CSS, JS, style, sprites, fonts — is
 * cached, and tiles are handled by offline.js through Cache Storage directly
 * (a `.pmtiles` archive is read with byte ranges, and a 206 cannot be cached).
 */

const VERSION = 'v3';
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
  '/js/poi.js',
  '/js/search.js',
  '/js/share.js',
  '/js/ui.js',
  // Navigation is imported on demand, but it is precached anyway: the moment a
  // driver needs it is the moment they are least likely to have signal.
  '/js/nav.js',
  '/js/navmath.js',
  '/js/voice.js',
  '/js/offline.js',
  '/js/offline-style.js',
  '/style/nicanav.json',
  '/style/nicanav-night.json',
  '/sprites/nicanav.json',
  '/sprites/nicanav.png',
  '/sprites/nicanav@2x.json',
  '/sprites/nicanav@2x.png',
  '/manifest.webmanifest',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(SHELL_CACHE)
      // Keep the previous worker if this release cannot cache its full shell.
      .then((cache) => cache.addAll(SHELL.map((url) => new Request(url, { cache: 'reload' }))))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) =>
        Promise.all(
          names
            .filter((name) => name.startsWith('nicanav-') && !name.endsWith(VERSION) && !name.startsWith('nicanav-offline'))
            .map((name) => caches.delete(name)),
        ),
      )
      .then(() => self.clients.claim()),
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

  if (isImmutableAsset(url)) {
    event.respondWith(
      caches.match(request).then(
        (hit) =>
          hit ||
          fetch(request).then((response) => {
            if (canCache(response)) {
              const copy = response.clone();
              event.waitUntil(caches.open(ASSET_CACHE).then((cache) => cache.put(request, copy)).catch(() => {}));
            }
            return response;
          }),
      ),
    );
    return;
  }

  // The shell: network first with a short timeout, cache as the safety net.
  // A driver who has lost signal still gets the app; one who has not gets the
  // current version without a hard refresh.
  const fallback = async () => (await caches.match(request)) ||
    (navigation && await caches.match('/index.html')) ||
    new Response('Sin conexión', {
      status: 503,
      headers: { 'Content-Type': 'text/plain; charset=utf-8' },
    });
  const network = fetch(request).then(async (response) => {
    if (canCache(response)) {
      try {
        const cache = await caches.open(SHELL_CACHE);
        await cache.put(navigation ? '/index.html' : request, response.clone());
      } catch { /* Quota/private mode must not turn an online response into an error. */ }
    }
    return response.status >= 500 ? fallback() : response;
  }).catch(fallback);
  event.waitUntil(network.then(() => undefined));
  let timer;
  event.respondWith(Promise.race([
    network,
    new Promise((resolve) => { timer = setTimeout(() => resolve(fallback()), 3000); }),
  ]).finally(() => clearTimeout(timer)));
});

function canCache(response) {
  return response.status === 200 && !response.redirected &&
    !/no-store|private/i.test(response.headers.get('cache-control') || '');
}

// Lets a new version take over without waiting for every tab to close.
self.addEventListener('message', (event) => {
  if (event.data === 'skip-waiting') self.skipWaiting();
});
