/**
 * The only module that knows an API URL.
 *
 * Every call returns parsed JSON or throws an {@link ApiError} carrying the
 * server's stable `code` plus a Spanish `message`, so callers branch on the
 * code and show the message.  Two Nicaraguan realities shape this file:
 * requests must be abortable (a fast typist on a slow Claro connection would
 * otherwise queue ten searches, and the last one to land wins — not the last
 * one typed), and every request has a deadline, because a 4G connection that
 * has silently died looks exactly like one that is merely slow.
 */

import { CONFIG } from './config.js';

/** Default request deadline.  Long enough for a bad cell, short enough to retry. */
const DEFAULT_TIMEOUT_MS = 12000;

/** Routing does real work upstream (Valhalla + closure lookup); give it more room. */
const ROUTE_TIMEOUT_MS = 25000;

/** An API failure with a code to branch on and a message to show the user. */
export class ApiError extends Error {
  /**
   * @param {string} code stable snake_case identifier, e.g. `no_road_nearby`
   * @param {string} message Spanish text safe to display
   * @param {number} [status] HTTP status, 0 when the request never completed
   */
  constructor(code, message, status = 0) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
  }

  /** True when retrying the exact same request could plausibly succeed. */
  get retriable() {
    return (
      this.status === 0 ||
      this.status >= 500 ||
      this.code === 'network' ||
      this.code === 'timeout' ||
      this.code === 'rate_limited'
    );
  }
}

/**
 * Build a URL with only the parameters that were actually supplied.
 * `null`/`undefined`/`''` are dropped so an unset `lat` never becomes `lat=`.
 * @param {string} path
 * @param {Record<string, unknown>} [params]
 * @returns {string}
 */
function url(path, params) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value === null || value === undefined || value === '') continue;
    query.set(key, typeof value === 'boolean' ? String(value) : String(value));
  }
  const search = query.toString();
  return `${CONFIG.apiBase}${path}${search ? `?${search}` : ''}`;
}

/**
 * Combine a caller's abort signal with a timeout.
 *
 * `AbortSignal.any` would do this in one line but is too new to rely on for a
 * mid-range Android; this composition works everywhere fetch does.
 *
 * @param {AbortSignal|undefined} signal
 * @param {number} timeoutMs
 * @returns {{signal: AbortSignal, done: () => void, timedOut: () => boolean}}
 */
function deadline(signal, timeoutMs) {
  const controller = new AbortController();
  let expired = false;
  const timer = setTimeout(() => {
    expired = true;
    controller.abort();
  }, timeoutMs);

  const forward = () => controller.abort();
  if (signal) {
    if (signal.aborted) forward();
    else signal.addEventListener('abort', forward, { once: true });
  }

  return {
    signal: controller.signal,
    done: () => {
      clearTimeout(timer);
      if (signal) signal.removeEventListener('abort', forward);
    },
    timedOut: () => expired,
  };
}

/**
 * One HTTP round trip, with the error envelope unwrapped.
 *
 * @param {string} target absolute or app-relative URL
 * @param {{method?: string, body?: unknown, signal?: AbortSignal, timeoutMs?: number}} [options]
 * @returns {Promise<any>} the parsed JSON body
 * @throws {ApiError}
 */
