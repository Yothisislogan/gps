/**
 * Tests for the navigation geometry.
 *
 * Run with `node --test tests/js/`. This is a dev-only check — the app itself
 * still has no build step and no Node dependency at runtime.
 *
 * The coordinates below trace real ground: the Carretera a Masaya corridor out
 * of Managua, and the Rotonda El Güegüense. Using real geometry keeps the
 * tolerances honest, because a route through Managua is not a straight line on
 * a flat plane.
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  ARRIVAL_RADIUS_M,
  OFF_ROUTE_FIXES,
  OFF_ROUTE_THRESHOLD_M,
  VALHALLA_PRECISION,
  bearingDeg,
  cumulativeDistances,
  decodePolyline,
  destination,
  encodePolyline,
  haversineM,
  isArrival,
  isOffRoute,
  maneuverProgress,
  normalizeRoute,
  pointAtDistance,
  remainingDistanceM,
  snapToRoute,
  spokenDistance,
} from '../../web/js/navmath.js';

/** Metrocentro -> Rotonda Jean Paul Genie, roughly along Carretera a Masaya. */
const CARRETERA = [
  { lat: 12.1246, lon: -86.2686 },
  { lat: 12.1195, lon: -86.2648 },
  { lat: 12.1128, lon: -86.2601 },
  { lat: 12.1064, lon: -86.2557 },
  { lat: 12.0998, lon: -86.2512 },
];

const GUEGUENSE = { lat: 12.1352, lon: -86.2807 };

describe('polyline', () => {
  it('defaults to precision 6, which is what Valhalla sends', () => {
    assert.equal(VALHALLA_PRECISION, 6);
  });

  it('round-trips a Managua route', () => {
    const decoded = decodePolyline(encodePolyline(CARRETERA));
    assert.equal(decoded.length, CARRETERA.length);
    for (let i = 0; i < CARRETERA.length; i += 1) {
      assert.ok(Math.abs(decoded[i].lat - CARRETERA[i].lat) < 1e-6);
      assert.ok(Math.abs(decoded[i].lon - CARRETERA[i].lon) < 1e-6);
    }
  });

  it('decoding at precision 5 lands in the Pacific', () => {
    // The single most common Valhalla integration bug. Asserted so nobody
    // "fixes" the default back to 5.
    const wrong = decodePolyline(encodePolyline(CARRETERA), 5);
    assert.ok(Math.abs(wrong[0].lat - CARRETERA[0].lat * 10) < 1e-4);
  });

  it('survives an empty or truncated payload', () => {
    assert.deepEqual(decodePolyline(''), []);
    const encoded = encodePolyline(CARRETERA);
    assert.ok(decodePolyline(encoded.slice(0, 6)).length < CARRETERA.length);
  });
});

describe('geodesy', () => {
  it('measures a known Managua distance', () => {
    // Metrocentro to the Rotonda El Güegüense is about 1.8 km.
    const metres = haversineM(CARRETERA[0], GUEGUENSE);
    assert.ok(metres > 1500 && metres < 2300, `got ${metres}`);
  });

  it('bearing and destination round-trip', () => {
    for (const bearing of [0, 45, 90, 180, 270, 359]) {
      const target = destination(GUEGUENSE, bearing, 500);
      assert.ok(Math.abs(haversineM(GUEGUENSE, target) - 500) < 1);
      const back = bearingDeg(GUEGUENSE, target);
      const delta = Math.abs(back - bearing) % 360;
      assert.ok(Math.min(delta, 360 - delta) < 0.5, `${bearing} -> ${back}`);
    }
  });

  it('going south decreases latitude and west decreases longitude', () => {
    // Nicaragua is all positive latitude and negative longitude; a flipped sign
    // here is the classic bug and it looks plausible on screen.
    assert.ok(destination(GUEGUENSE, 180, 200).lat < GUEGUENSE.lat);
    assert.ok(destination(GUEGUENSE, 270, 200).lon < GUEGUENSE.lon);
    assert.ok(destination(GUEGUENSE, 0, 200).lat > GUEGUENSE.lat);
    assert.ok(destination(GUEGUENSE, 90, 200).lon > GUEGUENSE.lon);
  });
});

