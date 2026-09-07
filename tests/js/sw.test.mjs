import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { test } from 'node:test';

const source = readFileSync(new URL('../../web/sw.js', import.meta.url), 'utf8');
const origin = 'https://mapa.example.ni';

function worker(fetcher = async () => { throw new TypeError('offline'); }) {
  const listeners = {}, entries = new Map(), timers = [], writes = [];
  const key = (request) => new URL(typeof request === 'string' ? request : request.url, origin).href;
  const cache = {
    match: async (request) => entries.get(key(request))?.clone(),
    put: async (request, response) => { writes.push(key(request)); entries.set(key(request), response); },
  };
  vm.runInNewContext(source, {
    self: { location: { origin }, addEventListener: (name, fn) => { listeners[name] = fn; } },
    caches: { ...cache, open: async () => cache },
    fetch: fetcher, URL, Request, Response,
    setTimeout: (fn) => { timers.push(fn); return timers.length; }, clearTimeout() {},
  });
  return {
    entries, writes, timers,
    get(path, { mode = 'cors', headers = {} } = {}) {
      let response;
      const pending = [];
      listeners.fetch({
        request: { url: new URL(path, origin).href, method: 'GET', mode, headers: new Headers(headers) },
        respondWith: (promise) => { response = promise; },
        waitUntil: (promise) => pending.push(promise),
      });
      return { response, pending };
    },
    cache(path, body, type = 'text/html') {
      entries.set(key(path), new Response(body, { headers: { 'Content-Type': type } }));
    },
  };
}

test('API, admin pages and authenticated requests never enter offline handling', () => {
  const sw = worker();
  for (const path of ['/api', '/api/route', '/admin', '/admin/aliases', '/tiles/base.pmtiles', '/unknown']) {
    sw.cache(path, 'private');
    assert.equal(sw.get(path, { mode: 'navigate' }).response, undefined, path);
  }
  assert.equal(sw.get('/index.html', { headers: { Authorization: 'Basic test' } }).response, undefined);
});

test('offline deep links open the shell, but a missing module never receives HTML', async () => {
  const sw = worker();
  sw.cache('/index.html', '<main>Map</main>');
  assert.equal(await (await sw.get('/@12.1,-86.2,16z', { mode: 'navigate' }).response).text(), '<main>Map</main>');
  const missing = await sw.get('/js/nav.js').response;
  assert.equal(missing.status, 503);
  assert.match(missing.headers.get('content-type'), /text\/plain/);
});

test('a stalled request without a cached copy still has a deadline', async () => {
  const sw = worker(() => new Promise(() => {}));
  const request = sw.get('/js/nav.js');
  sw.timers[0]();
  assert.equal((await request.response).status, 503);
});

test('server errors fall back to a known public shell', async () => {
  const sw = worker(async () => new Response('unavailable', { status: 503 }));
  sw.cache('/index.html', 'cached app');
  assert.equal(await (await sw.get('/', { mode: 'navigate' }).response).text(), 'cached app');
  assert.equal(sw.writes.length, 0);
});

test('private and no-store responses are never written to cache', async () => {
  for (const policy of ['private, max-age=60', 'no-store']) {
    const sw = worker(async () => new Response('do not save', { headers: { 'Cache-Control': policy } }));
    await sw.get('/index.html').response;
    assert.equal(sw.writes.length, 0);
  }
});

test('navigating to pins does not create an unbounded history of cached locations', async () => {
  const sw = worker(async () => new Response('app'));
  await sw.get('/@12.1,-86.2,16z', { mode: 'navigate' }).response;
  await sw.get('/@12.3,-86.4,16z', { mode: 'navigate' }).response;
  assert.deepEqual([...sw.entries.keys()], [`${origin}/index.html`]);
});

test('a fresh worker can serve renderer, worker, fonts and hours parser without the HTTP cache', async () => {
  const sw = worker();
  for (const path of ['/vendor/v1/maplibre-gl.mjs', '/vendor/v1/maplibre-gl-shared.mjs',
    '/vendor/v1/maplibre-gl-worker.mjs', '/vendor/v1/pmtiles.js', '/vendor/v1/opening-hours.mjs',
    '/vendor/v1/noto-regular.woff2', '/sprites/nicanav@2x.png']) {
    sw.cache(path, 'installed asset', 'application/octet-stream');
    assert.equal(await (await sw.get(path).response).text(), 'installed asset', path);
  }
});
