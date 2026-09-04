/**
 * Service worker: the app shell offline, and nothing dangerous cached.
 *
 * The rule that matters: **API responses are never served stale.** A cached
 * route or a cached "abierto ahora" is worse than an error message, because the
 * driver acts on it. Only the shell — HTML, CSS, JS, style, sprites, fonts — is
 * cached, and tiles are handled by offline.js through Cache Storage directly
 * (a `.pmtiles` archive is read with byte ranges, and a 206 cannot be cached).
 */

const VERSION = 'v1';
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
  '/style/nicanav.json',
  '/style/nicanav-night.json',
  '/sprites/nicanav.json',
  '/sprites/nicanav.png',
  '/manifest.webmanifest',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(SHELL_CACHE)
      // Individually, not addAll: one 404 must not fail the whole install and
      // leave the app with no offline shell at all.
      .then((cache) =>
        Promise.all(
          SHELL.map((url) =>
            cache.add(new Request(url, { cache: 'reload' })).catch(() => undefined),
          ),
        ),
      )
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

/** Fonts, sprites and the style: immutable enough to serve from cache first. */
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

  // Never cache the API. A stale route is a wrong turn.
  if (url.pathname.startsWith('/api/')) return;

  // Tiles are byte-range requests; the cache cannot hold their 206 responses,
  // and offline.js owns the whole-archive copy instead.
  if (url.pathname.startsWith('/tiles/')) return;

  if (isImmutableAsset(url)) {
    event.respondWith(
      caches.match(request).then(
        (hit) =>
          hit ||
          fetch(request).then((response) => {
            if (response.ok) {
              const copy = response.clone();
              caches.open(ASSET_CACHE).then((cache) => cache.put(request, copy));
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
  event.respondWith(
    Promise.race([
      fetch(request).then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(SHELL_CACHE).then((cache) => cache.put(request, copy));
        }
        return response;
      }),
      new Promise((resolve) => {
        setTimeout(() => caches.match(request).then((hit) => hit && resolve(hit)), 3000);
      }),
    ]).catch(() =>
      caches
        .match(request)
        .then((hit) => hit || caches.match('/index.html'))
        .then(
          (hit) =>
            hit ||
            new Response('Sin conexión', {
              status: 503,
              headers: { 'Content-Type': 'text/plain; charset=utf-8' },
            }),
        ),
    ),
  );
});

// Lets a new version take over without waiting for every tab to close.
self.addEventListener('message', (event) => {
  if (event.data === 'skip-waiting') self.skipWaiting();
});
