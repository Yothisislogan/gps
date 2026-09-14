/**
 * Drive the route without a car.
 *
 * Turn-by-turn is the one part of this stack that cannot be checked from a
 * desk: every interesting behaviour — a prompt spoken at the right distance,
 * an off-route detection, a reroute that does not propose a U-turn across the
 * median of the Carretera a Masaya — needs a moving GPS receiver. Waiting for
 * a field drive to find out that the banner never updates is how a week gets
 * spent.
 *
 * So the simulator replaces the position source, and nothing else. It hands
 * nav.js objects shaped exactly like a GeolocationPosition, at the same
 * cadence a real receiver does. Everything downstream — snapping, prompts,
 * off-route, rerouting, arrival — runs unmodified, which is the entire point:
 * a test that stubs the navigation loop tests the stub.
 *
 * Query parameters, all off unless asked for:
 *
 *   ?sim=1              drive the active route at 40 km/h, 1 Hz fixes,
 *                       +/-5 m of position noise, one 15 s signal dropout
 *   ?sim=1&speed=80     ... at 80 km/h
 *   ?sim=1&detour=1     leave the route a third of the way in, so the
 *                       off-route detector and one reroute actually fire
 *   ?gpx=<url>          replay a recorded track instead, at its own
 *                       timestamps — real receiver noise, real stops
 *
 * `?gpx=` implies simulation; `?sim=1` alone follows whatever route the app
 * has. A GPX replay is the more honest test of the two, because a synthetic
 * track sits exactly on the road centreline and a real one never does.
 */

import {
  bearingDeg,
  cumulativeDistances,
  destination,
  haversineM,
  pointAtDistance,
} from './navmath.js';

/** What a phone actually reports on a dash mount, roughly. */
export const FIX_INTERVAL_MS = 1000;
/** Default cruise, km/h. Managua traffic, not the open Panamericana. */
export const DEFAULT_SPEED_KMH = 40;
/** Fraction of the route at which `detour=1` wanders off, and for how long. */
export const DETOUR_AT = 0.4;
export const DETOUR_SPAN = 0.15;
/**
 * How far off the line the detour goes. isOffRoute() wants three consecutive
 * fixes past 40 m, so this has to clear that with room for jitter and for the
 * snap tolerance.
 */
export const DETOUR_OFFSET_M = 80;

/**
 * Position noise, metres. A phone on a dash mount is never exactly on the
 * centreline, and an off-route detector tuned against a track that is will be
 * tuned wrong.
 */
export const JITTER_M = 5;

/**
 * One dropout per drive, and where it starts.
 *
 * Signal goes on the Carretera Sur and in the cuts around Las Nubes. What that
 * must NOT do is look like leaving the route: no fixes arrive, so the off-route
 * history stops growing, and the fix on the far side is 165 m further along at
 * 40 km/h. A snapper that only searches forward from the last index gets that
 * wrong, and the driver is told to recalculate for no reason.
 */
export const GAP_AT = 0.6;
export const GAP_MS = 15_000;

/**
 * Read the simulator's settings out of a URL.
 *
 * @param {string | URL | {search: string}} [source] defaults to the page URL
 * @returns {{enabled: boolean, speedKmh: number, detour: boolean, gpx: string | null}}
 */
export function simulatorOptions(source) {
  const search =
    typeof source === 'string'
      ? source.includes('?')
        ? source.slice(source.indexOf('?'))
        : source
      : source && typeof source === 'object' && 'search' in source
        ? source.search
        : typeof location !== 'undefined'
          ? location.search
          : '';
  const params = new URLSearchParams(search);

  const gpx = params.get('gpx');
  const flag = params.get('sim');
  // ?sim=0 and ?sim=false mean off. Anything else present means on, because
  // `?sim` with no value is what people actually type.
  const simFlag = flag !== null && flag !== '0' && flag !== 'false';

  const speed = Number.parseFloat(params.get('speed') || '');
  return {
    enabled: simFlag || Boolean(gpx),
    speedKmh: Number.isFinite(speed) && speed > 0 ? speed : DEFAULT_SPEED_KMH,
    detour: params.get('detour') === '1' || params.get('detour') === 'true',
    gpx: gpx || null,
  };
}

// --------------------------------------------------------------------------- //
// The track
// --------------------------------------------------------------------------- //

/**
 * A drive along a shape, sampled by elapsed time.
 *
 * Deliberately pure: no timers, no browser, no navigator. `at(ms)` is a
 * function of elapsed time alone, so the whole thing is testable under
 * `node --test` and a test can jump an hour ahead in one call.
 */