describe('snapToRoute', () => {
  it('snaps a point beside the road onto it', () => {
    const beside = destination(CARRETERA[1], 90, 25);
    const snapped = snapToRoute(beside, CARRETERA);
    assert.ok(snapped.distanceM < 30, `distance ${snapped.distanceM}`);
    assert.ok(snapped.alongM > 0);
  });

  it('clamps before the start and past the end', () => {
    const cumulative = cumulativeDistances(CARRETERA);
    const total = cumulative[cumulative.length - 1];

    const before = snapToRoute(destination(CARRETERA[0], 315, 400), CARRETERA);
    assert.ok(before.alongM >= 0 && before.alongM < total * 0.2);

    const after = snapToRoute(destination(CARRETERA[CARRETERA.length - 1], 135, 400), CARRETERA);
    assert.ok(after.alongM > total * 0.8);
    assert.ok(after.alongM <= total + 1);
  });

  it('along-distance grows monotonically down the route', () => {
    let previous = -1;
    for (const vertex of CARRETERA) {
      const snapped = snapToRoute(vertex, CARRETERA);
      assert.ok(snapped.alongM >= previous, 'along must not go backwards');
      previous = snapped.alongM;
    }
  });

  it('refuses an empty shape rather than inventing a position', () => {
    // Silently returning "you are at the start" would put the driver on a route
    // that does not exist.
    assert.throws(() => snapToRoute(GUEGUENSE, []));
    assert.equal(snapToRoute(GUEGUENSE, [GUEGUENSE]).distanceM, 0);
  });
});

describe('remaining distance', () => {
  it('counts down to zero', () => {
    const cumulative = cumulativeDistances(CARRETERA);
    const total = cumulative[cumulative.length - 1];
    assert.ok(Math.abs(remainingDistanceM(CARRETERA, 0, cumulative) - total) < 1);
    assert.ok(remainingDistanceM(CARRETERA, total, cumulative) < 1);
    assert.ok(remainingDistanceM(CARRETERA, total + 5000, cumulative) >= 0);
  });

  it('point-at-distance walks the line', () => {
    const cumulative = cumulativeDistances(CARRETERA);
    const total = cumulative[cumulative.length - 1];
    const middle = pointAtDistance(CARRETERA, total / 2, cumulative);
    assert.ok(Math.abs(haversineM(CARRETERA[0], middle) - total / 2) < total * 0.15);
  });
});

describe('off-route detection', () => {
  const far = { distanceM: OFF_ROUTE_THRESHOLD_M + 20, speedMps: 12 };
  const near = { distanceM: 5, speedMps: 12 };

  it('needs several consecutive fixes, not one', () => {
    assert.equal(isOffRoute([far]), false);
    assert.equal(isOffRoute(Array(OFF_ROUTE_FIXES).fill(far)), true);
  });

  it('a single good fix resets it', () => {
    const history = [...Array(OFF_ROUTE_FIXES).fill(far)];
    history[history.length - 1] = near;
    assert.equal(isOffRoute(history), false);
  });

  it('a parked car is never off route', () => {
    // A stationary GPS drifts tens of metres. Rerouting somebody sitting at a
    // semáforo is worse than useless.
    const parked = { distanceM: OFF_ROUTE_THRESHOLD_M + 50, speedMps: 0.2 };
    assert.equal(isOffRoute(Array(OFF_ROUTE_FIXES + 2).fill(parked)), false);
  });

  it('an unknown speed does not trigger a reroute', () => {
    const noSpeed = { distanceM: OFF_ROUTE_THRESHOLD_M + 50, speedMps: NaN };
    assert.equal(isOffRoute(Array(OFF_ROUTE_FIXES + 2).fill(noSpeed)), false);
  });
});

describe('arrival', () => {
  it('needs proximity and a slow speed', () => {
    const near = destination(GUEGUENSE, 45, ARRIVAL_RADIUS_M - 10);
    assert.equal(isArrival(near, GUEGUENSE, 1), true);
    assert.equal(isArrival(near, GUEGUENSE, 20), false, 'driving past is not arriving');
  });

  it('is false far away however slowly you are moving', () => {
    const far = destination(GUEGUENSE, 45, 400);
    assert.equal(isArrival(far, GUEGUENSE, 0), false);
  });

  it('treats an unknown speed near the destination as stopped', () => {
    const near = destination(GUEGUENSE, 45, 10);
    assert.equal(isArrival(near, GUEGUENSE, NaN), true);
  });
});

