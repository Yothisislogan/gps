/**
 * The place card and the directions sheet.
 *
 * The card is the product's answer to Google's thinnest area in Nicaragua: a
 * business here lives on Facebook and answers on WhatsApp, so those two links
 * matter more than a website, and a "verificado" date matters more than a star
 * rating nobody has left.
 *
 * "Abierto ahora" is computed in the browser from the OSM `opening_hours`
 * string. That keeps it correct at the user's local time without a server call
 * — and when the string will not parse, the raw text is shown rather than a
 * guess, because telling somebody a place is closed when it is open is worse
 * than telling them nothing.
 */

import * as api from './api.js';
import { CONFIG, categoryLabel } from './config.js';
import {
  actionRow,
  el,
  errorBlock,
  formatDate,
  formatDistance,
  formatDuration,
  sheet,
  spinner,
  t,
  toast,
} from './ui.js';

/** @type {any} */
let openingHoursLib = null;
let openingHoursFailed = false;

/**
 * Load opening_hours.js on first use.
 *
 * It is not in the app shell because most sessions never open a card that has
 * hours, and it is not small.
 */
async function loadOpeningHours() {
  if (openingHoursLib || openingHoursFailed) return openingHoursLib;
  try {
    const module = await import(CONFIG.openingHoursUrl);
    openingHoursLib = module.default || module;
  } catch {
    // A CDN failure must not blank the card; hours fall back to raw text.
    openingHoursFailed = true;
  }
  return openingHoursLib;
}

/**
 * Evaluate an OSM opening_hours string for right now.
 * @param {string} value
 * @returns {Promise<{state: 'open'|'closed'|'unknown', until?: Date}>}
 */
export async function openNow(value) {
  const library = await loadOpeningHours();
  if (!library || !value) return { state: 'unknown' };
  try {
    const parsed = new library(value, { lat: CONFIG.center[1], lon: CONFIG.center[0] });
    const now = new Date();
    const isOpen = parsed.getState(now);
    const next = parsed.getNextChange(now);
    return { state: isOpen ? 'open' : 'closed', until: next || undefined };
  } catch {
    // Nicaraguan hours are written freehand more often than not
    // ("L-V 8am-5pm, S medio día"), and a parse failure is expected.
    return { state: 'unknown' };
  }
}

function whatsappLink(number) {
  if (!number) return null;
  const digits = String(number).replace(/[^\d]/g, '');
  return digits ? `https://wa.me/${digits}` : null;
}

/**
 * Build the place card's body.
 * @param {any} card a PoiCard from the API
 * @param {{onDirections: (card: any) => void, onReport: (card: any) => void,
 *          onShare: (card: any) => void}} handlers
 */
