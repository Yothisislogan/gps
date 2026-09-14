import assert from 'node:assert/strict';
import { afterEach, test, mock } from 'node:test';
import { health, search } from '../../web/js/api.js';

afterEach(() => mock.restoreAll());

function stalledBody() {
  let started;
  const reading = new Promise((resolve) => { started = resolve; });
  mock.method(globalThis, 'fetch', async (_url, { signal }) => ({
    ok: true, status: 200,
    text: () => new Promise((_resolve, reject) => {
      signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
      started();
    }),
  }));
  return reading;
}

test('the deadline includes a stalled response body after headers arrive', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const reading = stalledBody();
  const failure = assert.rejects(health(), (error) => error.code === 'timeout');
  await reading;
  t.mock.timers.tick(5000);
  await failure;
});

test('cancelling an obsolete search also cancels its response body', async () => {
  const reading = stalledBody();
  const controller = new AbortController();
  const failure = assert.rejects(search('Granada', { signal: controller.signal }), { name: 'AbortError' });
  await reading;
  controller.abort();
  await failure;
});

test('a disconnected body is a network failure rather than malformed JSON', async () => {
  mock.method(globalThis, 'fetch', async () => ({
    ok: true, status: 200, text: async () => { throw new TypeError('connection lost'); },
  }));
  await assert.rejects(health(), (error) => error.code === 'network');
});

test('server errors retain their stable code and status', async () => {
  mock.method(globalThis, 'fetch', async () => new Response(JSON.stringify({
    error: { code: 'database_unavailable', message: 'Probá de nuevo.' },
  }), { status: 503 }));
  await assert.rejects(health(), (error) => error.code === 'database_unavailable' && error.status === 503);
});
