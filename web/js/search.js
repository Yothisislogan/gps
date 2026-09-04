/**
 * Search: one request per keystroke, whatever the user is typing.
 *
 * The API answers a place search and an address parse in the same payload, so
 * this module renders both from one response. That is a product decision, not
 * an optimisation: nobody typing "de la rotonda 2c al sur" thinks of it as a
 * different *kind* of query from "fritanga", and on a Claro connection a second
 * round trip is felt.
 *
 * Everything here is DOM-node construction — never innerHTML. Place names come
 * from OSM and Overture, which is to say from the public, and one of them will
 * eventually contain a script tag.
 */

import * as api from './api.js';
import { CHIPS, CONFIG, categoryLabel } from './config.js';
import { clear, el, errorBlock, formatDistance, isOffline, spinner, t } from './ui.js';

const RECENT_KEY = 'nicanav.recent';

/** @type {AbortController|null} */
let inFlight = null;
let debounceTimer = 0;

/**
 * Recent searches, newest first. Stored per device: on a shared family phone
 * this is the difference between finding the clinic again in one tap and
 * retyping it.
 * @returns {Array<{q: string, id?: string, name?: string, lat?: number, lon?: number}>}
 */
export function recentSearches() {
  try {
    const parsed = JSON.parse(localStorage.getItem(RECENT_KEY) || '[]');
    return Array.isArray(parsed) ? parsed.slice(0, CONFIG.maxRecentSearches) : [];
  } catch {
    return [];
  }
}

/** Remember a chosen result. */
export function rememberSearch(entry) {
  if (!entry || !entry.q) return;
  const existing = recentSearches().filter(
    (item) => item.q.toLowerCase() !== String(entry.q).toLowerCase(),
  );
  const next = [entry, ...existing].slice(0, CONFIG.maxRecentSearches);
  try {
    localStorage.setItem(RECENT_KEY, JSON.stringify(next));
  } catch {
    /* private mode, or a full quota: recents are a convenience, not state */
  }
}

export function clearRecentSearches() {
  try {
    localStorage.removeItem(RECENT_KEY);
  } catch {
    /* ignore */
  }
}

/**
 * Render the category chips.
 * @param {HTMLElement} container
 * @param {(category: string|null) => void} onPick
 */
export function renderChips(container, onPick) {
  clear(container);
  let active = null;
  for (const chip of CHIPS) {
    const button = el('button', {
      class: 'chip',
      type: 'button',
      text: chip.label,
      dataset: { category: chip.category },
      'aria-pressed': 'false',
      onClick: () => {
        const next = active === chip.category ? null : chip.category;
        active = next;
        for (const node of container.querySelectorAll('.chip')) {
          node.setAttribute('aria-pressed', String(node === button && next !== null));
        }
        onPick(next);
      },
    });
    container.appendChild(button);
  }
}

/**
 * One search result row.
 * @param {{glyph?: string, title: string, meta?: string, distanceM?: number|null,
 *          badges?: Array<{text: string, kind: string}>, onSelect: () => void}} spec
 */
function resultRow(spec) {
  const badges = (spec.badges || []).map((badge) =>
    el('span', { class: `badge badge--${badge.kind}`, text: badge.text }),
  );
  return el(
    'button',
    { class: 'result', type: 'button', onClick: spec.onSelect },
    [
      el('span', { class: 'result__glyph', 'aria-hidden': 'true', text: spec.glyph || '•' }),
      el('span', { class: 'result__body' }, [
        el('span', { class: 'result__title' }, [
          el('span', { class: 'result__name', text: spec.title }),
          ...badges,
        ]),
        spec.meta ? el('span', { class: 'result__meta', text: spec.meta }) : null,
      ]),
      spec.distanceM != null
        ? el('span', { class: 'result__distance', text: formatDistance(spec.distanceM) })
        : null,
    ],
  );
}

function group(headingKey, rows, extra = null) {
  if (!rows.length) return null;
  return el('div', { class: 'results__group' }, [
    el('div', { class: 'results__heading', text: t(headingKey) }),
    ...rows,
    extra,
  ]);
}

/**
 * Describe a geocoded candidate the way the user can check it.
 *
 * Confidence is shown deliberately: a pin that quietly claims certainty and
 * lands two blocks away teaches people to distrust every pin, while one that
 * says "70 %" and invites a drag teaches them to correct it.
 */
