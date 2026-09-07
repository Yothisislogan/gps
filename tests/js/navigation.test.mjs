import assert from 'node:assert/strict';
import { beforeEach, afterEach, test, mock } from 'node:test';
import { installDom, deferred, flush } from './helpers/dom.mjs';
import { startNavigation, stopNavigation, isNavigating } from '../../web/js/nav.js';
import { encodePolyline } from '../../web/js/navmath.js';

const points = [{ lat: 12.12, lon: -86.26 }, { lat: 12.10, lon: -86.25 }];
export const route = { trip: { summary: { length: 3, time: 300 }, legs: [{
  shape: encodePolyline(points), summary: { length: 3, time: 300 },
  maneuvers: [{ type: 1, begin_shape_index: 0, end_shape_index: 1, length: 3, time: 300, instruction: 'Seguí' }],
}] } };
let dom, watches, released, navigatorFake;

function lock() {
  return Object.assign(new EventTarget(), {
    released: false,
    async release() { this.released = true; released++; this.dispatchEvent(new Event('release')); },
  });
}

beforeEach(() => {
  dom = installDom(); watches = []; released = 0;
  navigatorFake = {
    wakeLock: { request: async () => lock() },
    geolocation: {
      watchPosition(success, error) { watches.push({ success, error }); return watches.length; },
      clearWatch() {},
    },
  };
  mock.getter(globalThis, 'navigator', () => navigatorFake);
});
afterEach(() => { stopNavigation(); mock.restoreAll(); });

function options(extra = {}) {
  return { route, destination: points[1], ...extra };
}
function fix(extra = {}) {
  return { timestamp: Date.now(), coords: {
    latitude: 12.14, longitude: -86.29, accuracy: 5, speed: 10, heading: 90, ...extra,
  } };
}
async function triggerReroute() {
  for (let i = 0; i < 4; i++) watches.at(-1).success(fix());
  await flush();
}

test('stopping while wake lock is pending does not restart GPS later', async () => {
  const pending = deferred();
  navigatorFake.wakeLock.request = () => pending.promise;
  const started = startNavigation(options());
  stopNavigation();
  pending.resolve(lock());
  await started;
  assert.equal(isNavigating(), false);
  assert.equal(watches.length, 0);
  assert.equal(released, 1);
});

test('rerouting preserves the road preferences selected before the trip', async () => {
  let body;
  mock.method(globalThis, 'fetch', async (_url, request) => {
    body = JSON.parse(request.body); return Response.json(route);
  });
  await startNavigation(options({ routeOptions: { avoid_unpaved: true, costing: 'bicycle' } }));
  await triggerReroute();
  assert.equal(body.avoid_unpaved, true);
  assert.equal(body.costing, 'bicycle');
  assert.equal(body.heading, 90);
});

test('an old reroute cannot overwrite a replacement trip, even if its transport ignores abort', async () => {
  const pending = deferred();
  let signal, updated = 0;
  mock.method(globalThis, 'fetch', async (_url, request) => {
    signal = request.signal; return pending.promise;
  });
  await startNavigation(options());
  await triggerReroute();
  assert.ok(signal);
  await startNavigation(options({ map: { showRoute: () => updated++ } }));
  assert.equal(signal.aborted, true);
  pending.resolve(Response.json(route));
  await flush();
  assert.equal(updated, 0);
  assert.equal(isNavigating(), true);
});

test('late GPS callbacks from a previous watch are ignored', async () => {
  let positions = 0;
  await startNavigation(options());
  const oldWatch = watches[0];
  await startNavigation(options({ map: { setMarker: () => positions++, flyTo() {} } }));
  oldWatch.success(fix());
  assert.equal(positions, 0);
});

test('old and inaccurate fixes cannot advance guidance', async () => {
  let positions = 0;
  await startNavigation(options({ map: { setMarker: () => positions++, flyTo() {} } }));
  watches[0].success(fix({ accuracy: 250 }));
  watches[0].success({ ...fix(), timestamp: Date.now() - 60000 });
  assert.equal(positions, 0);
});

test('a wake lock released by the browser is acquired again when visible', async () => {
  const locks = [];
  navigatorFake.wakeLock.request = async () => { const value = lock(); locks.push(value); return value; };
  await startNavigation(options());
  await locks[0].release();
  dom.doc.dispatchEvent(new Event('visibilitychange'));
  await flush();
  assert.equal(locks.length, 2);
});

test('denying location permission ends guidance', async () => {
  await startNavigation(options());
  watches[0].error({ code: 1 });
  assert.equal(isNavigating(), false);
});
