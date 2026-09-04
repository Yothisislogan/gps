/**
 * Deep links and the share sheet.
 *
 * Sharing a location in Nicaragua is not a pair of coordinates — it is a
 * sentence ("del Colegio Teresiano, 1c al lago, casa de portón verde") with the
 * coordinates attached as a check.  So the share sheet always offers both, and
 * puts WhatsApp first, because that is the channel this actually travels on.
 */

import { reverse } from './api.js';
import { CONFIG } from './config.js';
import { el, sheet, spinner, t, toast, clear, icon } from './ui.js';

/** `/@12.1415,-86.1682,16z` — the shape Google trained everyone to recognise. */
const DEEP_LINK_RE = /@(-?\d{1,2}(?:\.\d+)?),(-?\d{1,3}(?:\.\d+)?)(?:,(\d{1,2}(?:\.\d+)?)z?)?/;

/**
 * Read a shared location out of a URL or path.
 *
 * @param {string} input `location.pathname + location.hash`, a full URL, or a
 *   pasted link
 * @returns {{lat: number, lon: number, zoom: number}|null} null when the string
 *   carries no usable location
 */
export function parseDeepLink(input) {
  if (!input) return null;
  const match = DEEP_LINK_RE.exec(String(input));
  if (!match) return null;
  const lat = Number(match[1]);
  const lon = Number(match[2]);
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
  if (lat < -90 || lat > 90 || lon < -180 || lon > 180) return null;
  const zoom = Number(match[3]);
  return {
    lat,
    lon,
    zoom: Number.isFinite(zoom) ? Math.min(CONFIG.maxZoom, Math.max(1, zoom)) : 16,
  };
}

/**
 * The canonical shareable URL for a point.
 *
 * Five decimals is about a metre — enough to find a gate, short enough to
 * survive being retyped from a WhatsApp message.
 *
 * @param {number} lat
 * @param {number} lon
 * @param {number} [zoom]
 * @returns {string}
 */
export function deepLinkUrl(lat, lon, zoom = 17) {
  const origin = typeof window !== 'undefined' ? window.location.origin : '';
  const z = Math.round(Math.min(CONFIG.maxZoom, Math.max(1, zoom)) * 10) / 10;
  return `${origin}/@${lat.toFixed(5)},${lon.toFixed(5)},${z}z`;
}

/**
 * Keep the address bar on a link that can be copied and sent at any moment.
 *
 * `replaceState` rather than `pushState`: panning the map should not fill the
 * back stack, or the hardware back button stops leaving the app.
 *
 * @param {any} map a MapLibre map instance
 */
export function writeDeepLink(map) {
  if (!map || typeof map.getCenter !== 'function' || typeof history === 'undefined') return;
  try {
    const centre = map.getCenter();
    const zoom = Math.round(map.getZoom() * 10) / 10;
    const path = `/@${centre.lat.toFixed(5)},${centre.lng.toFixed(5)},${zoom}z${window.location.search}`;
    history.replaceState(null, '', path);
  } catch {
    /* some in-app browsers block history writes; the map keeps working */
  }
}

/**
 * Copy text, reporting the result to the user either way.
 * @param {string} value
 * @returns {Promise<void>}
 */
async function copy(value) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(value);
      toast(t('common.copied'), { kind: 'success' });
      return;
    }
    throw new Error('clipboard unavailable');
  } catch {
    // Older Android WebViews have no async clipboard; the deprecated path still
    // works there and failing silently would look like a dead button.
    const area = el('textarea', { class: 'offscreen', value });
    document.body.appendChild(area);
    area.select();
    let ok = false;
    try {
      ok = document.execCommand('copy');
    } catch {
      ok = false;
    }
    document.body.removeChild(area);
    toast(ok ? t('common.copied') : t('share.addressFailed'), { kind: ok ? 'success' : 'error' });
  }
}

/**
 * Assemble the message people actually send.
 * @param {{name?: string, address?: string, lat: number, lon: number, url: string}} parts
 * @returns {string}
 */