function candidateMeta(candidate) {
  const parts = [];
  if (candidate.relative && Array.isArray(candidate.relative.offsets)) {
    const hops = candidate.relative.offsets
      .map((offset) => `${formatDistance(offset.distance_m)} ${offset.direction_text}`)
      .join(' · ');
    if (hops) parts.push(hops);
  }
  if (candidate.landmark_name) parts.push(`desde ${candidate.landmark_name}`);
  if (Array.isArray(candidate.notes) && candidate.notes.length) parts.push(candidate.notes[0]);
  return parts.join(' — ');
}

/**
 * Render a whole result set.
 *
 * @param {HTMLElement} container
 * @param {any} payload the /api/search response
 * @param {{onPlace: (hit: any) => void, onCandidate: (candidate: any) => void}} handlers
 */
export function renderResults(container, payload, handlers) {
  clear(container);

  const candidates = (payload.geocode && payload.geocode.candidates) || [];
  const hits = payload.hits || [];

  const addressRows = candidates.map((candidate) =>
    resultRow({
      glyph: '⌖',
      title: candidate.label,
      meta: candidateMeta(candidate),
      badges: [
        {
          text: `${Math.round((candidate.confidence || 0) * 100)}%`,
          kind: 'confidence',
        },
      ],
      onSelect: () => handlers.onCandidate(candidate),
    }),
  );

  const placeRows = hits.map((hit) =>
    resultRow({
      glyph: '◉',
      title: hit.name,
      meta: [categoryLabel(hit.category), hit.address_text || hit.city].filter(Boolean).join(' · '),
      distanceM: hit.distance_m,
      badges: hit.verified ? [{ text: t('common.verified'), kind: 'verified' }] : [],
      onSelect: () => handlers.onPlace(hit),
    }),
  );

  const groups = [
    group('search.addresses', addressRows),
    group('search.places', placeRows),
  ].filter(Boolean);

  if (!groups.length) {
    container.appendChild(
      el('div', { class: 'empty' }, [
        el('div', { text: t('search.empty') }),
        el('div', { class: 'empty__hint', text: t('search.empty.hint') }),
      ]),
    );
    return;
  }
  for (const node of groups) container.appendChild(node);
}

/** Render the recent-search list shown before anything is typed. */
export function renderRecents(container, handlers) {
  clear(container);
  const recents = recentSearches();
  if (!recents.length) {
    container.appendChild(
      el('div', { class: 'empty' }, [
        el('div', { text: t('search.empty') }),
        el('div', { class: 'empty__hint', text: t('search.empty.hint') }),
      ]),
    );
    return;
  }
  const rows = recents.map((entry) =>
    resultRow({
      glyph: '↺',
      title: entry.name || entry.q,
      meta: entry.name && entry.name !== entry.q ? entry.q : '',
      onSelect: () => handlers.onRecent(entry),
    }),
  );
  container.appendChild(
    group(
      'search.recent',
      rows,
      el('button', {
        class: 'result',
        type: 'button',
        text: t('search.recent.clear'),
        onClick: () => {
          clearRecentSearches();
          renderRecents(container, handlers);
        },
      }),
    ),
  );
}

/**
 * Run a search, debounced, with the previous request aborted.
 *
 * Aborting matters more than debouncing here: a fast typist on a slow link
 * otherwise gets results for "fri" arriving after results for "fritanga".
 *
 * @param {string} query
 * @param {{lat?: number, lon?: number, category?: string|null,
 *          onStart?: () => void, onDone: (payload: any) => void,
 *          onError: (error: Error) => void}} options
 */
export function runSearch(query, options) {
  clearTimeout(debounceTimer);
  if (inFlight) inFlight.abort();

  const trimmed = query.trim();
  if (!trimmed && !options.category) {
    options.onDone({ hits: [], geocode: null, query: '' });
    return;
  }

  debounceTimer = window.setTimeout(async () => {
    const controller = new AbortController();
    inFlight = controller;
    if (options.onStart) options.onStart();
    try {
      const payload = await api.search(trimmed || (options.category ?? ''), {
        lat: options.lat,
        lon: options.lon,
        category: options.category || undefined,
        radiusM: options.category ? CONFIG.chipRadiusM : undefined,
        limit: 25,
        signal: controller.signal,
      });
      if (!controller.signal.aborted) options.onDone(payload);
    } catch (error) {
      if (controller.signal.aborted || (error && error.name === 'AbortError')) return;
      options.onError(/** @type {Error} */ (error));
    } finally {
      if (inFlight === controller) inFlight = null;
    }
  }, CONFIG.searchDebounceMs);
}