export function renderCard(card, handlers) {
  const subtitleParts = [categoryLabel(card.category)];
  if (Array.isArray(card.cuisine) && card.cuisine.length) subtitleParts.push(card.cuisine.join(', '));
  if (card.price_level) subtitleParts.push('$'.repeat(Math.max(1, card.price_level)));

  const badges = [];
  if (card.verified_at) {
    badges.push(
      el('span', {
        class: 'badge badge--verified',
        text: `${t('common.verified')} ${formatDate(card.verified_at)}`,
      }),
    );
  }
  if (card.status === 'closed') {
    badges.push(el('span', { class: 'badge badge--closed', text: t('poi.closed') }));
  }

  const hoursNode = el('div', { class: 'card__row' });
  if (card.opening_hours) {
    hoursNode.appendChild(el('span', { class: 'card__note', text: t('hours.unknown') }));
    openNow(card.opening_hours).then((status) => {
      hoursNode.replaceChildren();
      if (status.state === 'unknown') {
        // Show what the mapper actually wrote; a human can read it.
        hoursNode.appendChild(el('span', { class: 'card__note', text: card.opening_hours }));
        return;
      }
      hoursNode.appendChild(
        el('span', {
          class: `badge badge--${status.state === 'open' ? 'open' : 'closed'}`,
          text: status.state === 'open' ? t('hours.open') : t('hours.closed'),
        }),
      );
      if (status.until) {
        hoursNode.appendChild(
          el('span', {
            class: 'card__note',
            text: `${status.state === 'open' ? t('hours.until') : t('hours.opens')} ${status.until.toLocaleTimeString('es-NI', { hour: '2-digit', minute: '2-digit' })}`,
          }),
        );
      }
    });
  }

  const whatsapp = whatsappLink(card.whatsapp || card.phone);
  const actions = actionRow([
    { label: t('poi.directions'), icon: 'directions', primary: true, onClick: () => handlers.onDirections(card) },
    card.phone ? { label: t('poi.call'), icon: 'phone', href: `tel:${card.phone}` } : null,
    whatsapp ? { label: t('poi.whatsapp'), icon: 'whatsapp', href: whatsapp } : null,
    card.facebook ? { label: t('poi.facebook'), icon: 'facebook', href: card.facebook } : null,
    card.instagram ? { label: t('poi.instagram'), icon: 'instagram', href: card.instagram } : null,
    card.website ? { label: t('poi.website'), icon: 'globe', href: card.website } : null,
    { label: t('poi.share'), icon: 'share', onClick: () => handlers.onShare(card) },
    { label: t('poi.report'), icon: 'report', onClick: () => handlers.onReport(card) },
  ]);

  const photo = (card.photos || [])[0];

  return el('div', { class: 'card' }, [
    el('div', { class: 'card__subtitle', text: subtitleParts.filter(Boolean).join(' · ') }),
    badges.length ? el('div', { class: 'card__row' }, badges) : null,
    card.opening_hours ? hoursNode : null,
    card.address_text
      ? el('div', { class: 'card__address' }, [
          el('code', { text: card.address_text }),
        ])
      : null,
    photo
      ? el('div', {}, [
          el('img', {
            class: 'card__photo',
            src: photo.url,
            alt: card.name,
            loading: 'lazy',
            decoding: 'async',
          }),
          el('div', {
            class: 'card__credit',
            text: `${t('poi.photoCredit')} ${photo.credit || ''} ${photo.license || ''}`.trim(),
          }),
        ])
      : null,
    Array.isArray(card.alt_names) && card.alt_names.length
      ? el('div', { class: 'card__note', text: `${t('poi.altNames')}: ${card.alt_names.join(', ')}` })
      : null,
    actions,
  ]);
}

/**
 * Fetch and show one place.
 * @param {string} poiId
 * @param {any} context the shared app context from map.js
 */
export async function openPlaceCard(poiId, context) {
  sheet.open(el('div', { class: 'card' }, [spinner()]), { title: '' });
  try {
    const card = await api.poi(poiId);
    sheet.open(renderCard(card, handlersFor(context, card)), { title: card.name });
    if (context && context.map) {
      context.map.setMarker('selected', card.lat, card.lon, { kind: 'selected', title: card.name });
    }
    return card;
  } catch (error) {
    const message = error && error.code === 'poi_not_found' ? t('poi.notFound') : t('search.failed');
    sheet.open(el('div', { class: 'card' }, [errorBlock(message)]), { title: '' });
    return null;
  }
}

/**
 * Show a point that is already in hand — a long-press, a search hit, a geocoded
 * pin — with no round trip.
 *
 * When the point came from a low-confidence address parse, the card offers to
 * confirm it. That is the flywheel: every corrected pin becomes ground truth
 * for the next person who types the same address, and over time this is how the
 * country ends up with an address database.
 *
 * @param {{lat: number, lon: number, name?: string, id?: string, category?: string,
 *          query?: string, candidate?: any}} place
 * @param {any} context
 */
