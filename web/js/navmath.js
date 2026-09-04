/**
 * Pure navigation geometry and route normalisation.
 *
 * Nothing in this file touches the DOM, `window`, `navigator` or the network,
 * so the whole module runs under `node --test` (see tests/js/navmath.test.mjs).
 * That matters more here than anywhere else in the client: this is the code
 * that decides whether the driver is told to turn, and it is the only part of
 * the nav stack that can be checked without a car.
 *
 * Conventions mirror `common/geo.py` so a value means the same thing on both
 * sides of the wire: points are `{lat, lon}` (never `lng`, never bare x/y),
 * distances are metres, durations seconds, bearings degrees clockwise from
 * north.
 */

/** Valhalla encodes every shape at precision 6, not Google's 5. */
export const VALHALLA_PRECISION = 6;

/** Mean Earth radius, same constant as common/geo.py. */
export const EARTH_RADIUS_M = 6371008.8;

/**
 * Padded whole-country box, copied from `common.geo.NICARAGUA_BBOX`.
 * Used only as a sanity check on decoded shapes — see {@link decodeRouteShape}.
 */
export const NICARAGUA_BBOX = Object.freeze({
  minLon: -87.75,
  minLat: 10.68,
  maxLon: -82.6,
  maxLat: 15.05,
});

/** docs/PLAN.md 6.1: off-route is >40 m from the line for 3 fixes above 5 km/h. */
export const OFF_ROUTE_THRESHOLD_M = 40;
export const OFF_ROUTE_FIXES = 3;
export const MOVING_SPEED_MPS = 5 / 3.6;

/** docs/PLAN.md 6.1: arrival is within 30 m of the destination under 5 km/h. */
export const ARRIVAL_RADIUS_M = 30;

const DEG = Math.PI / 180;
const RAD = 180 / Math.PI;

// --------------------------------------------------------------------------- //
// Encoded polyline codec
// --------------------------------------------------------------------------- //

/**
 * Decode an encoded polyline into `[{lat, lon}, ...]`.
 *
 * Lenient in the same way `common.polyline.decode` is: a truncated payload
 * yields the prefix that decoded cleanly rather than throwing, because half a
 * route drawn on the map is more useful mid-drive than an exception.
 *
 * @param {string} str encoded polyline
 * @param {number} [precision=6] decimal places baked into the encoding
 * @returns {{lat: number, lon: number}[]}
 */
export function decodePolyline(str, precision = VALHALLA_PRECISION) {
  if (typeof str !== 'string' || str.length === 0) return [];
  const factor = 10 ** precision;
  const points = [];
  const length = str.length;
  let index = 0;
  let lat = 0;
  let lon = 0;

  while (index < length) {
    let shift = 0;
    let result = 0;
    let byte = 0;
    do {
      if (index >= length) return points;
      byte = str.charCodeAt(index++) - 63;
      result |= (byte & 0x1f) << shift;
      shift += 5;
    } while (byte >= 0x20);
    lat += result & 1 ? ~(result >> 1) : result >> 1;

    shift = 0;
    result = 0;
    do {
      if (index >= length) return points;
      byte = str.charCodeAt(index++) - 63;
      result |= (byte & 0x1f) << shift;
      shift += 5;
    } while (byte >= 0x20);
    lon += result & 1 ? ~(result >> 1) : result >> 1;

    points.push({ lat: lat / factor, lon: lon / factor });
  }
  return points;
}

/**
 * Encode `[{lat, lon}, ...]`; the inverse of {@link decodePolyline}.
 *
 * The client needs this to hand a route shape back to `/api/poi/along_route`,
 * which takes an encoded polyline at precision 6.
 *
 * @param {{lat: number, lon: number}[]} points
 * @param {number} [precision=6]
 * @returns {string}
 */
export function encodePolyline(points, precision = VALHALLA_PRECISION) {
  const factor = 10 ** precision;
  let out = '';
  let prevLat = 0;
  let prevLon = 0;
  for (const point of points) {
    const ilat = Math.round(point.lat * factor);
    const ilon = Math.round(point.lon * factor);
    out += encodeSigned(ilat - prevLat) + encodeSigned(ilon - prevLon);
    prevLat = ilat;
    prevLon = ilon;
  }
  return out;
}

function encodeSigned(delta) {
  let value = delta < 0 ? ~(delta << 1) : delta << 1;
  let out = '';
  while (value >= 0x20) {
    out += String.fromCharCode((0x20 | (value & 0x1f)) + 63);
    value >>>= 5;
  }
  return out + String.fromCharCode(value + 63);
}

/**
 * Decode a route shape, catching the precision-5/6 mix-up before it reaches the map.
 *
 * Reading a precision-6 shape at precision 5 multiplies every coordinate by ten
 * and drops the route in the middle of the Pacific; reading a precision-5 shape
 * at 6 divides by ten and puts it in the Gulf of Guinea. Both failures are
 * silent — the map just pans somewhere absurd — so the decoded first vertex is
 * checked against Nicaragua's box and the other precision is tried before
 * giving up.
 *
 * @param {string} encoded
 * @param {{precision?: number, bbox?: typeof NICARAGUA_BBOX}} [options]
 * @returns {{points: {lat: number, lon: number}[], precision: number, inBbox: boolean}}
 */
export function decodeRouteShape(encoded, options = {}) {
  const { precision = VALHALLA_PRECISION, bbox = NICARAGUA_BBOX } = options;
  const primary = decodePolyline(encoded, precision);
  if (primary.length === 0 || withinBbox(primary[0], bbox)) {
    return { points: primary, precision, inBbox: primary.length > 0 };
  }
  const alternate = precision === 6 ? 5 : 6;
  const fallback = decodePolyline(encoded, alternate);
  if (fallback.length > 0 && withinBbox(fallback[0], bbox)) {
    return { points: fallback, precision: alternate, inBbox: true };
  }
  // Neither reading lands in Nicaragua. Hand back the requested precision and
  // let the caller decide; a route outside the country is legitimate input for
  // a test fixture, and refusing to decode would be worse than a wrong map.
  return { points: primary, precision, inBbox: false };
}