function shareText(parts) {
  const lines = [];
  if (parts.name) lines.push(parts.name);
  if (parts.address) lines.push(parts.address);
  lines.push(`${parts.lat.toFixed(5)}, ${parts.lon.toFixed(5)}`);
  lines.push(parts.url);
  return lines.join('\n');
}

/**
 * A labelled, copyable row.
 * @param {string} label
 * @param {string} value
 * @returns {HTMLElement}
 */
function copyRow(label, value) {
  return el('div', { class: 'share-row' }, [
    el('div', { class: 'share-row-text' }, [
      el('span', { class: 'share-row-label', text: label }),
      el('span', { class: 'share-row-value', text: value }),
    ]),
    el(
      'button',
      { class: 'icon-button', type: 'button', 'aria-label': `${t('common.copy')}: ${label}`, onClick: () => copy(value) },
      icon('share', 20),
    ),
  ]);
}

/**
 * Open the share sheet for a point, fetching the relative address in the
 * background so the coordinates are shareable immediately.
 *
 * @param {{lat: number, lon: number, name?: string}} point
 * @param {{map?: any}} [context]
 * @returns {void}
 */
export function openShareSheet(point, context = {}) {
  const zoom = context.map && context.map.map ? context.map.map.getZoom() : 17;
  const url = deepLinkUrl(point.lat, point.lon, zoom);
  const coords = `${point.lat.toFixed(5)}, ${point.lon.toFixed(5)}`;

  const addressSlot = el('div', { class: 'share-address' }, spinner(t('share.addressLoading')));
  /** @type {string} */
  let address = '';

  const buttons = el('div', { class: 'share-actions' });

  const renderButtons = () => {
    clear(buttons);
    const message = shareText({ name: point.name, address, lat: point.lat, lon: point.lon, url });
    buttons.appendChild(
      el(
        'a',
        {
          class: 'btn btn--primary btn--block',
          href: `https://wa.me/?text=${encodeURIComponent(message)}`,
          target: '_blank',
          rel: 'noopener noreferrer',
        },
        [icon('whatsapp', 22), el('span', { text: t('share.whatsapp') })],
      ),
    );
    if (typeof navigator !== 'undefined' && navigator.share) {
      buttons.appendChild(
        el(
          'button',
          {
            class: 'btn btn--block',
            type: 'button',
            onClick: () => {
              navigator.share({ title: point.name || t('share.title'), text: message, url }).catch(() => {
                // The user dismissing the OS share sheet rejects too; nothing to say.
              });
            },
          },
          [icon('share', 22), el('span', { text: t('share.system') })],
        ),
      );
    }
    buttons.appendChild(
      el(
        'button',
        { class: 'btn btn--block', type: 'button', onClick: () => copy(message) },
        [icon('check', 22), el('span', { text: t('share.copyAll') })],
      ),
    );
  };

  renderButtons();

  sheet.open(
    el('div', { class: 'share' }, [
      addressSlot,
      copyRow(t('share.coords'), coords),
      copyRow(t('share.link'), url),
      buttons,
    ]),
    { title: t('share.title') },
  );

  reverse(point.lat, point.lon)
    .then((response) => {
      const candidate = (response.candidates || [])[0];
      clear(addressSlot);
      if (!candidate || !candidate.label) {
        addressSlot.appendChild(el('p', { class: 'muted', text: t('share.addressFailed') }));
        return;
      }
      address = candidate.label;
      addressSlot.appendChild(
        el('div', { class: 'share-row-text' }, [
          el('span', { class: 'share-row-label', text: t('share.address') }),
          el('span', { class: 'share-row-value share-row-value--strong', text: address }),
        ]),
      );
      renderButtons();
    })
    .catch((error) => {
      clear(addressSlot);
      addressSlot.appendChild(
        el('p', { class: 'muted', text: error && error.message ? error.message : t('share.addressFailed') }),
      );
    });
}