export function openPointCard(place, context) {
  const handlers = handlersFor(context, place);
  const rows = [
    el('div', {
      class: 'card__subtitle',
      text: [categoryLabel(place.category), place.address_text || place.city]
        .filter(Boolean)
        .join(' · '),
    }),
  ];

  // What would a Nicaraguan call this spot? Asked in the background, because
  // it is the string people actually send over WhatsApp.
  const addressNode = el('div', { class: 'card__address' }, [
    el('code', { text: t('share.addressLoading') }),
  ]);
  rows.push(addressNode);
  api
    .reverse(place.lat, place.lon)
    .then((response) => {
      const candidate = (response.candidates || [])[0];
      addressNode.replaceChildren(el('code', { text: candidate ? candidate.label : t('share.addressFailed') }));
    })
    .catch(() => {
      addressNode.replaceChildren(el('code', { text: t('share.addressFailed') }));
    });

  if (place.candidate && place.query && place.candidate.confidence < CONFIRM_BELOW) {
    rows.push(
      el('div', { class: 'card__note', text: `${t('common.confidence')}: ${Math.round(place.candidate.confidence * 100)}%` }),
      actionRow([
        {
          label: t('common.here'),
          icon: 'check',
          onClick: async () => {
            try {
              await api.saveAlias({ text: place.query, lat: place.lat, lon: place.lon });
              toast(t('report.thanks'));
            } catch {
              toast(t('report.failed'), { kind: 'error' });
            }
          },
        },
      ]),
    );
  }

  rows.push(
    actionRow([
      { label: t('poi.directions'), icon: 'directions', primary: true, onClick: () => handlers.onDirections(place) },
      { label: t('poi.share'), icon: 'share', onClick: () => handlers.onShare(place) },
      { label: t('poi.report'), icon: 'report', onClick: () => handlers.onReport(place) },
      place.id ? { label: t('common.open'), icon: 'chevron', onClick: () => openPlaceCard(place.id, context) } : null,
    ]),
  );

  sheet.open(el('div', { class: 'card' }, rows), {
    title: place.name || t('common.here'),
  });
}

/** Below this confidence a parsed address asks the user to confirm the pin. */
const CONFIRM_BELOW = 0.85;

/** The card's buttons, bound to the shared context. */
function handlersFor(context, place) {
  return {
    onDirections: (target) => openDirections(target || place, context),
    onReport: (target) => openReport(context, target || place),
    onShare: (target) => {
      const point = target || place;
      if (context && context.openShare) context.openShare({ lat: point.lat, lon: point.lon, name: point.name });
    },
  };
}

// --------------------------------------------------------------------------- //
// Directions
// --------------------------------------------------------------------------- //

const MANEUVER_GLYPH = {
  1: '↑', 2: '↑', 3: '↑', 4: '⚑', 5: '⚑', 6: '⚑',
  9: '↖', 10: '↗', 11: '↑', 14: '↰', 15: '↖', 16: '↗', 17: '↱',
  18: '↰', 19: '↱', 20: '↰', 21: '↱', 22: '↑', 23: '↖', 24: '↗',
  26: '↻', 27: '↻', 37: '↑',
};

/** Spanish maneuver arrow for a Valhalla maneuver type. */
export function maneuverGlyph(type) {
  return MANEUVER_GLYPH[type] || '↑';
}

/**
 * Render the route sheet: summary, alternates, and the turn list.
 * @param {any} response the /api/route reply (native Valhalla format)
 * @param {{onStart: () => void, onPickAlternate?: (index: number) => void, active?: number,
 *          avoidUnpaved?: boolean, onAvoidUnpavedChange?: (value: boolean) => void}} handlers
 */