function withinBbox(point, bbox) {
  return (
    point.lat >= bbox.minLat &&
    point.lat <= bbox.maxLat &&
    point.lon >= bbox.minLon &&
    point.lon <= bbox.maxLon
  );
}

// --------------------------------------------------------------------------- //
// Geodesy
// --------------------------------------------------------------------------- //

/**
 * Great-circle distance between two `{lat, lon}` points, in metres.
 *
 * @param {{lat: number, lon: number}} a
 * @param {{lat: number, lon: number}} b
 * @returns {number}
 */
export function haversineM(a, b) {
  const phi1 = a.lat * DEG;
  const phi2 = b.lat * DEG;
  const dphi = phi2 - phi1;
  const dlambda = (b.lon - a.lon) * DEG;
  const h =
    Math.sin(dphi / 2) ** 2 +
    Math.cos(phi1) * Math.cos(phi2) * Math.sin(dlambda / 2) ** 2;
  return 2 * EARTH_RADIUS_M * Math.asin(Math.min(1, Math.sqrt(h)));
}

/**
 * Forward azimuth from `a` to `b`, degrees clockwise from north in [0, 360).
 *
 * @param {{lat: number, lon: number}} a
 * @param {{lat: number, lon: number}} b
 * @returns {number}
 */
export function bearingDeg(a, b) {
  const phi1 = a.lat * DEG;
  const phi2 = b.lat * DEG;
  const dlambda = (b.lon - a.lon) * DEG;
  const y = Math.sin(dlambda) * Math.cos(phi2);
  const x =
    Math.cos(phi1) * Math.sin(phi2) -
    Math.sin(phi1) * Math.cos(phi2) * Math.cos(dlambda);
  return (Math.atan2(y, x) * RAD + 360) % 360;
}

/**
 * Travel `distanceM` along `bearing` from a point.
 *
 * @param {{lat: number, lon: number}} point
 * @param {number} bearing degrees clockwise from north
 * @param {number} distanceM
 * @returns {{lat: number, lon: number}}
 */
export function destination(point, bearing, distanceM) {
  const delta = distanceM / EARTH_RADIUS_M;
  const theta = bearing * DEG;
  const phi1 = point.lat * DEG;
  const lambda1 = point.lon * DEG;
  const sinPhi2 =
    Math.sin(phi1) * Math.cos(delta) +
    Math.cos(phi1) * Math.sin(delta) * Math.cos(theta);
  const phi2 = Math.asin(Math.max(-1, Math.min(1, sinPhi2)));
  const lambda2 =
    lambda1 +
    Math.atan2(
      Math.sin(theta) * Math.sin(delta) * Math.cos(phi1),
      Math.cos(delta) - Math.sin(phi1) * Math.sin(phi2),
    );
  return { lat: phi2 * RAD, lon: ((lambda2 * RAD + 540) % 360) - 180 };
}

/**
 * Smallest signed difference between two bearings, in (-180, 180].
 *
 * @param {number} from
 * @param {number} to
 * @returns {number} positive when `to` is clockwise of `from`
 */
export function bearingDeltaDeg(from, to) {
  let delta = ((to - from + 540) % 360) - 180;
  if (delta === -180) delta = 180;
  return delta;
}

/**
 * Build the local equirectangular frame used for snapping, anchored at a point.
 *
 * Accurate to ~0.1 % over the tens of kilometres a Nicaraguan route covers,
 * which is an order of magnitude below OSM's own positional error here, and it
 * costs two multiplications per vertex instead of six trig calls — the reason
 * the snap loop can afford to scan a whole 45 km route on every GPS fix.
 *
 * @param {number} lat0
 * @param {number} lon0
 * @returns {{toXY: (lat: number, lon: number) => [number, number],
 *            toLatLon: (x: number, y: number) => {lat: number, lon: number}}}
 */
export function localProjection(lat0, lon0) {
  const mPerDegLat =
    111132.92 - 559.82 * Math.cos(2 * lat0 * DEG) + 1.175 * Math.cos(4 * lat0 * DEG);
  const mPerDegLon = 111412.84 * Math.cos(lat0 * DEG) - 93.5 * Math.cos(3 * lat0 * DEG);
  return {
    toXY: (lat, lon) => [(lon - lon0) * mPerDegLon, (lat - lat0) * mPerDegLat],
    toLatLon: (x, y) => ({ lat: lat0 + y / mPerDegLat, lon: lon0 + x / mPerDegLon }),
  };
}

// --------------------------------------------------------------------------- //
// Along-line measures and snapping
// --------------------------------------------------------------------------- //

/**
 * Distance from the start of the shape to each vertex, in metres.
 *
 * `result[0]` is 0 and `result[n - 1]` is the route length, so a route's total
 * length and every step boundary come from one pass instead of being recomputed.
 *
 * @param {{lat: number, lon: number}[]} shape
 * @returns {Float64Array}
 */
export function cumulativeDistances(shape) {
  const out = new Float64Array(shape.length);
  for (let i = 1; i < shape.length; i += 1) {
    out[i] = out[i - 1] + haversineM(shape[i - 1], shape[i]);
  }
  return out;
}

/**
 * Snap a GPS fix onto the route line.
 *
 * Clamps to the ends: a point before the start snaps to vertex 0 with
 * `alongM === 0`, and a point past the finish snaps to the last vertex with
 * `alongM === routeLength`. That is what makes the "driver overshot the
 * destination" case behave instead of producing a negative distance.
 *
 * `alongM` is interpolated between the *cumulative* distances of the bracketing
 * vertices rather than added as a planar length, so it is monotonic and never
 * exceeds the route length even where the projection is slightly off.
 *
 * @param {{lat: number, lon: number}} point
 * @param {{lat: number, lon: number}[]} shape
 * @param {{cumulative?: Float64Array, startIndex?: number, endIndex?: number}} [options]
 *   `startIndex`/`endIndex` restrict the scan to a window of the shape; nav.js
 *   uses that to stay cheap on long routes, but the default is a full scan
 *   because off-route detection must be able to see the whole line.
 * @returns {{point: {lat: number, lon: number}, index: number, distanceM: number,
 *            alongM: number, t: number, bearingDeg: number}}
 */