async function request(target, options = {}) {
  const { method = 'GET', body, signal, timeoutMs = DEFAULT_TIMEOUT_MS } = options;
  const guard = deadline(signal, timeoutMs);

  let response;
  let text;
  try {
    response = await fetch(target, {
      method,
      signal: guard.signal,
      headers: body === undefined ? { Accept: 'application/json' } : { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    // Receiving headers does not mean the body has arrived. Keep cancellation
    // and the deadline alive while a slow mobile connection delivers it.
    text = await response.text();
  } catch (error) {
    // A caller-initiated abort is not a failure to report — it is the caller
    // saying "I no longer care"; let it propagate as the DOMException it is.
    if (signal && signal.aborted) throw error;
    if (guard.timedOut()) {
      throw new ApiError('timeout', 'La conexión está muy lenta. Probá de nuevo.', 0);
    }
    throw new ApiError('network', 'Sin conexión. Revisá tus datos o el wifi.', 0);
  } finally {
    guard.done();
  }

  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }

  if (!response.ok) {
    const envelope = payload && typeof payload === 'object' ? payload.error : null;
    if (envelope && envelope.code) {
      throw new ApiError(String(envelope.code), String(envelope.message || 'Ocurrió un error.'), response.status);
    }
    if (response.status === 429) {
      throw new ApiError('rate_limited', 'Demasiadas consultas seguidas. Esperá un momento.', 429);
    }
    if (response.status === 404) {
      throw new ApiError('not_found', 'No encontramos eso.', 404);
    }
    throw new ApiError('http_error', 'El servidor no respondió bien. Probá de nuevo.', response.status);
  }

  if (payload === null) {
    throw new ApiError('bad_response', 'El servidor respondió algo inesperado.', response.status);
  }
  return payload;
}

/**
 * Places, streets, barrios and landmarks — plus, in the same payload, the
 * geocoder's reading of the query when it looks like a Nicaraguan address.
 *
 * @param {string} q
 * @param {{lat?: number, lon?: number, limit?: number, kind?: string,
 *          category?: string|null, radiusM?: number, signal?: AbortSignal}} [opts]
 * @returns {Promise<any>} `{query, hits, geocode, took_ms}`
 */
export function search(q, opts = {}) {
  return request(
    url('/search', {
      q,
      lat: opts.lat,
      lon: opts.lon,
      limit: opts.limit,
      kind: opts.kind,
      category: opts.category,
      radius_m: opts.radiusM,
    }),
    { signal: opts.signal },
  );
}

/**
 * Resolve an address string to scored candidates.
 * @param {string} q
 * @param {{lat?: number, lon?: number, city?: string, limit?: number, signal?: AbortSignal}} [opts]
 * @returns {Promise<any>} `{query, candidates, parsed_as}`
 */
export function geocode(q, opts = {}) {
  return request(
    url('/geocode', { q, lat: opts.lat, lon: opts.lon, city: opts.city, limit: opts.limit }),
    { signal: opts.signal },
  );
}

/**
 * Turn a point into the sentence a Nicaraguan would say out loud.
 * The first candidate carries the `relative` breakdown when one was found.
 * @param {number} lat
 * @param {number} lon
 * @param {{city?: string, signal?: AbortSignal}} [opts]
 * @returns {Promise<any>} `{query, candidates, parsed_as}`
 */
export function reverse(lat, lon, opts = {}) {
  return request(url('/reverse', { lat, lon, city: opts.city }), { signal: opts.signal });
}

/**
 * One place's full card.
 * @param {string} id POI id without the `poi:` index prefix
 * @param {{photos?: boolean, signal?: AbortSignal}} [opts]
 * @returns {Promise<any>} a `PoiCard`
 */
export function poi(id, opts = {}) {
  return request(url(`/poi/${encodeURIComponent(id)}`, { photos: opts.photos }), {
    signal: opts.signal,
  });
}

/**
 * POIs near a route — "is there a gas station before Masaya?".
 * @param {string} polyline encoded shape, precision 6
 * @param {{category?: string, radiusM?: number, limit?: number, signal?: AbortSignal}} [opts]
 * @returns {Promise<{hits: any[]}>}
 */
export function poisAlongRoute(polyline, opts = {}) {
  return request(
    url('/poi/along_route', {
      polyline,
      category: opts.category,
      radius_m: opts.radiusM,
      limit: opts.limit,
    }),
    { signal: opts.signal },
  );
}

/**
 * Route between two or more points.  The API injects active closures, so this
 * is the only path to Valhalla.
 * @param {object} body a `RouteRequest`
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<any>} the Valhalla response plus a `nicanav` block
 */
export function route(body, opts = {}) {
  return request(url('/route'), {
    method: 'POST',
    body,
    signal: opts.signal,
    timeoutMs: ROUTE_TIMEOUT_MS,
  });
}

/**
 * Report a closure, pothole, wrong one-way or shut business.
 * @param {object} body a `ReportSubmission`
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<{id: string, status: string, message?: string}>}
 */
export function report(body, opts = {}) {
  return request(url('/report'), { method: 'POST', body, signal: opts.signal });
}

/**
 * Suggest a place that is not on the map yet.
 * @param {object} body a `PoiSuggestion`
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<{id: string, status: string, message?: string}>}
 */
export function suggestPoi(body, opts = {}) {
  return request(url('/poi/suggest'), { method: 'POST', body, signal: opts.signal });
}

/**
 * Teach the geocoder: this address string belongs at this point.
 * @param {object} body an `AliasSubmission`
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<{id: string, status: string, message?: string}>}
 */
export function saveAlias(body, opts = {}) {
  return request(url('/alias'), { method: 'POST', body, signal: opts.signal });
}

/**
 * Per-dependency readiness.  "The map works but search is down" is a real
 * state on a one-box deployment, so this returns a block per service.
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<any>}
 */
export function health(opts = {}) {
  return request(url('/healthz'), { signal: opts.signal, timeoutMs: 5000 });
}
