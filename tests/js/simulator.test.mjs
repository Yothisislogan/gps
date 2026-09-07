/**
 * Tests for the navigation simulator.
 *
 * Run with `node --test "tests/js/*.test.mjs"`.
 *
 * The simulator's whole value is that it feeds the real navigation loop, so
 * what is checked here is that its output is indistinguishable from a
 * receiver's: the right shape, the right cadence, plausible speeds and
 * headings, and — for the detour — an offset large enough that navmath's own
 * off-route detector actually fires. Asserting against `isOffRoute` rather
 * than against a hard-coded number is deliberate: if the threshold moves, this
 * test moves with it instead of quietly testing nothing.
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  OFF_ROUTE_FIXES,
  cumulativeDistances,
  haversineM,
  isOffRoute,
  snapToRoute,
} from '../../web/js/navmath.js';
import {
  DEFAULT_SPEED_KMH,
  GAP_MS,
  GpxTrack,
  JITTER_M,
  SimulatedTrack,
  createPositionSource,
  parseGpx,
  simulatorOptions,
  trackSource,
} from '../../web/js/simulator.js';

/** Jitter plus what snapping to a coarse polyline adds on a bend. */
const ON_LINE_M = JITTER_M + 2;

/** Carretera a Masaya out of Managua: MGA, Rotonda Centroamérica, Metrocentro. */
const ROUTE = [
  { lat: 12.1415, lon: -86.1682 },
  { lat: 12.1094, lon: -86.2545 },
  { lat: 12.1246, lon: -86.2686 },
];

describe('simulatorOptions', () => {
  it('is off unless asked for', () => {
    assert.equal(simulatorOptions('').enabled, false);
    assert.equal(simulatorOptions('?q=managua').enabled, false);
  });

  it('reads sim, speed and detour', () => {
    const options = simulatorOptions('?sim=1&speed=80&detour=1');
    assert.equal(options.enabled, true);
    assert.equal(options.speedKmh, 80);
    assert.equal(options.detour, true);
  });

  it('treats a bare ?sim as on and ?sim=0 as off', () => {
    assert.equal(simulatorOptions('?sim').enabled, true);
    assert.equal(simulatorOptions('?sim=0').enabled, false);
    assert.equal(simulatorOptions('?sim=false').enabled, false);
  });

  it('falls back to the default speed rather than NaN', () => {
    assert.equal(simulatorOptions('?sim=1&speed=rapido').speedKmh, DEFAULT_SPEED_KMH);
    assert.equal(simulatorOptions('?sim=1&speed=-5').speedKmh, DEFAULT_SPEED_KMH);
  });

  it('turns itself on for a gpx url', () => {
    const options = simulatorOptions('?gpx=/drives/masaya.gpx');
    assert.equal(options.enabled, true);
    assert.equal(options.gpx, '/drives/masaya.gpx');
  });

  it('accepts a whole url or a location-like object', () => {
    assert.equal(simulatorOptions('https://mapa.example.ni/?sim=1').enabled, true);
    assert.equal(simulatorOptions({ search: '?sim=1' }).enabled, true);
  });
});