export class SimulatedTrack {
  /**
   * @param {Array<{lat: number, lon: number}>} shape
   * @param {{speedKmh?: number, detour?: boolean, detourAt?: number,
   *          detourSpan?: number, detourOffsetM?: number, jitterM?: number,
   *          gapAt?: number, gapMs?: number}} [options]
   */
  constructor(shape, options = {}) {
    this.setShape(shape);
    this.speedMps = ((options.speedKmh ?? DEFAULT_SPEED_KMH) * 1000) / 3600;
    this.detour = Boolean(options.detour);
    this.detourAt = options.detourAt ?? DETOUR_AT;
    this.detourSpan = options.detourSpan ?? DETOUR_SPAN;
    this.detourOffsetM = options.detourOffsetM ?? DETOUR_OFFSET_M;
    this.jitterM = options.jitterM ?? JITTER_M;
    this.gapAt = options.gapAt ?? GAP_AT;
    this.gapMs = options.gapMs ?? GAP_MS;
  }

  /**
   * Follow a different line from here on.
   *
   * Called after a reroute: the driver is now on the new route, and a
   * simulator that kept walking the old one would trigger an endless reroute
   * loop that looks exactly like a bug in the reroute logic.
   *
   * @param {Array<{lat: number, lon: number}>} shape
   * @param {number} [elapsedMs] where the drive is now, so distance restarts
   */
  setShape(shape, elapsedMs = 0) {
    this.shape = Array.isArray(shape) ? shape : [];
    this.cumulative = this.shape.length ? cumulativeDistances(this.shape) : [0];
    this.totalM = this.cumulative[this.cumulative.length - 1] || 0;
    // Distance already covered before this shape took over.
    this.originMs = elapsedMs;
    // A reroute means the driver is back on the line; one detour per run.
    this.detourDone = this.detourDone || false;
  }

  /** Metres travelled along the current shape at `elapsedMs`. */
  distanceAt(elapsedMs) {
    const seconds = Math.max(0, elapsedMs - this.originMs) / 1000;
    return Math.min(this.totalM, seconds * this.speedMps);
  }

  /** True once the drive has reached the end of the line. */
  finishedAt(elapsedMs) {
    return this.totalM > 0 && this.distanceAt(elapsedMs) >= this.totalM;
  }

  /**
   * The fix a receiver would report at `elapsedMs`.
   *
   * @param {number} elapsedMs
   * @returns {{coords: {latitude: number, longitude: number, accuracy: number,
   *            speed: number, heading: number | null, altitude: null,
   *            altitudeAccuracy: null}, timestamp: number} | null}
   */
  at(elapsedMs) {
    if (!this.shape.length) return null;
    if (this.inGap(elapsedMs)) return null;
    const alongM = this.distanceAt(elapsedMs);
    const here = pointAtDistance(this.shape, alongM, this.cumulative);
    if (!here) return null;

    // Heading from the next few metres of road, not from the last two fixes:
    // at a standstill the latter is undefined and the arrow spins.
    const ahead = pointAtDistance(this.shape, Math.min(this.totalM, alongM + 10), this.cumulative);
    const heading = ahead && haversineM(here, ahead) > 0.5 ? bearingDeg(here, ahead) : null;

    let point = here;
    let speedMps = this.finishedAt(elapsedMs) ? 0 : this.speedMps;

    if (this.detour && !this.detourDone && this.totalM > 0) {
      const fraction = alongM / this.totalM;
      // A segment, not the rest of the drive: if the app fails to reroute, the
      // track rejoining makes that visible instead of hiding it behind a
      // permanently-off-route display.
      if (fraction >= this.detourAt && fraction <= this.detourAt + this.detourSpan) {
        // Sideways, not along: a point 80 m further down the road is still on
        // the road, and the whole purpose is to be off it.
        const away = ((heading ?? 0) + 90) % 360;
        // Ramp in so the fixes look like a driver taking a side street rather
        // than like a receiver glitch. isOffRoute() wants three consecutive
        // fixes past the threshold; a step function hands it those instantly
        // and tests nothing about how the detector behaves on the way out.
        const past = (fraction - this.detourAt) * this.totalM;
        const offset = Math.min(this.detourOffsetM, 20 + past);
        point = destination(here, away, offset);
      }
    }

    // Applied last, so it perturbs the detour too: a real receiver is noisy
    // wherever the vehicle happens to be.
    point = this.jitter(point, elapsedMs);

    return {
      coords: {
        latitude: point.lat,
        longitude: point.lon,
        // A phone on a dash mount under a Managua sky, not a survey receiver.
        accuracy: 8,
        speed: speedMps,
        heading,
        altitude: null,
        altitudeAccuracy: null,
      },
      timestamp: Date.now(),
    };
  }

  /**
   * Whether this instant falls inside the drive's one signal dropout.
   *
   * @param {number} elapsedMs
   */
  inGap(elapsedMs) {
    if (!(this.gapMs > 0) || !(this.totalM > 0)) return false;
    const startMs = this.originMs + ((this.gapAt * this.totalM) / this.speedMps) * 1000;
    return elapsedMs >= startMs && elapsedMs < startMs + this.gapMs;
  }