export function snapToRoute(point, shape, options = {}) {
  if (!Array.isArray(shape) || shape.length === 0) {
    throw new Error('snapToRoute needs at least one shape vertex');
  }
  const cumulative = options.cumulative ?? cumulativeDistances(shape);
  if (shape.length === 1) {
    return {
      point: { ...shape[0] },
      index: 0,
      distanceM: haversineM(point, shape[0]),
      alongM: 0,
      t: 0,
      bearingDeg: 0,
    };
  }

  const startIndex = Math.max(0, options.startIndex ?? 0);
  const endIndex = Math.min(shape.length - 1, options.endIndex ?? shape.length - 1);
  const { toXY, toLatLon } = localProjection(point.lat, point.lon);
  const [px, py] = toXY(point.lat, point.lon);

  let bestDistance = Infinity;
  let bestIndex = startIndex;
  let bestT = 0;
  let bestX = 0;
  let bestY = 0;

  for (let i = startIndex; i < endIndex; i += 1) {
    const [ax, ay] = toXY(shape[i].lat, shape[i].lon);
    const [bx, by] = toXY(shape[i + 1].lat, shape[i + 1].lon);
    const dx = bx - ax;
    const dy = by - ay;
    const lengthSq = dx * dx + dy * dy;
    if (lengthSq === 0) continue;
    const t = Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / lengthSq));
    const cx = ax + t * dx;
    const cy = ay + t * dy;
    const distance = Math.hypot(px - cx, py - cy);
    if (distance < bestDistance) {
      bestDistance = distance;
      bestIndex = i;
      bestT = t;
      bestX = cx;
      bestY = cy;
    }
  }

  if (bestDistance === Infinity) {
    // Degenerate line: every segment in the window is zero-length.
    return {
      point: { ...shape[startIndex] },
      index: startIndex,
      distanceM: haversineM(point, shape[startIndex]),
      alongM: cumulative[startIndex],
      t: 0,
      bearingDeg: 0,
    };
  }

  const a = cumulative[bestIndex];
  const b = cumulative[bestIndex + 1];
  return {
    point: toLatLon(bestX, bestY),
    index: bestIndex,
    distanceM: bestDistance,
    alongM: a + bestT * (b - a),
    t: bestT,
    bearingDeg: bearingDeg(shape[bestIndex], shape[bestIndex + 1]),
  };
}

/**
 * Metres left to drive from a point `alongM` into the route.
 *
 * @param {{lat: number, lon: number}[]} shape
 * @param {number} alongM
 * @param {Float64Array} [cumulative]
 * @returns {number} never negative
 */
export function remainingDistanceM(shape, alongM, cumulative) {
  const cum = cumulative ?? cumulativeDistances(shape);
  const total = cum.length ? cum[cum.length - 1] : 0;
  return Math.max(0, total - Math.max(0, alongM));
}

/**
 * The point sitting `alongM` metres into the shape, clamped to both ends.
 *
 * @param {{lat: number, lon: number}[]} shape
 * @param {number} alongM
 * @param {Float64Array} [cumulative]
 * @returns {{lat: number, lon: number}}
 */
export function pointAtDistance(shape, alongM, cumulative) {
  if (!shape.length) throw new Error('pointAtDistance needs at least one vertex');
  const cum = cumulative ?? cumulativeDistances(shape);
  const total = cum[cum.length - 1];
  if (alongM <= 0 || shape.length === 1) return { ...shape[0] };
  if (alongM >= total) return { ...shape[shape.length - 1] };
  let low = 0;
  let high = shape.length - 1;
  while (high - low > 1) {
    const mid = (low + high) >> 1;
    if (cum[mid] <= alongM) low = mid;
    else high = mid;
  }
  const span = cum[high] - cum[low];
  if (span <= 0) return { ...shape[low] };
  const t = (alongM - cum[low]) / span;
  return destination(shape[low], bearingDeg(shape[low], shape[high]), t * span);
}

/**
 * The slice of the shape from `alongM` to the end, for drawing the road ahead.
 *
 * @param {{lat: number, lon: number}[]} shape
 * @param {number} alongM
 * @param {Float64Array} [cumulative]
 * @returns {{lat: number, lon: number}[]}
 */
export function shapeAfter(shape, alongM, cumulative) {
  const cum = cumulative ?? cumulativeDistances(shape);
  if (alongM <= 0) return shape.slice();
  const head = pointAtDistance(shape, alongM, cum);
  const rest = [];
  for (let i = 0; i < shape.length; i += 1) {
    if (cum[i] > alongM) rest.push(shape[i]);
  }
  return [head, ...rest];
}

// --------------------------------------------------------------------------- //
// Off-route and arrival rules
// --------------------------------------------------------------------------- //

/**
 * Decide whether the driver has actually left the route.
 *
 * Both halves of the rule matter. Distance alone is not enough: a parked phone
 * in downtown Managua drifts tens of metres between fixes, and a reroute from a
 * stationary car is worse than useless — it burns a request, talks over
 * nothing, and can swap the route for a worse one while the driver is looking
 * for parking. Requiring `consecutive` fixes filters the single bad fix you get
 * every time you pass under the Pista Juan Pablo II overpasses.
 *
 * Fixes with a non-finite speed count as *not moving*: nav.js is responsible
 * for deriving a speed from successive positions when Android reports
 * `coords.speed === null`, and if even that is unavailable the safe answer is
 * to stay on the current route.
 *
 * @param {{distanceM: number, speedMps: number}[]} history oldest first
 * @param {number} [thresholdM=40]
 * @param {number} [consecutive=3]
 * @param {number} [minSpeedMps=1.389] 5 km/h
 * @returns {boolean}
 */
export function isOffRoute(
  history,
  thresholdM = OFF_ROUTE_THRESHOLD_M,
  consecutive = OFF_ROUTE_FIXES,
  minSpeedMps = MOVING_SPEED_MPS,
) {
  if (!Array.isArray(history) || history.length < consecutive || consecutive < 1) {
    return false;
  }
  for (let i = history.length - consecutive; i < history.length; i += 1) {
    const fix = history[i];
    const distance = Number(fix?.distanceM);
    const speed = Number(fix?.speedMps);
    if (!Number.isFinite(distance) || distance <= thresholdM) return false;
    if (!Number.isFinite(speed) || speed <= minSpeedMps) return false;
  }
  return true;
}

