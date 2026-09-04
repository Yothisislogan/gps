/**
 * Turn-by-turn navigation.
 *
 * Ferrostar's web components cannot be loaded without a bundler — its dist
 * externalises four bare specifiers and its Rust core is built for a bundler
 * target — so this is the custom loop docs/PLAN.md section 6.1 names as the
 * alternative. All the geometry lives in navmath.js, which is unit-tested under
 * `node --test`; this file is the browser half: sensors, screen, voice, and the
 * decisions about when to speak and when to reroute.
 *
 * Three things here exist because of how phones behave on a dash mount in
 * Nicaragua, not because of how navigation works in theory:
 *
 *  - the wake lock is re-acquired on every visibility change, because Android
 *    silently drops it whenever the tab hides, and a screen that sleeps in
 *    traffic is a missed turn;
 *  - the route survives a reload, because a browser under memory pressure will
 *    reload the page while the driver is looking at the road;
 *  - a reroute never fires more than one request at a time, and backs off when
 *    the network is failing, because signal drops on the Carretera Sur and a
 *    reroute storm turns a bad connection into no connection.
 */

import * as api from './api.js';
import {
  formatDistance,
  isArrival,
  isOffRoute,
  maneuverProgress,
  normalizeRoute,
  remainingDistanceM,
  remainingDurationS,
  snapToRoute,
  spokenDistance,
} from './navmath.js';
import { el, formatDuration, t, toast } from './ui.js';
import { isVoiceEnabled, primeVoices, setVoiceEnabled, speak } from './voice.js';

const SESSION_KEY = 'nicanav.nav';
/** Fixes kept for the off-route detector — three consecutive, plus slack. */
const HISTORY = 6;
/** A reroute request in flight blocks the next one; this is the floor between them. */
const REROUTE_COOLDOWN_MS = 8000;

/** @type {any} */
let session = null;

/**
 * @typedef {object} NavSession
 * @property {any} map the MapController from map.js
 * @property {any} plan normalizeRoute() output
 * @property {{lat: number, lon: number}} destination
 */

// --------------------------------------------------------------------------- //
// Chrome
// --------------------------------------------------------------------------- //

function buildChrome(root) {
  const arrow = el('div', { class: 'nav-banner__arrow', 'aria-hidden': 'true', text: '↑' });
  const distance = el('div', { class: 'nav-banner__distance', text: '' });
  const instruction = el('div', { class: 'nav-banner__instruction', text: '' });
  const then = el('div', { class: 'nav-banner__then', text: '' });

  const eta = el('div', { class: 'nav-bar__value', text: '—' });
  const remaining = el('div', { class: 'nav-bar__value', text: '—' });
  const speed = el('div', { class: 'nav-bar__value', text: '—' });
  const speedBox = el('div', { class: 'nav-speed' }, [
    el('div', {}, [speed, el('div', { class: 'nav-bar__label', text: 'km/h' })]),
  ]);

  const voiceButton = el('button', {
    class: 'fab fab--small',
    type: 'button',
    'aria-label': 'Voz',
    'aria-pressed': String(isVoiceEnabled()),
    text: '🔊',
    onClick: () => {
      const next = !isVoiceEnabled();
      setVoiceEnabled(next);
      voiceButton.setAttribute('aria-pressed', String(next));
    },
  });

  const stop = el('button', {
    class: 'nav-bar__stop',
    type: 'button',
    text: 'Salir',
    onClick: () => stopNavigation(),
  });

  root.replaceChildren(
    el('div', { class: 'nav-banner', role: 'status', 'aria-live': 'assertive' }, [
      arrow,
      el('div', { class: 'nav-banner__body' }, [distance, instruction, then]),
    ]),
    el('div', { class: 'nav-bar' }, [
      el('div', { class: 'nav-bar__stat' }, [eta, el('div', { class: 'nav-bar__label', text: 'llegada' })]),
      el('div', { class: 'nav-bar__stat' }, [remaining, el('div', { class: 'nav-bar__label', text: 'faltan' })]),
      speedBox,
      el('div', { class: 'nav-bar__spacer' }),
      voiceButton,
      stop,
    ]),
  );
  root.hidden = false;
  return { arrow, distance, instruction, then, eta, remaining, speed, speedBox };
}

const ARROWS = {
  turn_left: '↰', turn_right: '↱', slight_left: '↖', slight_right: '↗',
  sharp_left: '↰', sharp_right: '↱', uturn: '⤾', straight: '↑', continue: '↑',
  roundabout: '↻', merge: '⤳', fork: '⑂', depart: '↑', arrive: '⚑', ramp: '↗',
};

function arrowFor(maneuver) {
  if (!maneuver) return '↑';
  return ARROWS[maneuver.kind] || ARROWS[maneuver.modifier] || '↑';
}