export function renderRoute(response, handlers) {
  const routes = [response, ...(response.alternates || [])];
  const active = handlers.active ?? 0;
  const trip = (routes[active] || response).trip || {};
  const summary = trip.summary || {};
  const legs = trip.legs || [];
  const maneuvers = legs.flatMap((leg) => leg.maneuvers || []);

  const steps = el(
    'ol',
    { class: 'steps' },
    maneuvers.map((maneuver) =>
      el('li', { class: 'step' }, [
        el('span', { class: 'step__glyph', 'aria-hidden': 'true', text: maneuverGlyph(maneuver.type) }),
        el('span', { class: 'step__text', text: maneuver.instruction || '' }),
        maneuver.length
          ? el('span', { class: 'step__distance', text: formatDistance(maneuver.length * 1000) })
          : null,
      ]),
    ),
  );

  const alternateButtons = routes.length > 1 ? routes.map((alternate, index) => {
    const alternateSummary = (alternate.trip || {}).summary || {};
    return el(
      'button',
      {
        class: 'route__alt',
        type: 'button',
        'aria-pressed': String(active === index),
        onClick: () => handlers.onPickAlternate && handlers.onPickAlternate(index),
      },
      [
        el('span', { text: t('dir.route', { n: index + 1 }) }),
        el('span', {
          text: `${formatDistance((alternateSummary.length || 0) * 1000)} · ${formatDuration(alternateSummary.time || 0)}`,
        }),
      ],
    );
  }) : [];

  return el('div', { class: 'card' }, [
    el('div', { class: 'route__summary' }, [
      el('span', { class: 'route__distance', text: formatDistance((summary.length || 0) * 1000) }),
      el('span', { class: 'route__duration', text: formatDuration(summary.time || 0) }),
    ]),
    response.nicanav && response.nicanav.closures_applied
      ? el('div', {
          class: 'card__note',
          text: `${response.nicanav.closures_applied} cierre(s) evitado(s)`,
        })
      : null,
    response.nicanav?.closures_status === 'unavailable'
      ? el('div', { class: 'card__note', role: 'status', text: t('dir.closuresUnavailable') })
      : null,
    handlers.onAvoidUnpavedChange ? el('label', { class: 'setting' }, [
      el('input', {
        type: 'checkbox', checked: handlers.avoidUnpaved,
        onChange: (event) => handlers.onAvoidUnpavedChange(event.target.checked),
      }),
      el('span', { text: t('dir.avoidUnpaved') }),
    ]) : null,
    actionRow([{ label: t('dir.start'), icon: 'directions', primary: true, onClick: handlers.onStart }]),
    ...alternateButtons,
    steps,
  ]);
}

/**
 * Route from the driver to a destination and show the result.
 *
 * @param {{lat: number, lon: number, name?: string}} destination
 * @param {any} context the shared app context from map.js
 */
let directionsRequest = null;

export async function openDirections(destination, context) {
  directionsRequest?.abort();
  const controller = directionsRequest = new AbortController();
  let fix = context.getPosition ? context.getPosition() : null;
  if (!fix && context.requestPosition) {
    try {
      fix = await context.requestPosition();
    } catch {
      fix = null;
    }
  }
  if (!fix) {
    toast(t('dir.noPosition'), { kind: 'error' });
    return null;
  }
  if (controller.signal.aborted) return null;

  const sheetOptions = { title: t('dir.title'), onClose: () => controller.abort() };
  sheet.open(el('div', { class: 'card' }, [spinner(t('dir.calculating'))]), sheetOptions);
  try {
    const routeOptions = {
      costing: 'auto', avoid_unpaved: context.avoidUnpaved ?? readAvoidUnpaved(), language: 'es-ES',
    };
    const response = await api.route({
      ...routeOptions,
      locations: [
        { lat: fix.lat, lon: fix.lon },
        { lat: destination.lat, lon: destination.lon },
      ],
      alternates: 2,
    }, { signal: controller.signal });
    if (controller.signal.aborted) return null;

    const choose = (active) => {
      const selected = [response, ...(response.alternates || [])][active];
      const chosen = { ...selected, nicanav: response.nicanav };
      const shape = chosen.trip?.legs?.[0]?.shape;
      if (shape && context.map) context.map.showRoute(shape, { fit: true });
      sheet.open(renderRoute(response, {
        active,
        onPickAlternate: choose,
        avoidUnpaved: routeOptions.avoid_unpaved,
        onAvoidUnpavedChange: (value) => {
          try { localStorage.setItem(AVOID_UNPAVED_KEY, value ? '1' : '0'); } catch { /* optional */ }
          openDirections(destination, { ...context, avoidUnpaved: value });
        },
        onStart: () => beginNavigation(chosen, destination, context, routeOptions),
      }), sheetOptions);
    };
    choose(0);
    return response;
  } catch (error) {
    if (controller.signal.aborted) return null;
    const message = (error && error.message) || t('dir.failed');
    sheet.open(el('div', { class: 'card' }, [errorBlock(message)]), { title: t('dir.title') });
    return null;
  }
}