/**
 * Arrival test: close to the destination *and* slowed down.
 *
 * The speed half stops the banner collapsing to "llegaste" while the driver is
 * still doing 50 km/h past the door of the place on Carretera a Masaya, which
 * is exactly when the last instruction is still needed.
 *
 * @param {{lat: number, lon: number}} point
 * @param {{lat: number, lon: number}} target
 * @param {number} speedMps
 * @param {{radiusM?: number, maxSpeedMps?: number}} [options]
 * @returns {boolean}
 */
export function isArrival(point, target, speedMps, options = {}) {
  const radiusM = options.radiusM ?? ARRIVAL_RADIUS_M;
  const maxSpeedMps = options.maxSpeedMps ?? MOVING_SPEED_MPS;
  if (!point || !target) return false;
  if (haversineM(point, target) > radiusM) return false;
  const speed = Number(speedMps);
  // An unknown speed near the destination is treated as stopped: the driver is
  // 30 m away, and a false "llegaste" there costs far less than never arriving.
  return !Number.isFinite(speed) || speed <= maxSpeedMps;
}

// --------------------------------------------------------------------------- //
// Maneuver progress
// --------------------------------------------------------------------------- //

/**
 * Locate the driver among the normalised steps.
 *
 * Every normalised step carries the maneuver that happens at its *end* (see
 * {@link normalizeRoute}), so `current.maneuver` is the thing the banner shows
 * and `distanceToManeuverM` is the number next to it.
 *
 * @param {number} alongM distance travelled into the route
 * @param {ReturnType<typeof normalizeRoute>['steps']} steps
 * @returns {{index: number, current: object, next: object|null,
 *            distanceToManeuverM: number, fractionOfStep: number,
 *            isFinalStep: boolean}|null}
 */
export function maneuverProgress(alongM, steps) {
  if (!Array.isArray(steps) || steps.length === 0) return null;
  const position = Math.max(0, alongM);

  let index = 0;
  let low = 0;
  let high = steps.length - 1;
  while (low <= high) {
    const mid = (low + high) >> 1;
    if (steps[mid].beginAlongM <= position) {
      index = mid;
      low = mid + 1;
    } else {
      high = mid - 1;
    }
  }
  // A zero-length trailing step (Valhalla's destination maneuver, OSRM's
  // "arrive") would otherwise swallow the driver as soon as they touch its
  // begin distance and leave the banner with nothing to point at.
  while (index > 0 && steps[index].endAlongM <= steps[index].beginAlongM) {
    index -= 1;
  }

  const current = steps[index];
  const next = steps[index + 1] ?? null;
  const span = current.endAlongM - current.beginAlongM;
  return {
    index,
    current,
    next,
    distanceToManeuverM: Math.max(0, current.endAlongM - position),
    fractionOfStep: span > 0 ? Math.min(1, Math.max(0, (position - current.beginAlongM) / span)) : 1,
    isFinalStep: index >= steps.length - 1,
  };
}

/**
 * Seconds left on the route, from the step durations the router gave us.
 *
 * Prorating the current step by distance rather than scaling the whole route by
 * a single average keeps the ETA honest when the remaining distance is one long
 * carretera leg and the driven part was city grid — the two have wildly
 * different speeds in Managua.
 *
 * @param {ReturnType<typeof normalizeRoute>['steps']} steps
 * @param {number} alongM
 * @returns {number}
 */
export function remainingDurationS(steps, alongM) {
  if (!Array.isArray(steps) || steps.length === 0) return 0;
  const progress = maneuverProgress(alongM, steps);
  if (!progress) return 0;
  let seconds = 0;
  for (let i = progress.index + 1; i < steps.length; i += 1) seconds += steps[i].durationS;
  const current = progress.current;
  seconds += current.durationS * (1 - progress.fractionOfStep);
  return Math.max(0, seconds);
}

// --------------------------------------------------------------------------- //
// Distance wording
// --------------------------------------------------------------------------- //

const DISTANCE_WORDS = {
  es: { km: 'km', m: 'm', kmSpoken: ['kilómetro', 'kilómetros'], mSpoken: 'metros', now: 'ahora' },
  en: { km: 'km', m: 'm', kmSpoken: ['kilometer', 'kilometers'], mSpoken: 'meters', now: 'now' },
};

/**
 * Round a distance the way a driver reads it off a banner.
 *
 * Rounding is coarse on purpose: "347 m" implies a precision the GPS does not
 * have and takes longer to read at 60 km/h than "350 m".
 *
 * @param {number} metres
 * @param {string} [lang='es'] `es` or `en`; anything else falls back to `es`
 * @returns {string}
 */
export function formatDistance(metres, lang = 'es') {
  const words = DISTANCE_WORDS[lang?.slice(0, 2)] ?? DISTANCE_WORDS.es;
  const value = Math.max(0, Number(metres) || 0);
  if (value >= 1000) {
    const km = value / 1000;
    const decimals = km >= 10 ? 0 : 1;
    const text = km.toFixed(decimals);
    return `${words === DISTANCE_WORDS.es ? text.replace('.', ',') : text} ${words.km}`;
  }
  if (value >= 100) return `${Math.round(value / 50) * 50} ${words.m}`;
  if (value >= 10) return `${Math.round(value / 10) * 10} ${words.m}`;
  return `${Math.max(0, Math.round(value / 5) * 5)} ${words.m}`;
}

/**
 * The same distance, spelled out for text-to-speech.
 *
 * Abbreviations are expanded because Android's Spanish TTS reads "350 m" as
 * "trescientos cincuenta eme" often enough to matter.
 *
 * @param {number} metres
 * @param {string} [lang='es']
 * @returns {string}
 */