  /**
   * Scatter a point by up to `jitterM`, deterministically.
   *
   * Deterministic on purpose: `at(ms)` has to stay a pure function of elapsed
   * time or a test cannot assert anything about it, and a bug that only
   * reproduces on one random seed is a bug nobody fixes.
   *
   * @param {{lat: number, lon: number}} point
   * @param {number} elapsedMs
   */
  jitter(point, elapsedMs) {
    if (!(this.jitterM > 0)) return point;
    const tick = Math.floor(elapsedMs / 1000);
    // mulberry32, two draws: one bearing, one radius.
    const draw = (n) => {
      let x = (n + 0x6d2b79f5) | 0;
      x = Math.imul(x ^ (x >>> 15), x | 1);
      x ^= x + Math.imul(x ^ (x >>> 7), x | 61);
      return ((x ^ (x >>> 14)) >>> 0) / 4294967296;
    };
    // sqrt keeps the scatter uniform over the disc instead of clustering at
    // the centre, which is what a receiver's error actually looks like.
    return destination(point, draw(tick) * 360, Math.sqrt(draw(tick * 2 + 1)) * this.jitterM);
  }

  /** Tell the track a reroute happened, so it stops wandering. */
  rerouted() {
    this.detourDone = true;
  }
}

// --------------------------------------------------------------------------- //
// GPX
// --------------------------------------------------------------------------- //

/**
 * Points out of a GPX track.
 *
 * Written against the shape every logger emits — `<trkpt lat lon>` with an
 * optional `<time>` — rather than against the schema, because half the loggers
 * in circulation put the track points in `<rtept>` or omit `<trkseg>`. Both are
 * accepted. Elevation is ignored: nothing here uses it.
 *
 * @param {string} xml
 * @returns {Array<{lat: number, lon: number, timeMs: number | null}>}
 */
export function parseGpx(xml) {
  const points = [];
  // A regex, not DOMParser: this runs identically in the browser and under
  // `node --test`, and the input is one tag shape, not arbitrary XML.
  // Attribute values may be single- or double-quoted, and lon may come before
  // lat: both are legal XML and both appear in the wild.
  const tag = /<(?:trkpt|rtept|wpt)\b([^>]*?)(\/?)>/gi;
  const attr = (raw, name) => {
    const found = new RegExp(`\\b${name}\\s*=\\s*["']([-\\d.eE+]+)["']`).exec(raw);
    return found ? Number.parseFloat(found[1]) : NaN;
  };
  let match;
  while ((match = tag.exec(xml)) !== null) {
    const lat = attr(match[1], 'lat');
    const lon = attr(match[1], 'lon');
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;

    let timeMs = null;
    if (!match[2]) {
      // Look only as far as this point's own closing tag, or a <time> from the
      // file header would be attached to every fix.
      const rest = xml.slice(tag.lastIndex, tag.lastIndex + 400);
      const end = rest.search(/<\/(?:trkpt|rtept|wpt)>/i);
      const window = end === -1 ? rest : rest.slice(0, end);
      const time = /<time>([^<]+)<\/time>/i.exec(window);
      if (time) {
        const parsed = Date.parse(time[1].trim());
        if (Number.isFinite(parsed)) timeMs = parsed;
      }
    }
    points.push({ lat, lon, timeMs });
  }
  return points;
}

/**
 * A track that replays recorded points.
 *
 * Where the GPX carries timestamps they are honoured, so a recording that sat
 * at a red light for ninety seconds sits at that light in the simulator too —
 * which is exactly the case that breaks a naive off-route detector. Without
 * timestamps it falls back to constant speed.
 */
export class GpxTrack {
  /**
   * @param {Array<{lat: number, lon: number, timeMs: number | null}>} points
   * @param {{speedKmh?: number}} [options]
   */
  constructor(points, options = {}) {
    this.points = points.filter((p) => Number.isFinite(p.lat) && Number.isFinite(p.lon));
    const speedMps = ((options.speedKmh ?? DEFAULT_SPEED_KMH) * 1000) / 3600;

    const first = this.points.find((p) => p.timeMs !== null);
    const timed = first !== undefined && this.points.filter((p) => p.timeMs !== null).length > 1;

    let offsetMs = 0;
    this.offsets = this.points.map((point, index) => {
      if (index === 0) return 0;
      const previous = this.points[index - 1];
      if (timed && point.timeMs !== null && previous.timeMs !== null) {
        // A logger that went backwards in time (some do, across a DST-less
        // clock correction) must not rewind the replay.
        offsetMs += Math.max(0, point.timeMs - previous.timeMs);
      } else {
        offsetMs += (haversineM(previous, point) / speedMps) * 1000;
      }
      return offsetMs;
    });
    this.durationMs = offsetMs;
  }