// --------------------------------------------------------------------------- //
// The loop
// --------------------------------------------------------------------------- //

function render(state) {
  const { chrome, plan, progress, snapped, speedMps } = state;
  if (!progress) return;

  chrome.arrow.textContent = arrowFor(progress.current.maneuver);
  chrome.distance.textContent = formatDistance(progress.distanceToManeuverM);
  chrome.instruction.textContent = progress.current.maneuver.instruction || '';
  chrome.then.textContent = progress.next
    ? `luego ${progress.next.maneuver.instruction || ''}`
    : '';

  const remainingM = remainingDistanceM(plan.shape, snapped.alongM, plan.cumulative);
  const remainingS = remainingDurationS(plan.steps, snapped.alongM);
  chrome.remaining.textContent = formatDistance(remainingM);

  const arrivalAt = new Date(Date.now() + remainingS * 1000);
  chrome.eta.textContent = arrivalAt.toLocaleTimeString('es-NI', {
    hour: '2-digit',
    minute: '2-digit',
  });

  chrome.speed.textContent =
    Number.isFinite(speedMps) && speedMps >= 0 ? String(Math.round(speedMps * 3.6)) : '—';
}

/**
 * Speak the prompts a step has earned.
 *
 * Each prompt fires once. Valhalla's own prompts carry the distance at which
 * they should be said (measured back from the end of the step), so timing is
 * the router's decision and not a guess made here.
 */
function announce(state, progress) {
  const step = progress.current;
  const remaining = progress.distanceToManeuverM;
  const spoken = state.spoken;

  for (const prompt of step.voice || []) {
    const key = `${step.index}:${prompt.remainingM}`;
    if (spoken.has(key)) continue;
    if (remaining <= prompt.remainingM) {
      spoken.add(key);
      speak(prompt.text, { priority: prompt.priority });
    }
  }
}

function onFix(fix) {
  if (!session) return;
  const point = { lat: fix.coords.latitude, lon: fix.coords.longitude };
  const speedMps = Number.isFinite(fix.coords.speed) ? Math.max(0, fix.coords.speed) : NaN;

  const snapped = snapToRoute(point, session.plan.shape, {
    startIndex: Math.max(0, session.lastIndex - 2),
  });
  session.lastIndex = snapped.index;
  session.history.push({ distanceM: snapped.distanceM, speedMps });
  if (session.history.length > HISTORY) session.history.shift();

  session.map?.setMarker('me', point.lat, point.lon, { kind: 'me', heading: fix.coords.heading });
  // Keep the driver's own position in the lower third: what matters is the road
  // ahead, not the road already driven.
  session.map?.flyTo(point.lat, point.lon, { zoom: 17, keepBearing: true, navMode: true });

  const progress = maneuverProgress(snapped.alongM, session.plan.steps);
  if (progress) {
    render({ chrome: session.chrome, plan: session.plan, progress, snapped, speedMps });
    announce(session, progress);
  }

  if (isArrival(point, session.destination, speedMps)) {
    speak('Llegaste a tu destino', { priority: 'high' });
    toast('Llegaste');
    stopNavigation();
    return;
  }

  if (isOffRoute(session.history)) reroute(point, fix.coords.heading);
  persist();
}

/**
 * Request a new route from where the driver actually is.
 *
 * `heading` is what stops the router turning them around: without it a reroute
 * on a dual carriageway happily proposes a U-turn across the median.
 */
async function reroute(point, heading) {
  if (!session || session.rerouting) return;
  const now = Date.now();
  if (now - session.lastRerouteAt < REROUTE_COOLDOWN_MS) return;

  session.rerouting = true;
  session.lastRerouteAt = now;
  speak('Recalculando', { priority: 'high' });

  try {
    const response = await api.route({
      locations: [
        { lat: point.lat, lon: point.lon },
        { lat: session.destination.lat, lon: session.destination.lon },
      ],
      costing: 'auto',
      alternates: 0,
      heading: Number.isFinite(heading) ? heading : undefined,
      language: 'es-ES',
    });
    adoptRoute(response);
  } catch {
    // Signal drops on the Carretera Sur. Keep guiding on the old line rather
    // than blanking the banner; the next fix will try again after the cooldown.
    toast(t('dir.failed'), { kind: 'error' });
  } finally {
    session.rerouting = false;
  }
}

function adoptRoute(response) {
  if (!session) return;
  session.plan = normalizeRoute(response, { lang: 'es' });
  session.raw = response;
  session.history = [];
  session.lastIndex = 0;
  session.spoken = new Set();
  const shape = (response.trip?.legs || []).map((leg) => leg.shape).filter(Boolean)[0];
  if (shape) session.map?.showRoute(shape, { fit: false });
  persist();
}