export function spokenDistance(metres, lang = 'es') {
  const code = lang?.slice(0, 2) === 'en' ? 'en' : 'es';
  const words = DISTANCE_WORDS[code];
  const value = Math.max(0, Number(metres) || 0);
  if (value < 15) return words.now;
  if (value >= 1000) {
    const km = value / 1000;
    if (Math.abs(km - Math.round(km)) < 0.05) {
      const whole = Math.round(km);
      return `${whole} ${whole === 1 ? words.kmSpoken[0] : words.kmSpoken[1]}`;
    }
    const text = km.toFixed(1);
    return `${code === 'es' ? text.replace('.', ',') : text} ${words.kmSpoken[1]}`;
  }
  const rounded = value >= 100 ? Math.round(value / 50) * 50 : Math.round(value / 10) * 10;
  return `${rounded} ${words.mSpoken}`;
}

// --------------------------------------------------------------------------- //
// Maneuver classification
// --------------------------------------------------------------------------- //

/**
 * Turn angles used to draw the banner arrow, in degrees: 0 straight ahead,
 * positive clockwise (right), ±180 a U-turn.
 */
const MODIFIER_ANGLE = {
  straight: 0,
  'slight right': 35,
  right: 90,
  'sharp right': 135,
  'slight left': -35,
  left: -90,
  'sharp left': -135,
  uturn: 180,
};

/**
 * Valhalla's native maneuver `type` is an integer enum. Values 0-27 are the
 * documented set the auto profile actually emits in Nicaragua; the ferry and
 * transit values above them are mapped optimistically.
 *
 * UNVERIFIED: the numbering above 27 (ferry/transit/elevator) has not been
 * checked against a running Valhalla — confirm on first deploy by routing to
 * Ometepe, which is the only ferry a Nicaraguan auto route can hit.
 */
const VALHALLA_TYPE = {
  0: ['continue', 0],
  1: ['depart', 0],
  2: ['depart', 45],
  3: ['depart', -45],
  4: ['arrive', 0],
  5: ['arrive', 45],
  6: ['arrive', -45],
  7: ['continue', 0],
  8: ['continue', 0],
  9: ['turn', 35],
  10: ['turn', 90],
  11: ['turn', 135],
  12: ['uturn', 180],
  13: ['uturn', -180],
  14: ['turn', -135],
  15: ['turn', -90],
  16: ['turn', -35],
  17: ['ramp', 0],
  18: ['ramp', 60],
  19: ['ramp', -60],
  20: ['exit', 60],
  21: ['exit', -60],
  22: ['fork', 0],
  23: ['fork', 30],
  24: ['fork', -30],
  25: ['merge', 0],
  26: ['roundabout', 0],
  27: ['roundabout-exit', 0],
  28: ['ferry', 0],
  29: ['ferry', 0],
  37: ['merge', 30],
  38: ['merge', -30],
};

/**
 * Reduce a router maneuver to something the banner can draw.
 *
 * @param {{valhallaType?: number, osrmType?: string, modifier?: string, exit?: number}} raw
 * @returns {{kind: string, angleDeg: number, exit: number|null}}
 */
export function maneuverIcon(raw = {}) {
  const exit = Number.isFinite(raw.exit) ? raw.exit : null;

  if (Number.isFinite(raw.valhallaType) && VALHALLA_TYPE[raw.valhallaType]) {
    const [kind, angle] = VALHALLA_TYPE[raw.valhallaType];
    return { kind, angleDeg: angle, exit };
  }

  const modifier = String(raw.modifier ?? '').toLowerCase();
  const angle = MODIFIER_ANGLE[modifier] ?? 0;
  const osrm = String(raw.osrmType ?? '').toLowerCase();
  switch (osrm) {
    case 'depart':
      return { kind: 'depart', angleDeg: angle, exit };
    case 'arrive':
      return { kind: 'arrive', angleDeg: angle, exit };
    case 'roundabout':
    case 'rotary':
    case 'roundabout turn':
      return { kind: 'roundabout', angleDeg: angle, exit };
    case 'exit roundabout':
    case 'exit rotary':
      return { kind: 'roundabout-exit', angleDeg: angle, exit };
    case 'merge':
      return { kind: 'merge', angleDeg: angle, exit };
    case 'on ramp':
      return { kind: 'ramp', angleDeg: angle || 60, exit };
    case 'off ramp':
      return { kind: 'exit', angleDeg: angle || 60, exit };
    case 'fork':
      return { kind: 'fork', angleDeg: angle, exit };
    case 'end of road':
    case 'turn':
    case 'new name':
    case 'continue':
    case 'notification':
      return { kind: modifier === 'uturn' ? 'uturn' : 'turn', angleDeg: angle, exit };
    default:
      return { kind: modifier === 'uturn' ? 'uturn' : 'turn', angleDeg: angle, exit };
  }
}

const ES_TURN_PHRASE = {
  'slight right': 'Girá levemente a la derecha',
  right: 'Girá a la derecha',
  'sharp right': 'Girá cerrado a la derecha',
  'slight left': 'Girá levemente a la izquierda',
  left: 'Girá a la izquierda',
  'sharp left': 'Girá cerrado a la izquierda',
  straight: 'Seguí derecho',
  uturn: 'Hacé un retorno',
};

const ES_ORDINAL = [
  '',
  'primera',
  'segunda',
  'tercera',
  'cuarta',
  'quinta',
  'sexta',
  'séptima',
  'octava',
];

/**
 * Last-resort Spanish instruction, used only when the router sent no text.
 *
 * Valhalla normally writes the instruction itself (that is why `/api/route`
 * forces `language`), so this only fires if `banner_instructions` came back
 * empty. Written in the Nicaraguan register rather than translated from
 * peninsular Spanish: "rotonda", "retorno", voseo imperatives.
 *
 * @param {{kind: string, angleDeg: number, exit: number|null}} icon
 * @param {string|null} roadName
 * @returns {string}
 */