describe('SimulatedTrack', () => {
  it('starts at the origin and ends at the destination', () => {
    const track = new SimulatedTrack(ROUTE, { speedKmh: 60 });
    const first = track.at(0);
    assert.ok(
      haversineM({ lat: first.coords.latitude, lon: first.coords.longitude }, ROUTE[0]) <
        ON_LINE_M,
    );

    // Long past the end: the track clamps rather than driving into the sea.
    const last = track.at((track.totalM / (60 / 3.6)) * 1000 + 60_000);
    const end = ROUTE[ROUTE.length - 1];
    // Well inside ARRIVAL_RADIUS_M, which is what actually has to hold.
    assert.ok(haversineM({ lat: last.coords.latitude, lon: last.coords.longitude }, end) < ON_LINE_M);
  });

  it('covers the requested speed, not some other one', () => {
    const track = new SimulatedTrack(ROUTE, { speedKmh: 36 }); // 10 m/s exactly
    assert.ok(Math.abs(track.distanceAt(60_000) - 600) < 1);
    assert.equal(Math.round(track.at(60_000).coords.speed * 100) / 100, 10);
  });

  it('stops moving at the destination so arrival can fire', () => {
    // isArrival() requires the vehicle to be slow as well as close; a track
    // that reports cruising speed at the destination never arrives.
    const track = new SimulatedTrack(ROUTE, { speedKmh: 60 });
    const forever = (track.totalM / 16.7) * 1000 + 600_000;
    assert.equal(track.at(forever).coords.speed, 0);
    assert.equal(track.finishedAt(forever), true);
  });

  it('stays within receiver noise of the line when no detour was asked for', () => {
    // The point of the bound: jitter must be small enough that a plain drive
    // never trips the 40 m off-route threshold, or every simulated run
    // reroutes and the simulator is useless for testing rerouting.
    const track = new SimulatedTrack(ROUTE, { speedKmh: 40 });
    const cumulative = cumulativeDistances(ROUTE);
    for (let ms = 0; ms < 900_000; ms += 1000) {
      const fix = track.at(ms);
      if (!fix) continue;
      const snapped = snapToRoute(
        { lat: fix.coords.latitude, lon: fix.coords.longitude },
        ROUTE,
        { cumulative },
      );
      assert.ok(
        snapped.distanceM < ON_LINE_M,
        `drifted ${snapped.distanceM.toFixed(1)} m at ${ms} ms`,
      );
    }
  });

  it('scatters the fixes rather than tracing the centreline exactly', () => {
    const track = new SimulatedTrack(ROUTE, { speedKmh: 40 });
    let scattered = 0;
    for (let ms = 0; ms < 120_000; ms += 1000) {
      const fix = track.at(ms);
      if (!fix) continue;
      const snapped = snapToRoute({ lat: fix.coords.latitude, lon: fix.coords.longitude }, ROUTE);
      if (snapped.distanceM > 0.5) scattered += 1;
    }
    assert.ok(scattered > 60, `only ${scattered} of ~120 fixes carried any noise`);
  });

  it('is deterministic, so a failure reproduces', () => {
    const a = new SimulatedTrack(ROUTE, { speedKmh: 40 });
    const b = new SimulatedTrack(ROUTE, { speedKmh: 40 });
    for (const ms of [0, 37_000, 500_000]) {
      assert.deepEqual(a.at(ms)?.coords.latitude, b.at(ms)?.coords.latitude);
      assert.deepEqual(a.at(ms)?.coords.longitude, b.at(ms)?.coords.longitude);
    }
  });

  it('drops one run of fixes, so a signal gap can be tested', () => {
    const track = new SimulatedTrack(ROUTE, { speedKmh: 40 });
    let dropped = 0;
    let runs = 0;
    let previousWasFix = true;
    for (let ms = 0; ms < 1_200_000; ms += 1000) {
      const fix = track.at(ms);
      if (!fix) {
        dropped += 1;
        if (previousWasFix) runs += 1;
        previousWasFix = false;
      } else {
        previousWasFix = true;
      }
    }
    assert.equal(runs, 1, 'expected exactly one dropout per drive');
    assert.equal(dropped, GAP_MS / 1000);
  });

  it('keeps moving through the gap, so the far side is a jump forward', () => {
    // This is the case that produces a false reroute: the vehicle is 165 m
    // further along at 40 km/h and a forward-only snapper can miss it.
    const track = new SimulatedTrack(ROUTE, { speedKmh: 40, jitterM: 0 });
    const before = track.at(0);
    let gapStart = null;
    for (let ms = 0; ms < 1_200_000; ms += 1000) {
      if (track.inGap(ms)) {
        gapStart = ms;
        break;
      }
    }
    assert.ok(gapStart !== null, 'no gap found');
    const across = track.distanceAt(gapStart + GAP_MS) - track.distanceAt(gapStart - 1000);
    assert.ok(across > 150, `only advanced ${across.toFixed(0)} m across the gap`);
    assert.ok(before);
  });

  it('can be built without noise or a gap, for a test that wants neither', () => {
    const track = new SimulatedTrack(ROUTE, { speedKmh: 40, jitterM: 0, gapMs: 0 });
    const cumulative = cumulativeDistances(ROUTE);
    for (let ms = 0; ms < 600_000; ms += 5000) {
      const fix = track.at(ms);
      assert.ok(fix, `no fix at ${ms} ms`);
      const snapped = snapToRoute(
        { lat: fix.coords.latitude, lon: fix.coords.longitude },
        ROUTE,
        { cumulative },
      );
      assert.ok(snapped.distanceM < 1);
    }
  });

  it('reports a heading along the road rather than nothing', () => {
    const track = new SimulatedTrack(ROUTE, { speedKmh: 40, jitterM: 0 });
    const heading = track.at(60_000).coords.heading;
    assert.ok(Number.isFinite(heading));
    assert.ok(heading >= 0 && heading < 360);
  });

  it('leaves the route far enough for the off-route detector to fire', () => {
    const track = new SimulatedTrack(ROUTE, { speedKmh: 40, detour: true });
    // Asserted against navmath's own detector, not a hard-coded distance, so
    // this follows the threshold instead of quietly testing nothing if it moves.
    const history = [];
    let tripped = false;
    for (let ms = 0; ms < 1_800_000; ms += 1000) {
      const fix = track.at(ms);
      if (!fix) continue;
      const snapped = snapToRoute({ lat: fix.coords.latitude, lon: fix.coords.longitude }, ROUTE);
      history.push({ distanceM: snapped.distanceM, speedMps: fix.coords.speed });
      if (history.length > OFF_ROUTE_FIXES + 3) history.shift();
      if (isOffRoute(history)) {
        tripped = true;
        break;
      }
    }
    assert.ok(tripped, 'detour=1 never triggered isOffRoute');
  });

  it('follows the new line after a reroute instead of looping', () => {
    const track = new SimulatedTrack(ROUTE, { speedKmh: 40, detour: true, jitterM: 0, gapMs: 0 });
    track.at(600_000);
    const rerouted = [
      { lat: 12.1094, lon: -86.2545 },
      { lat: 12.1246, lon: -86.2686 },
    ];
    track.rerouted();
    track.setShape(rerouted, 600_000);

    const fix = track.at(630_000);
    const snapped = snapToRoute({ lat: fix.coords.latitude, lon: fix.coords.longitude }, rerouted);
    assert.ok(snapped.distanceM < 2, `still ${snapped.distanceM.toFixed(1)} m off the new route`);
  });

  it('survives an empty shape rather than throwing into the nav loop', () => {
    const track = new SimulatedTrack([], { speedKmh: 40 });
    assert.equal(track.at(0), null);
    assert.equal(track.finishedAt(0), false);
  });
});

