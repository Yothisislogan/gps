import assert from 'node:assert/strict';
import { test } from 'node:test';
import { PMTiles } from 'pmtiles';
import { downloadCircle, offlineStatus, clearOffline } from '../../web/js/offline.js';

function archive() {
  const bytes = new Uint8Array(135), view = new DataView(bytes.buffer);
  bytes.set(new TextEncoder().encode('PMTiles')); bytes[7] = 3;
  for (const [offset, value] of [[8,127],[16,5],[24,132],[32,2],[40,134],[48,0],[56,134],[64,1],[72,1],[80,1],[88,1]]) view.setBigUint64(offset, BigInt(value), true);
  bytes.set([1,1,1,1,0,0], 96); bytes.set([1,0,1,1,1,123,125,0],127);
  return bytes;
}

async function withStorage(run) {
  const names = ['window','caches','fetch','navigator','location'];
  const previous = names.map(k => [k, Object.getOwnPropertyDescriptor(globalThis,k)]);
  const entries = new Map();
  const cache = {
    match: async key => entries.get(key)?.clone(),
    delete: async key => entries.delete(key),
    put: async (key, response) => { entries.set(key, new Response(await response.arrayBuffer(), { headers: response.headers })); },
  };
  const storage = { estimate: async () => ({ quota: 1e8, usage: 0 }) };
  const values = { window: { caches: {}, pmtiles: { PMTiles } }, caches: { open: async () => cache },
    navigator: { storage }, location: { origin: 'https://example.invalid' },
    fetch: async () => new Response(archive(), { headers: { 'Content-Length':'135' } }) };
  for (const [key,value] of Object.entries(values)) Object.defineProperty(globalThis,key,{configurable:true,writable:true,value});
  try { await run({ entries, storage }); }
  finally { for (const [key, descriptor] of previous) { if (descriptor) Object.defineProperty(globalThis,key,descriptor); else delete globalThis[key]; } }
}

test('a streamed download commits one stable pointer and can be removed', async () => withStorage(async ({ entries }) => {
  const progress = [];
  assert.deepEqual(await downloadCircle(f => progress.push(f)), { bytes:135 });
  assert(entries.has('/offline/current-map'));
  assert.equal((await offlineStatus()).bytes,135);
  assert.equal(progress.at(-1),1);
  assert(await clearOffline());
  assert.equal(entries.size,0);
}));

test('cancellation and corrupt replacements preserve the previously validated map', async () => withStorage(async ({ entries }) => {
  await downloadCircle();
  const original = await entries.get('/offline/current-map').clone().text();
  globalThis.fetch = async () => new Response('<html>Sign in to Wi-Fi</html>');
  await assert.rejects(downloadCircle(), /mapa/);
  assert.equal(await entries.get('/offline/current-map').clone().text(),original);
  assert.equal(entries.size,2);
  globalThis.fetch = async () => new Response(archive());
  const abort = new AbortController();
  await assert.rejects(downloadCircle(() => abort.abort(), abort.signal));
  assert.equal(await entries.get('/offline/current-map').clone().text(),original);
  assert.equal(entries.size,2);
}));

test('storage shortage fails before replacing an existing map', async () => withStorage(async ({ entries, storage }) => {
  await downloadCircle();
  const original = await entries.get('/offline/current-map').clone().text();
  storage.estimate = async () => ({ quota:100, usage:90 });
  await assert.rejects(downloadCircle(), /espacio/);
  assert.equal(await entries.get('/offline/current-map').clone().text(),original);
}));