export function describeManeuverEs(icon, roadName) {
  const onto = roadName ? ` en ${roadName}` : '';
  switch (icon.kind) {
    case 'depart':
      return roadName ? `Salí por ${roadName}` : 'Arrancá';
    case 'arrive':
      return 'Llegaste a tu destino';
    case 'roundabout':
    case 'roundabout-exit': {
      const exit = icon.exit && icon.exit > 0 && icon.exit < ES_ORDINAL.length
        ? ` y tomá la ${ES_ORDINAL[icon.exit]} salida`
        : '';
      return `Entrá a la rotonda${exit}${onto}`;
    }
    case 'merge':
      return `Incorporate${onto}`;
    case 'ramp':
      return `Tomá la rampa${onto}`;
    case 'exit':
      return `Tomá la salida${onto}`;
    case 'fork':
      return `Mantenete ${icon.angleDeg > 0 ? 'a la derecha' : icon.angleDeg < 0 ? 'a la izquierda' : 'derecho'}${onto}`;
    case 'uturn':
      return `Hacé un retorno${onto}`;
    case 'ferry':
      return 'Tomá el ferry';
    default: {
      const key = angleToModifier(icon.angleDeg);
      return `${ES_TURN_PHRASE[key] ?? 'Seguí'}${onto}`;
    }
  }
}

function angleToModifier(angle) {
  const a = Number(angle) || 0;
  if (Math.abs(a) >= 170) return 'uturn';
  if (a >= 115) return 'sharp right';
  if (a >= 55) return 'right';
  if (a >= 18) return 'slight right';
  if (a <= -115) return 'sharp left';
  if (a <= -55) return 'left';
  if (a <= -18) return 'slight left';
  return 'straight';
}

// --------------------------------------------------------------------------- //
// Route normalisation
// --------------------------------------------------------------------------- //

const UNIT_TO_M = { kilometers: 1000, kilometres: 1000, km: 1000, miles: 1609.344, mi: 1609.344 };

/**
 * Voice-prompt distances derived for the native Valhalla format, in metres
 * remaining in the step. OSRM output carries its own `voiceInstructions`, so
 * this table is only used when the client asked for `format: "json"`.
 *
 * `minStepM` keeps a prompt from firing at the same moment as the one before
 * it: there is no point saying "en 1,5 kilómetros" on a 600 m block in Altamira.
 */
const NATIVE_PROMPTS = [
  { remainingM: 1500, minStepM: 2500, priority: 'normal' },
  { remainingM: 400, minStepM: 700, priority: 'normal' },
];

/** Seconds of travel before a turn at which the final "now do it" prompt fires. */
const FINAL_PROMPT_SECONDS = 8;
const FINAL_PROMPT_MIN_M = 60;
const FINAL_PROMPT_MAX_M = 250;

/**
 * Turn an `/api/route` response into the flat structure the nav loop drives.
 *
 * Handles both shapes the API can return, because `RouteRequest.format` is
 * `"json"` (Valhalla native) or `"osrm"` and the proxy passes either straight
 * through:
 *
 * * native — `trip.legs[].shape` (encoded polyline 6) plus `maneuvers[]` with
 *   `begin_shape_index`/`end_shape_index` and `verbal_*` strings.
 * * osrm — `routes[].legs[].steps[]`, where Valhalla writes each maneuver's
 *   `bannerInstructions`/`voiceInstructions` onto the **previous** step
 *   (Mapbox's convention) and leaves them empty on the final `arrive` step.
 *
 * Both are collapsed to the same thing: one continuous shape, and a list of
 * steps each of which carries the maneuver that happens at *its own end*. That
 * is the only arrangement where "what do I announce, and how far away is it"
 * has a single answer.
 *
 * @param {object} response the `/api/route` payload
 * @param {{alternateIndex?: number|null, lang?: string}} [options]
 * @returns {{format: 'valhalla'|'osrm', shape: {lat: number, lon: number}[],
 *            cumulative: Float64Array, totalM: number, totalDurationS: number,
 *            steps: object[], language: string}}
 */
export function normalizeRoute(response, options = {}) {
  const lang = options.lang ?? 'es';
  if (response && Array.isArray(response.routes) && response.routes.length) {
    return normalizeOsrm(response, options, lang);
  }
  if (response && (response.trip || Array.isArray(response.alternates))) {
    return normalizeValhalla(response, options, lang);
  }
  throw new Error('normalizeRoute: unrecognised route payload');
}

function normalizeValhalla(response, options, lang) {
  const index = options.alternateIndex;
  const trip =
    Number.isInteger(index) && index >= 0
      ? response.alternates?.[index]?.trip ?? response.trip
      : response.trip;
  if (!trip || !Array.isArray(trip.legs) || trip.legs.length === 0) {
    throw new Error('normalizeRoute: Valhalla trip has no legs');
  }
  const unitFactor = UNIT_TO_M[String(trip.units ?? 'kilometers').toLowerCase()] ?? 1000;

  const shape = [];
  const rawManeuvers = [];
  for (const leg of trip.legs) {
    const offset = shape.length;
    const decoded = decodeRouteShape(leg.shape ?? '').points;
    // Legs share their joint vertex; keeping both would add a zero-length
    // segment and put two step boundaries at the same distance.
    const slice = offset > 0 && decoded.length ? decoded.slice(1) : decoded;
    const legBase = offset > 0 ? offset - 1 : 0;
    for (const point of slice) shape.push(point);
    for (const maneuver of leg.maneuvers ?? []) {
      rawManeuvers.push({ maneuver, base: legBase });
    }
  }
  if (shape.length < 2) throw new Error('normalizeRoute: Valhalla shape is too short');

  const cumulative = cumulativeDistances(shape);
  const totalM = cumulative[cumulative.length - 1];
  const last = shape.length - 1;
  const at = (i) => cumulative[Math.max(0, Math.min(last, i))];

  const steps = [];
  for (let i = 0; i < rawManeuvers.length - 1; i += 1) {
    const { maneuver, base } = rawManeuvers[i];
    const nextRaw = rawManeuvers[i + 1].maneuver;
    const beginAlongM = at(base + (maneuver.begin_shape_index ?? 0));
    const endAlongM = at(base + (maneuver.end_shape_index ?? 0));
    const lengthM = Math.max(0, endAlongM - beginAlongM);
    const durationS = Number(maneuver.time) || 0;
    const icon = maneuverIcon({
      valhallaType: Number(nextRaw.type),
      exit: Number(nextRaw.roundabout_exit_count) || null,
    });
    const roadName = joinNames(maneuver.street_names);
    const nextRoadName = joinNames(nextRaw.street_names ?? nextRaw.begin_street_names);
    const instruction = nextRaw.instruction || describeManeuverEs(icon, nextRoadName);

    steps.push({
      index: steps.length,
      beginAlongM,
      endAlongM,
      lengthM: lengthM || Number(maneuver.length) * unitFactor || 0,
      durationS,
      roadName,
      maneuver: {
        ...icon,
        instruction,
        roadName: nextRoadName,
        isArrival: icon.kind === 'arrive',
      },
      lanes: [], // Valhalla emits turn lanes only in its OSRM output.
      voice: nativePrompts({ maneuver, nextRaw, lengthM, durationS, icon, nextRoadName, lang }),
    });
  }

  if (steps.length === 0) {
    // Single-maneuver trip (origin and destination on the same edge).
    const only = rawManeuvers[0]?.maneuver;
    steps.push(fallbackArrivalStep(totalM, Number(trip.summary?.time) || 0, only, lang));
  }

  return {
    format: 'valhalla',
    shape,
    cumulative,
    totalM,
    totalDurationS: Number(trip.summary?.time) || steps.reduce((s, x) => s + x.durationS, 0),
    steps,
    language: trip.language ?? lang,
  };
}

