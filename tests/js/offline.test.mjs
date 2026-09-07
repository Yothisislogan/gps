import assert from 'node:assert/strict';
import { test } from 'node:test';
import { offlineStyle } from '../../web/js/offline-style.js';
import { useOfflineSource, rememberOnlineStyle } from '../../web/js/offline.js';
const original = { version: 8, sources: { base: {type: 'vector', url: 'pmtiles:///tiles/base.pmtiles'}, pois: {type: 'vector', url: 'pmtiles:///tiles/pois.pmtiles'} },
  layers: [{id: 'bg', type: 'background'}, {id: 'roads', source: 'base'}, {id: 'places', source: 'pois'}] };
test('cold offline style requests only the downloaded archive and leaves the original intact', () => {
  const style = offlineStyle(original, true);
  assert.equal(style.sources.base.url, 'pmtiles://nicanav://circle.pmtiles');
  assert.equal(style.sources.pois, undefined);
  assert.deepEqual(style.layers.map(l => l.id), ['bg', 'roads']);
  assert(original.sources.pois);
});
test('without an archive the shell can initialize without waiting for unavailable vector tiles', () => {
  assert.deepEqual(offlineStyle(original, false).sources, {});
  assert.deepEqual(offlineStyle(original, false).layers.map(l => l.id), ['bg']);
});
test('reconnection restores POI layers and normal sources', () => {
  let current = offlineStyle(original, true);
  const map = { getStyle: () => current, setStyle: next => { current = next; } };
  rememberOnlineStyle(map, original);
  useOfflineSource(map, false);
  assert.deepEqual(current, original);
});

test('a captive-portal response cannot replace a downloaded archive', async () => {
  const { PMTiles } = await import('pmtiles');
  const { downloadCircle } = await import('../../web/js/offline.js');
  const oldWindow = globalThis.window, oldCaches = globalThis.caches, oldFetch = globalThis.fetch;
  let writes = 0;
  const storage = { open: async () => ({ put: async () => { writes++; } }) };
  globalThis.window = { caches: storage, pmtiles: { PMTiles } };
  globalThis.caches = storage;
  globalThis.fetch = async () => new Response('<html>Please sign in</html>');
  try {
    await assert.rejects(downloadCircle());
    assert.equal(writes, 0);
  } finally {
    if (oldWindow === undefined) delete globalThis.window; else globalThis.window = oldWindow;
    if (oldCaches === undefined) delete globalThis.caches; else globalThis.caches = oldCaches;
    globalThis.fetch = oldFetch;
  }
});