/** Persist enough to resume after a reload — phones under memory pressure do. */
function persist() {
  if (!session) return;
  try {
    sessionStorage.setItem(
      SESSION_KEY,
      JSON.stringify({ route: session.raw, destination: session.destination }),
    );
  } catch {
    /* storage is a convenience here */
  }
}

/** The route a reload interrupted, if any. */
export function pendingSession() {
  try {
    const raw = sessionStorage.getItem(SESSION_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

// --------------------------------------------------------------------------- //
// Wake lock
// --------------------------------------------------------------------------- //

async function acquireWakeLock() {
  if (!('wakeLock' in navigator)) return null;
  try {
    return await navigator.wakeLock.request('screen');
  } catch {
    // Denied on low battery, among other reasons. Navigation still works; the
    // screen just sleeps, which is why the driver is told.
    return null;
  }
}

function watchVisibility() {
  const handler = async () => {
    if (!session) return;
    if (document.visibilityState === 'visible' && !session.wakeLock) {
      session.wakeLock = await acquireWakeLock();
    }
  };
  document.addEventListener('visibilitychange', handler);
  return () => document.removeEventListener('visibilitychange', handler);
}

// --------------------------------------------------------------------------- //
// Public API
// --------------------------------------------------------------------------- //

/**
 * Begin guidance.
 *
 * @param {{map: any, route: any, destination: {lat: number, lon: number},
 *          onEnd?: () => void}} options
 */
export async function startNavigation(options) {
  stopNavigation();

  const root = document.getElementById('nav-root');
  if (!root) throw new Error('nav-root is missing from the shell');

  const plan = normalizeRoute(options.route, { lang: 'es' });

  session = {
    map: options.map,
    raw: options.route,
    plan,
    destination: options.destination,
    chrome: buildChrome(root),
    history: [],
    spoken: new Set(),
    lastIndex: 0,
    lastRerouteAt: 0,
    rerouting: false,
    onEnd: options.onEnd,
    wakeLock: null,
    watchId: null,
    unwatchVisibility: null,
  };

  // Android needs a user gesture before the first utterance, and starting
  // navigation is one.
  primeVoices();
  showSafetyNoticeOnce(root);

  session.wakeLock = await acquireWakeLock();
  if (!session.wakeLock) {
    toast('La pantalla puede apagarse: no se pudo mantener encendida', { durationMs: 6000 });
  }
  session.unwatchVisibility = watchVisibility();

  if (!('geolocation' in navigator)) {
    toast(t('dir.noPosition'), { kind: 'error' });
    stopNavigation();
    return;
  }
  session.watchId = navigator.geolocation.watchPosition(onFix, () => {}, {
    enableHighAccuracy: true,
    maximumAge: 1000,
    timeout: 15000,
  });

  session.map?.setNightMode?.(document.documentElement.classList.contains('night'));
  speak(
    `Iniciando. ${formatDistance(plan.totalM)}, ${formatDuration(plan.totalDurationS)}`,
    { priority: 'normal' },
  );
  persist();
}

/** Stop guidance and put the screen back. */
export function stopNavigation() {
  if (!session) return;
  if (session.watchId !== null && 'geolocation' in navigator) {
    navigator.geolocation.clearWatch(session.watchId);
  }
  if (session.wakeLock) {
    try {
      session.wakeLock.release();
    } catch {
      /* already released */
    }
  }
  if (session.unwatchVisibility) session.unwatchVisibility();

  const root = document.getElementById('nav-root');
  if (root) {
    root.replaceChildren();
    root.hidden = true;
  }
  try {
    sessionStorage.removeItem(SESSION_KEY);
  } catch {
    /* ignore */
  }

  const { onEnd } = session;
  session = null;
  if (onEnd) onEnd();
}

/** True while guidance is running. */
export function isNavigating() {
  return session !== null;
}

const SAFETY_KEY = 'nicanav.safetyNotice';

/**
 * The hands-free notice, once per device.
 *
 * Not a legal formality: this app is used on a windshield mount in a country
 * where the roads demand attention, and the first run is the moment to say so.
 */
function showSafetyNoticeOnce(root) {
  try {
    if (localStorage.getItem(SAFETY_KEY)) return;
    localStorage.setItem(SAFETY_KEY, '1');
  } catch {
    return;
  }
  const notice = el('div', { class: 'nav-notice' }, [
    el('strong', { text: 'Manejá con las manos libres. ' }),
    el('span', {
      text: 'Poné el teléfono en el soporte antes de arrancar y seguí las indicaciones por voz.',
    }),
  ]);
  root.appendChild(notice);
  window.setTimeout(() => notice.remove(), 12000);
}

export { spokenDistance };