describe('parseGpx', () => {
  const GPX = `<?xml version="1.0"?>
    <gpx version="1.1"><metadata><time>2020-01-01T00:00:00Z</time></metadata>
    <trk><name>Masaya</name><trkseg>
      <trkpt lat="12.1415" lon="-86.1682"><ele>82</ele><time>2026-09-01T16:00:00Z</time></trkpt>
      <trkpt lon='-86.1700' lat='12.1400'><time>2026-09-01T16:00:10Z</time></trkpt>
      <trkpt lat="12.1380" lon="-86.1750"/>
    </trkseg></trk></gpx>`;

  it('reads points regardless of quoting or attribute order', () => {
    const points = parseGpx(GPX);
    assert.equal(points.length, 3);
    assert.deepEqual(
      points.map((p) => [p.lat, p.lon]),
      [
        [12.1415, -86.1682],
        [12.14, -86.17],
        [12.138, -86.175],
      ],
    );
  });

  it('does not attach the file header time to the first point', () => {
    const [first] = parseGpx(GPX);
    assert.equal(new Date(first.timeMs).toISOString(), '2026-09-01T16:00:00.000Z');
  });

  it('leaves a point with no time of its own untimed', () => {
    assert.equal(parseGpx(GPX)[2].timeMs, null);
  });

  it('returns nothing for something that is not a gpx', () => {
    assert.deepEqual(parseGpx('<html><body>404</body></html>'), []);
  });
});

describe('GpxTrack', () => {
  const points = [
    { lat: 12.1415, lon: -86.1682, timeMs: 1_000_000 },
    { lat: 12.1405, lon: -86.1700, timeMs: 1_010_000 },
    { lat: 12.1395, lon: -86.1720, timeMs: 1_100_000 },
  ];

  it('replays at the recorded times, including a ninety-second stop', () => {
    const track = new GpxTrack(points);
    assert.equal(track.durationMs, 100_000);
    assert.equal(track.at(0).coords.latitude, 12.1415);
    assert.equal(track.at(9_999).coords.latitude, 12.1415);
    assert.equal(track.at(10_000).coords.latitude, 12.1405);
    assert.equal(track.at(99_999).coords.latitude, 12.1405);
  });

  it('derives speed from the recording rather than inventing one', () => {
    const track = new GpxTrack(points);
    const metres = haversineM(points[0], points[1]);
    assert.ok(Math.abs(track.at(0).coords.speed - metres / 10) < 0.01);
  });

  it('falls back to constant speed when the logger wrote no times', () => {
    const untimed = points.map((p) => ({ ...p, timeMs: null }));
    const track = new GpxTrack(untimed, { speedKmh: 36 });
    const metres = haversineM(untimed[0], untimed[1]);
    assert.ok(Math.abs(track.offsets[1] - (metres / 10) * 1000) < 1);
  });

  it('does not rewind when a logger writes a backwards timestamp', () => {
    const broken = [
      { lat: 12.14, lon: -86.16, timeMs: 2_000 },
      { lat: 12.141, lon: -86.161, timeMs: 1_000 },
      { lat: 12.142, lon: -86.162, timeMs: 3_000 },
    ];
    const track = new GpxTrack(broken);
    for (let i = 1; i < track.offsets.length; i += 1) {
      assert.ok(track.offsets[i] >= track.offsets[i - 1], 'offsets went backwards');
    }
  });
});