const AVOID_UNPAVED_KEY = 'nicanav.avoidUnpaved';

/**
 * "Evitar caminos de tierra", off by default.
 *
 * Default off on purpose: plenty of Nicaraguan places are only reachable on
 * dirt, so excluding it outright would make them unroutable. The build-time
 * speed table already makes a dirt shortcut lose to a paved detour.
 */
function readAvoidUnpaved() {
  try {
    return localStorage.getItem(AVOID_UNPAVED_KEY) === '1';
  } catch {
    return false;
  }
}

/**
 * Hand off to the navigation module, which is loaded on demand.
 *
 * Navigation is a large chunk of code that most sessions never use: somebody
 * checking whether a fritanga is open should not pay for it.
 */
async function beginNavigation(response, destination, context, routeOptions) {
  try {
    const nav = await import('./nav.js');
    sheet.close();
    await nav.startNavigation({
      map: context.map,
      route: response,
      destination: { lat: destination.lat, lon: destination.lon },
      routeOptions,
      onEnd: () => context.map && context.map.clearRoute(),
    });
  } catch (error) {
    toast(t('dir.navUnavailable'), { kind: 'error' });
    console.error('nicanav: navigation failed to start', error);
  }
}

const REPORT_KINDS = [
  { kind: 'via_cerrada', label: 'Vía cerrada' },
  { kind: 'bache', label: 'Bache' },
  { kind: 'inundacion', label: 'Inundación' },
  { kind: 'sentido_incorrecto', label: 'Sentido incorrecto' },
  { kind: 'negocio_cerrado', label: 'Negocio cerrado' },
];

/**
 * Report a problem: one tap, five choices, at the driver's position.
 *
 * Everything here lands in a moderation queue rather than on the map, which is
 * what keeps a crowd-sourced map usable.
 *
 * @param {any} context
 * @param {{lat: number, lon: number}} [place] report about a place rather than here
 */
export function openReport(context, place = null) {
  const point = place || (context.getPosition ? context.getPosition() : null);
  if (!point) {
    toast(t('report.noPosition'), { kind: 'error' });
    if (context.requestPosition) context.requestPosition().catch(() => undefined);
    return;
  }

  const note = el('input', {
    class: 'searchbar__input',
    type: 'text',
    placeholder: t('report.note'),
    maxLength: 200,
  });

  const send = async (kind) => {
    try {
      await api.report({ kind, lat: point.lat, lon: point.lon, note: note.value || null });
      sheet.close();
      toast(t('report.thanks'));
    } catch {
      toast(t('report.failed'), { kind: 'error' });
    }
  };

  sheet.open(
    el('div', { class: 'card' }, [
      el('div', { class: 'card__subtitle', text: t('report.title') }),
      actionRow(REPORT_KINDS.map((entry) => ({ label: entry.label, icon: 'report', onClick: () => send(entry.kind) }))),
      el('div', { class: 'card__row' }, [note]),
    ]),
    { title: t('report.title') },
  );
}