/** The searching / offline / failed states, so the list is never just blank. */
export function renderState(container, state, onRetry) {
  clear(container);
  if (state === 'loading') {
    container.appendChild(el('div', { class: 'empty' }, [spinner(t('search.searching'))]));
  } else if (state === 'offline') {
    container.appendChild(
      el('div', { class: 'empty' }, [
        el('div', { text: t('search.offline') }),
        el('div', { class: 'empty__hint', text: t('search.offline.hint') }),
      ]),
    );
  } else {
    container.appendChild(errorBlock(t('search.failed'), onRetry));
  }
}

/**
 * Wire the search bar, the chips and the result list.
 *
 * Called once by map.js's bootstrap with the shared context, which is how every
 * screen reaches the map and the driver's position without a global.
 *
 * @param {{map: any, getPosition: () => any, requestPosition: () => Promise<any>,
 *          openPlaceCard: (id: string) => void,
 *          openPointCard: (point: any) => void}} context
 */
export function createSearch(context) {
  const form = document.getElementById('search-form');
  const input = /** @type {HTMLInputElement|null} */ (document.getElementById('search-input'));
  const results = document.getElementById('search-results');
  const chips = document.getElementById('chips');
  const clearButton = document.getElementById('search-clear');
  const backButton = document.getElementById('search-back');
  if (!input || !results) return null;

  let activeCategory = null;

  const show = (visible) => {
    results.hidden = !visible;
    if (backButton) backButton.hidden = !visible;
    if (clearButton) clearButton.hidden = !input.value;
  };

  const handlers = {
    onPlace(hit) {
      rememberSearch({ q: input.value || hit.name, id: hit.id, name: hit.name, lat: hit.lat, lon: hit.lon });
      show(false);
      input.blur();
      context.map.setMarker('selected', hit.lat, hit.lon, { kind: 'selected', title: hit.name });
      context.map.flyTo(hit.lat, hit.lon, { zoom: 17 });
      if (hit.kind === 'poi' && hit.id) context.openPlaceCard(hit.id);
      else context.openPointCard({ lat: hit.lat, lon: hit.lon, name: hit.name });
    },
    onCandidate(candidate) {
      rememberSearch({ q: input.value, name: candidate.label, lat: candidate.lat, lon: candidate.lon });
      show(false);
      input.blur();
      context.map.setMarker('selected', candidate.lat, candidate.lon, {
        kind: 'selected',
        title: candidate.label,
      });
      context.map.flyTo(candidate.lat, candidate.lon, { zoom: 17 });
      context.openPointCard({
        lat: candidate.lat,
        lon: candidate.lon,
        name: candidate.label,
        // The confirm-the-pin loop lives on the card: a low-confidence parse
        // invites a correction, and a corrected pin becomes ground truth for
        // the next person who types the same address.
        query: input.value,
        candidate,
      });
    },
    onRecent(entry) {
      input.value = entry.q;
      run();
    },
  };

  function run() {
    show(true);
    const query = input.value;
    if (!query.trim() && !activeCategory) {
      renderRecents(results, handlers);
      return;
    }
    if (isOffline()) {
      renderState(results, 'offline');
      return;
    }
    const fix = context.getPosition();
    runSearch(query, {
      lat: fix ? fix.lat : undefined,
      lon: fix ? fix.lon : undefined,
      category: activeCategory,
      onStart: () => renderState(results, 'loading'),
      onDone: (payload) => renderResults(results, payload, handlers),
      onError: () => renderState(results, 'error', run),
    });
  }

  input.addEventListener('input', run);
  input.addEventListener('focus', () => {
    if (!input.value) renderRecents(results, handlers);
    show(true);
  });
  if (form) {
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      input.blur();
      run();
    });
  }
  if (clearButton) {
    clearButton.addEventListener('click', () => {
      input.value = '';
      input.focus();
      run();
    });
  }
  if (backButton) {
    backButton.addEventListener('click', () => {
      show(false);
      input.blur();
    });
  }

  if (chips) {
    renderChips(chips, async (category) => {
      activeCategory = category;
      if (!category) {
        show(false);
        return;
      }
      // A category chip means "near me"; without a fix it would return the
      // whole country in arbitrary order.
      if (!context.getPosition()) {
        try {
          await context.requestPosition();
        } catch {
          /* the search still runs, just unbiased */
        }
      }
      run();
    });
  }

  // The manifest's shortcuts ("Gasolineras cerca") land here.
  const requested = new URLSearchParams(window.location.search).get('chip');
  if (requested) {
    activeCategory = requested;
    const chipButton = chips && chips.querySelector(`.chip[data-category="${requested}"]`);
    if (chipButton) chipButton.setAttribute('aria-pressed', 'true');
    context
      .requestPosition()
      .catch(() => undefined)
      .finally(run);
  }

  return { run, show };
}