describe('trackSource', () => {
  /** A fake clock, so a test can drive an hour in a millisecond. */
  function fakeTimers() {
    let now = 0;
    const scheduled = [];
    return {
      now: () => now,
      setInterval: (fn, ms) => {
        scheduled.push({ fn, ms, next: ms });
        return scheduled.length - 1;
      },
      clearInterval: (id) => {
        scheduled[id] = null;
      },
      advance(ms) {
        const target = now + ms;
        for (;;) {
          const due = scheduled
            .map((entry, id) => ({ entry, id }))
            .filter(({ entry }) => entry && entry.next <= target)
            .sort((a, b) => a.entry.next - b.entry.next)[0];
          if (!due) break;
          now = due.entry.next;
          due.entry.next += due.entry.ms;
          due.entry.fn();
        }
        now = target;
      },
    };
  }

  it('exposes the Geolocation methods nav.js calls, and no others it needs', () => {
    const source = trackSource(new SimulatedTrack(ROUTE));
    assert.equal(typeof source.watchPosition, 'function');
    assert.equal(typeof source.clearWatch, 'function');
  });

  it('delivers a first fix immediately, then one per interval', () => {
    const timers = fakeTimers();
    const source = trackSource(new SimulatedTrack(ROUTE, { speedKmh: 40, gapMs: 0 }), {
      intervalMs: 1000,
      now: timers.now,
      setInterval: timers.setInterval,
      clearInterval: timers.clearInterval,
    });

    const fixes = [];
    const id = source.watchPosition((fix) => fixes.push(fix));
    assert.equal(fixes.length, 1, 'no fix until the first tick');

    timers.advance(5000);
    assert.equal(fixes.length, 6);

    source.clearWatch(id);
    timers.advance(5000);
    assert.equal(fixes.length, 6, 'kept emitting after clearWatch');
  });

  it('hands out fixes shaped like GeolocationPosition', () => {
    const source = trackSource(new SimulatedTrack(ROUTE, { speedKmh: 40, gapMs: 0 }));
    let fix = null;
    const id = source.watchPosition((f) => {
      fix = f;
    });
    source.clearWatch(id);

    assert.ok(Number.isFinite(fix.coords.latitude));
    assert.ok(Number.isFinite(fix.coords.longitude));
    assert.ok(Number.isFinite(fix.coords.accuracy));
    assert.ok(Number.isFinite(fix.coords.speed));
    assert.ok(Number.isFinite(fix.timestamp));
    assert.equal(fix.coords.altitude, null);
  });
});

describe('createPositionSource', () => {
  it('returns nothing at all when the simulator was not asked for', async () => {
    const source = await createPositionSource({
      shape: ROUTE,
      options: simulatorOptions(''),
    });
    assert.equal(source, null);
  });

  it('builds a synthetic drive for ?sim=1', async () => {
    const source = await createPositionSource({
      shape: ROUTE,
      options: simulatorOptions('?sim=1&speed=50'),
    });
    assert.ok(source.track instanceof SimulatedTrack);
    assert.ok(Math.abs(source.track.speedMps - 50 / 3.6) < 1e-9);
  });

  it('replays a gpx when one is given', async () => {
    const xml = `<gpx><trkseg>
      <trkpt lat="12.1415" lon="-86.1682"/><trkpt lat="12.1400" lon="-86.1700"/>
    </trkseg></gpx>`;
    const source = await createPositionSource({
      shape: ROUTE,
      options: simulatorOptions('?gpx=/x.gpx'),
      fetch: async () => ({ ok: true, text: async () => xml }),
    });
    assert.ok(source.track instanceof GpxTrack);
    assert.equal(source.track.points.length, 2);
  });

  it('falls back to the synthetic drive when the gpx cannot be fetched', async () => {
    // Not to the real receiver: the driver asked for a simulation, and handing
    // them live GPS instead is the surprising answer.
    const source = await createPositionSource({
      shape: ROUTE,
      options: simulatorOptions('?gpx=/missing.gpx'),
      fetch: async () => ({ ok: false, status: 404, text: async () => '' }),
    });
    assert.ok(source.track instanceof SimulatedTrack);
  });

  it('falls back when the gpx parses to nothing usable', async () => {
    const source = await createPositionSource({
      shape: ROUTE,
      options: simulatorOptions('?gpx=/empty.gpx'),
      fetch: async () => ({ ok: true, text: async () => '<gpx></gpx>' }),
    });
    assert.ok(source.track instanceof SimulatedTrack);
  });
});