function nativePrompts({ maneuver, nextRaw, lengthM, durationS, icon, nextRoadName, lang }) {
  const prompts = [];
  const post = clean(maneuver.verbal_post_transition_instruction);
  // "Continúe por 2 kilómetros" belongs at the *start* of the step: it is said
  // right after the previous turn completes, which is when remaining === length.
  if (post && lengthM > 400) {
    prompts.push({ remainingM: lengthM, text: post, priority: 'normal' });
  }

  const alert = clean(nextRaw.verbal_transition_alert_instruction);
  if (alert) {
    for (const rule of NATIVE_PROMPTS) {
      if (lengthM < rule.minStepM) continue;
      // Valhalla's alert string carries no distance of its own, so the distance
      // is prefixed here.
      // UNVERIFIED: confirm against a live Valhalla that
      // verbal_transition_alert_instruction really is distance-free in Spanish;
      // if it is not, this produces "En 400 metros, en 400 metros, gire...".
      prompts.push({
        remainingM: rule.remainingM,
        text: `${lang === 'en' ? 'In' : 'En'} ${spokenDistance(rule.remainingM, lang)}, ${lowerFirst(alert)}`,
        priority: rule.priority,
      });
    }
  }

  const speedMps = lengthM > 0 && durationS > 0 ? lengthM / durationS : 11;
  const finalAt = Math.min(
    FINAL_PROMPT_MAX_M,
    Math.max(FINAL_PROMPT_MIN_M, speedMps * FINAL_PROMPT_SECONDS),
  );
  const pre =
    clean(nextRaw.verbal_pre_transition_instruction) ||
    clean(nextRaw.instruction) ||
    describeManeuverEs(icon, nextRoadName);
  prompts.push({ remainingM: Math.min(finalAt, Math.max(20, lengthM)), text: pre, priority: 'urgent' });

  // Largest trigger distance first: the loop fires the closest one that matches
  // and marks the rest spent, so a GPS jump never produces three prompts at once.
  return dedupePrompts(prompts);
}

function normalizeOsrm(response, options, lang) {
  const index = options.alternateIndex;
  const route =
    Number.isInteger(index) && index >= 0 && response.routes[index]
      ? response.routes[index]
      : response.routes[0];
  const rawSteps = [];
  const shape = [];
  const bounds = [];
  for (const leg of route.legs ?? []) {
    const legOrigin = shape.length ? shape.length - 1 : 0;
    for (const step of leg.steps ?? []) {
      const points = geometryPoints(step.geometry);
      const begin = shape.length ? shape.length - 1 : 0;
      const slice = shape.length && points.length ? points.slice(1) : points;
      for (const point of slice) shape.push(point);
      bounds.push([begin, Math.max(begin, shape.length - 1)]);
      // Leg-local vertex index: `annotation.*` arrays are indexed per leg, one
      // entry per segment, so the step's own offset inside its leg is what
      // looks a maxspeed up.
      rawSteps.push({ step, annotation: leg.annotation ?? null, legLocalBegin: begin - legOrigin });
    }
  }
  if (rawSteps.length === 0) {
    for (const point of geometryPoints(route.geometry)) shape.push(point);
  }
  if (shape.length < 2) throw new Error('normalizeRoute: OSRM shape is too short');

  const cumulative = cumulativeDistances(shape);
  const totalM = cumulative[cumulative.length - 1];

  const steps = [];
  for (let i = 0; i < rawSteps.length - 1; i += 1) {
    const { step, annotation, legLocalBegin } = rawSteps[i];
    const nextStep = rawSteps[i + 1].step;
    const [beginIndex, endIndex] = bounds[i];
    const beginAlongM = cumulative[beginIndex];
    const endAlongM = cumulative[endIndex];
    const maneuver = nextStep.maneuver ?? {};
    const icon = maneuverIcon({
      osrmType: maneuver.type,
      modifier: maneuver.modifier,
      exit: Number(maneuver.exit) || null,
    });
    const banner = pickBanner(step.bannerInstructions);
    const nextRoadName = clean(nextStep.name) || bannerText(banner);
    const instruction = bannerText(banner) || describeManeuverEs(icon, clean(nextStep.name));

    steps.push({
      index: steps.length,
      beginAlongM,
      endAlongM,
      lengthM: Math.max(0, endAlongM - beginAlongM),
      durationS: Number(step.duration) || 0,
      roadName: clean(step.name),
      maneuver: {
        ...icon,
        instruction,
        secondary: banner?.secondary?.text ? clean(banner.secondary.text) : null,
        roadName: nextRoadName,
        isArrival: icon.kind === 'arrive',
      },
      lanes: laneHints(step, nextStep),
      voice: dedupePrompts(osrmPrompts(step, icon, nextRoadName, lang)),
      speedLimitKph: null,
    });
  }

  if (steps.length === 0) {
    steps.push(fallbackArrivalStep(totalM, Number(route.duration) || 0, null, lang));
  }

  return {
    format: 'osrm',
    shape,
    cumulative,
    totalM: Number(route.distance) > 0 ? Number(route.distance) : totalM,
    totalDurationS: Number(route.duration) || steps.reduce((s, x) => s + x.durationS, 0),
    steps,
    language: lang,
  };
}

