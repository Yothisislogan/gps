import assert from 'node:assert/strict';
import { beforeEach, afterEach, test, mock } from 'node:test';
import { installDom, findAll, flush } from './helpers/dom.mjs';
import { openDirections } from '../../web/js/poi.js';
import { sheet } from '../../web/js/ui.js';
import { stopNavigation, pendingSession } from '../../web/js/nav.js';
import { encodePolyline } from '../../web/js/navmath.js';

function route(lon, length) {
  return { trip: { summary: { length, time: 600 }, legs: [{
    shape: encodePolyline([{ lat: 12.12, lon: -86.26 }, { lat: 12.10, lon }]),
    summary: { length, time: 600 }, maneuvers: [],
  }] } };
}
const first = route(-86.25, 3), second = route(-86.24, 4);
let dom, shown, requests, context;

beforeEach(() => {
  dom = installDom(); shown = []; requests = [];
  sheet.root = sheet.body = sheet.titleNode = sheet.closeHandler = null;
  context = {
    getPosition: () => ({ lat: 12.12, lon: -86.26 }),
    map: { showRoute: (shape) => shown.push(shape), clearRoute() {} },
  };
  mock.method(globalThis, 'fetch', async (_url, request) => {
    requests.push(JSON.parse(request.body));
    return Response.json({ ...first, alternates: [second], nicanav: { closures_status: 'unavailable' } });
  });
  mock.getter(globalThis, 'navigator', () => ({ geolocation: { watchPosition() { return 1; }, clearWatch() {} } }));
});
afterEach(() => { stopNavigation(); mock.restoreAll(); });

function buttons() { return findAll(sheet.body, (node) => node.tagName === 'BUTTON'); }
function click(label) { buttons().find((node) => node.textContent.includes(label)).dispatchEvent(new Event('click')); }

test('selecting an alternate changes the map and summary; Start uses that exact route', async () => {
  await openDirections({ lat: 12.10, lon: -86.25 }, context);
  click('Ruta 2');
  assert.equal(shown.at(-1), second.trip.legs[0].shape);
  assert.ok(buttons().some((node) => node.textContent.includes('Ruta 2') && node.getAttribute('aria-pressed') === 'true'));
  const distance = findAll(sheet.body, (node) => node.className === 'route__distance')[0];
  assert.match(distance.textContent, /4/);
  click('Empezar');
  await flush();
  assert.deepEqual(pendingSession().route.trip, second.trip);
});

test('the original route remains selectable after choosing an alternate', async () => {
  await openDirections({ lat: 12.10, lon: -86.25 }, context);
  click('Ruta 2'); click('Ruta 1');
  assert.equal(shown.at(-1), first.trip.legs[0].shape);
});

test('the road preference control recalculates and persists the preference', async () => {
  await openDirections({ lat: 12.10, lon: -86.25 }, context);
  const checkbox = findAll(sheet.body, (node) => node.tagName === 'INPUT')[0];
  checkbox.checked = true;
  checkbox.dispatchEvent(new Event('change'));
  await flush();
  assert.equal(requests.at(-1).avoid_unpaved, true);
  assert.equal(dom.storage.get('nicanav.avoidUnpaved'), '1');
});

test('closure-check failure is visible in the route sheet', async () => {
  await openDirections({ lat: 12.10, lon: -86.25 }, context);
  assert.match(sheet.body.textContent, /No pudimos comprobar los cierres/);
});