describe('normalizeRoute', () => {
  const valhalla = {
    trip: {
      units: 'kilometers',
      language: 'es-ES',
      summary: { length: 2.4, time: 300 },
      legs: [
        {
          shape: encodePolyline(CARRETERA),
          maneuvers: [
            {
              type: 1,
              instruction: 'Conduzca al sur por Carretera a Masaya.',
              verbal_pre_transition_instruction: 'Conduzca al sur por Carretera a Masaya.',
              street_names: ['Carretera a Masaya'],
              time: 240,
              length: 2.0,
              begin_shape_index: 0,
              end_shape_index: 3,
            },
            {
              type: 27,
              instruction: 'Tome la segunda salida de la rotonda.',
              roundabout_exit_count: 2,
              time: 60,
              length: 0.4,
              begin_shape_index: 3,
              end_shape_index: 4,
            },
            {
              type: 4,
              instruction: 'Llegaste a tu destino.',
              time: 0,
              length: 0,
              begin_shape_index: 4,
              end_shape_index: 4,
            },
          ],
        },
      ],
    },
  };

  it('produces a shape, steps and totals', () => {
    const plan = normalizeRoute(valhalla, { lang: 'es' });
    assert.equal(plan.format, 'valhalla');
    assert.equal(plan.shape.length, CARRETERA.length);
    assert.ok(plan.steps.length >= 2);
    assert.ok(plan.totalM > 0);
    assert.equal(plan.totalDurationS, 300);
  });

  it('attaches each step the instruction for the maneuver that ENDS it', () => {
    // The Mapbox convention Valhalla follows, and the thing a nav UI gets wrong
    // most often: while driving step 0 the banner must already read the turn at
    // its end, not the turn that began it.
    const plan = normalizeRoute(valhalla, { lang: 'es' });
    assert.match(plan.steps[0].maneuver.instruction, /rotonda/i);
    assert.equal(plan.steps[0].roadName, 'Carretera a Masaya');
  });

  it('keeps the narration in Spanish', () => {
    const plan = normalizeRoute(valhalla, { lang: 'es' });
    const text = plan.steps.map((step) => step.maneuver.instruction).join(' ');
    assert.match(text, /rotonda|destino|Conduzca/i);
    assert.doesNotMatch(text, /\b(Turn|Drive|Continue)\b/);
  });

  it('steps cover the route without gaps', () => {
    const plan = normalizeRoute(valhalla, { lang: 'es' });
    let previousEnd = 0;
    for (const step of plan.steps) {
      assert.ok(step.beginAlongM >= previousEnd - 1, 'steps must not overlap backwards');
      assert.ok(step.endAlongM >= step.beginAlongM);
      previousEnd = step.endAlongM;
    }
  });

  it('rejects a payload it does not recognise', () => {
    assert.throws(() => normalizeRoute({ nonsense: true }));
  });
});

describe('maneuverProgress', () => {
  const plan = normalizeRoute(
    {
      trip: {
        units: 'kilometers',
        summary: { length: 2.4, time: 300 },
        legs: [
          {
            shape: encodePolyline(CARRETERA),
            maneuvers: [
              { type: 1, instruction: 'Siga.', time: 240, length: 2.0, begin_shape_index: 0, end_shape_index: 3 },
              { type: 27, instruction: 'Rotonda.', time: 60, length: 0.4, begin_shape_index: 3, end_shape_index: 4 },
              { type: 4, instruction: 'Llegaste.', time: 0, length: 0, begin_shape_index: 4, end_shape_index: 4 },
            ],
          },
        ],
      },
    },
    { lang: 'es' },
  );

  it('reports the current step and the distance to its maneuver', () => {
    const progress = maneuverProgress(0, plan.steps);
    assert.equal(progress.index, 0);
    assert.ok(progress.distanceToManeuverM > 0);
    assert.ok(progress.next);
  });

  it('advances as the driver moves', () => {
    const first = maneuverProgress(0, plan.steps);
    const later = maneuverProgress(plan.totalM * 0.9, plan.steps);
    assert.ok(later.index >= first.index);
    assert.ok(later.distanceToManeuverM <= first.distanceToManeuverM);
  });

  it('marks the final step', () => {
    assert.equal(maneuverProgress(plan.totalM, plan.steps).isFinalStep, true);
  });

  it('returns null with no steps', () => {
    assert.equal(maneuverProgress(0, []), null);
  });
});

describe('spoken distance', () => {
  it('speaks Spanish by default', () => {
    assert.match(spokenDistance(1000), /kil[oó]metro/i);
    assert.match(spokenDistance(300), /metros/i);
  });

  it('says "ahora" when the turn is immediate', () => {
    // "En 5 metros gire a la derecha" is useless; by the time it is said the
    // turn is behind you.
    assert.match(spokenDistance(5), /ahora/i);
  });

  it('uses a comma decimal in Spanish', () => {
    assert.match(spokenDistance(1500), /1,5/);
  });

  it('switches to English on request', () => {
    assert.match(spokenDistance(1000, 'en'), /kilometer/i);
  });
});