function osrmPrompts(step, icon, roadName, lang) {
  const list = Array.isArray(step.voiceInstructions) ? step.voiceInstructions : [];
  const prompts = list
    .map((entry) => ({
      // Mapbox/Valhalla semantics: metres *remaining in this step* when the
      // prompt should be spoken, not distance from the step's start.
      remainingM: Number(entry.distanceAlongGeometry),
      text: clean(entry.announcement),
      priority: 'normal',
    }))
    .filter((entry) => Number.isFinite(entry.remainingM) && entry.text);

  if (prompts.length === 0) {
    // Valhalla leaves the arrays empty when voice_instructions was not honoured;
    // without a fallback the drive would be silent, which is the one failure a
    // nav app may not have.
    const stepLength = Math.max(0, Number(step.distance) || 0);
    const stepDuration = Math.max(1, Number(step.duration) || 1);
    const speedMps = stepLength > 0 ? stepLength / stepDuration : 11;
    const finalAt = Math.min(
      FINAL_PROMPT_MAX_M,
      Math.max(FINAL_PROMPT_MIN_M, speedMps * FINAL_PROMPT_SECONDS),
    );
    const text = describeManeuverEs(icon, roadName);
    if (stepLength > 700) {
      prompts.push({
        remainingM: 400,
        text: `${lang === 'en' ? 'In' : 'En'} ${spokenDistance(400, lang)}, ${lowerFirst(text)}`,
        priority: 'normal',
      });
    }
    prompts.push({ remainingM: finalAt, text, priority: 'normal' });
  }

  // The closest prompt is the one that must interrupt whatever is being said.
  let closest = null;
  for (const prompt of prompts) {
    if (!closest || prompt.remainingM < closest.remainingM) closest = prompt;
  }
  if (closest) closest.priority = 'urgent';
  return prompts;
}

function laneHints(step, nextStep) {
  // OSRM puts the lane configuration for a maneuver on the intersection where
  // it happens, which is the *first* intersection of the following step.
  const fromIntersection = nextStep?.intersections?.[0]?.lanes;
  if (Array.isArray(fromIntersection) && fromIntersection.length) {
    return fromIntersection.map((lane) => ({
      indications: Array.isArray(lane.indications) ? lane.indications.slice() : [],
      valid: lane.valid !== false,
      active: lane.active === true || lane.valid === true,
    }));
  }
  // Mapbox-style sub-banner: components of type "lane" carry the same thing.
  const banner = pickBanner(step.bannerInstructions);
  const components = banner?.sub?.components;
  if (Array.isArray(components)) {
    const lanes = components
      .filter((component) => component?.type === 'lane')
      .map((component) => ({
        indications: Array.isArray(component.directions) ? component.directions.slice() : [],
        valid: component.active !== false,
        active: component.active === true,
      }));
    if (lanes.length) return lanes;
  }
  return [];
}

function pickBanner(banners) {
  if (!Array.isArray(banners) || banners.length === 0) return null;
  // The banner with the largest trigger distance is the one shown for most of
  // the step; the shorter ones are the "then" variants close to the junction.
  let best = banners[0];
  for (const banner of banners) {
    if (Number(banner.distanceAlongGeometry) > Number(best.distanceAlongGeometry)) best = banner;
  }
  return best;
}

function bannerText(banner) {
  const primary = banner?.primary;
  if (!primary) return '';
  if (primary.text) return clean(primary.text);
  if (Array.isArray(primary.components)) {
    return clean(primary.components.map((component) => component?.text ?? '').join(' '));
  }
  return '';
}

function geometryPoints(geometry) {
  if (typeof geometry === 'string') return decodeRouteShape(geometry).points;
  if (geometry && Array.isArray(geometry.coordinates)) {
    return geometry.coordinates.map(([lon, lat]) => ({ lat, lon }));
  }
  return [];
}

function fallbackArrivalStep(totalM, durationS, maneuver, lang) {
  const icon = { kind: 'arrive', angleDeg: 0, exit: null };
  const instruction =
    clean(maneuver?.instruction) ||
    (lang === 'en' ? 'You have arrived at your destination' : 'Llegaste a tu destino');
  return {
    index: 0,
    beginAlongM: 0,
    endAlongM: totalM,
    lengthM: totalM,
    durationS,
    roadName: null,
    maneuver: { ...icon, instruction, roadName: null, isArrival: true },
    lanes: [],
    voice: [{ remainingM: Math.min(150, totalM), text: instruction, priority: 'urgent' }],
  };
}

function dedupePrompts(prompts) {
  const sorted = prompts
    .filter((prompt) => prompt.text && Number.isFinite(prompt.remainingM))
    .sort((a, b) => b.remainingM - a.remainingM);
  const out = [];
  for (const prompt of sorted) {
    const previous = out[out.length - 1];
    // Two prompts less than 40 m apart would be spoken on top of each other.
    if (previous && previous.remainingM - prompt.remainingM < 40) continue;
    out.push(prompt);
  }
  return out;
}

function joinNames(names) {
  if (Array.isArray(names) && names.length) return clean(names.join(' / '));
  if (typeof names === 'string') return clean(names);
  return null;
}

function clean(text) {
  if (typeof text !== 'string') return '';
  return text.replace(/\s+/g, ' ').trim();
}

function lowerFirst(text) {
  if (!text) return text;
  // Only lowercase a plain initial capital: "Gire" -> "gire", but never "MGA".
  if (text.length > 1 && text[1] === text[1].toUpperCase() && text[1] !== text[1].toLowerCase()) {
    return text;
  }
  return text[0].toLowerCase() + text.slice(1);
}