  finishedAt(elapsedMs) {
    return this.points.length > 0 && elapsedMs >= this.durationMs;
  }

  /** @returns {number} index of the last point at or before `elapsedMs`. */
  indexAt(elapsedMs) {
    let low = 0;
    let high = this.offsets.length - 1;
    while (low < high) {
      const mid = Math.ceil((low + high) / 2);
      if (this.offsets[mid] <= elapsedMs) low = mid;
      else high = mid - 1;
    }
    return low;
  }

  at(elapsedMs) {
    if (!this.points.length) return null;
    const index = this.indexAt(Math.max(0, elapsedMs));
    const point = this.points[index];
    const next = this.points[index + 1];

    let speedMps = 0;
    let heading = null;
    if (next) {
      const gapMs = this.offsets[index + 1] - this.offsets[index];
      const metres = haversineM(point, next);
      if (gapMs > 0) speedMps = (metres / gapMs) * 1000;
      if (metres > 0.5) heading = bearingDeg(point, next);
    }

    return {
      coords: {
        latitude: point.lat,
        longitude: point.lon,
        accuracy: 8,
        speed: speedMps,
        heading,
        altitude: null,
        altitudeAccuracy: null,
      },
      timestamp: Date.now(),
    };
  }

  // Same surface as SimulatedTrack so the position source does not care which
  // it is holding.
  setShape() {}
  rerouted() {}
}

// --------------------------------------------------------------------------- //
// The position source
// --------------------------------------------------------------------------- //

/**
 * An object with the two Geolocation methods nav.js uses.
 *
 * Kept to exactly `watchPosition` and `clearWatch` so the real
 * `navigator.geolocation` is a drop-in alternative and nav.js never has to
 * know which one it holds.
 *
 * @param {SimulatedTrack | GpxTrack} track
 * @param {{intervalMs?: number, now?: () => number,
 *          setInterval?: typeof setInterval,
 *          clearInterval?: typeof clearInterval}} [options]
 */
export function trackSource(track, options = {}) {
  const intervalMs = options.intervalMs ?? FIX_INTERVAL_MS;
  const now = options.now ?? (() => Date.now());
  const start = options.setInterval ?? ((fn, ms) => setInterval(fn, ms));
  const stop = options.clearInterval ?? ((id) => clearInterval(id));

  const timers = new Map();
  let nextId = 1;
  /** When the first watch started; the origin for every elapsed-time query. */
  let firstStart = null;

  return {
    track,
    watchPosition(onFix, _onError, _opts) {
      const id = nextId++;
      if (firstStart === null) firstStart = now();
      const began = firstStart;
      // One fix immediately: a real receiver with a warm almanac does the
      // same, and waiting a second for the banner to appear reads as a hang.
      const emit = () => {
        const fix = track.at(now() - began);
        if (fix) onFix(fix);
      };
      emit();
      timers.set(id, start(emit, intervalMs));
      return id;
    },
    /**
     * Follow a new line after a reroute.
     *
     * The source owns the clock, so nav.js can hand over a shape without
     * having to know how long the drive has been running.
     *
     * @param {Array<{lat: number, lon: number}>} shape
     */
    adoptShape(shape) {
      if (!Array.isArray(shape) || shape.length < 2) return;
      track.rerouted();
      track.setShape(shape, now() - (firstStart ?? now()));
    },
    clearWatch(id) {
      const timer = timers.get(id);
      if (timer !== undefined) {
        stop(timer);
        timers.delete(id);
      }
    },
    getCurrentPosition(onFix) {
      const fix = track.at(0);
      if (fix) onFix(fix);
    },
  };
}

/**
 * Build the position source for this page load, or null for a real drive.
 *
 * @param {{shape: Array<{lat: number, lon: number}>, options?: ReturnType<typeof simulatorOptions>,
 *          fetch?: typeof fetch}} args
 * @returns {Promise<ReturnType<typeof trackSource> | null>}
 */
export async function createPositionSource(args) {
  const options = args.options ?? simulatorOptions();
  if (!options.enabled) return null;

  if (options.gpx) {
    const get = args.fetch ?? ((url) => fetch(url));
    // A failed GPX load falls back to the synthetic drive rather than to a
    // real receiver: the driver asked for a simulation, and silently handing
    // them live GPS is the surprising answer.
    try {
      const response = await get(options.gpx);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const points = parseGpx(await response.text());
      if (points.length >= 2) {
        return trackSource(new GpxTrack(points, { speedKmh: options.speedKmh }));
      }
      console.warn('[sim] GPX had no usable track points; falling back to the route');
    } catch (error) {
      console.warn('[sim] could not load GPX:', error);
    }
  }

  return trackSource(
    new SimulatedTrack(args.shape, { speedKmh: options.speedKmh, detour: options.detour }),
  );
}
