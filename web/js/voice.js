/**
 * Spoken guidance over the Web Speech API.
 *
 * Three things make this harder than `speechSynthesis.speak(new Utterance(x))`:
 *
 * 1. Android's Google TTS loads its voice list asynchronously and the
 *    `voiceschanged` event fires unreliably, so the list is polled as well as
 *    listened for, with a cap and a working fallback if it never arrives.
 * 2. Chrome for Android refuses to speak until the page has had a user gesture,
 *    and it silently drops the utterance rather than throwing — hence
 *    {@link primeVoices}, which must be called from inside a real tap.
 * 3. Valhalla's Spanish is peninsular. Some of it is merely foreign-sounding
 *    ("glorieta"), and at least one word is genuinely crude in Nicaragua, so
 *    the text is rewritten before it is spoken (see {@link NICARAGUAN_PHRASING}).
 *
 * The queue is ours, not the browser's: `speechSynthesis` has no notion of
 * priority, and "girá a la derecha" arriving behind a stale "continuá por dos
 * kilómetros" is how a driver misses a turn.
 */

const STORAGE_KEY = 'nicanav.voice.v1';

/** Voice tags in the order we would like them, best first. */
const PREFERRED_ES = ['es-ni', 'es-mx', 'es-us', 'es-419', 'es-co', 'es-cr', 'es-gt', 'es-es', 'es'];
const PREFERRED_EN = ['en-us', 'en-gb', 'en'];

/** A prompt older than this is dropped: a turn instruction said late is worse than silence. */
const STALE_MS = 9000;

/** Rough Spanish TTS rate, used only to size the watchdog that unsticks the queue. */
const CHARS_PER_SECOND = 13;

const state = {
  enabled: loadEnabled(),
  lang: 'es-NI',
  voices: [],
  voicesResolved: false,
  chosen: { es: null, en: null },
  queue: /** @type {{text: string, lang: string, priority: string, queuedAt: number}[]} */ ([]),
  speaking: false,
  watchdog: 0,
  primed: false,
};

/**
 * Substitutions applied to every Spanish utterance, in order.
 *
 * Each entry says *why*, because a blind find-and-replace on a road name is how
 * you end up announcing a street that does not exist.  Patterns are anchored on
 * word boundaries and deliberately narrow: nothing here touches a proper noun.
 */
const NICARAGUAN_PHRASING = [
  {
    // Peninsular Spanish for a roundabout. Nicaragua says "rotonda", always —
    // the rotondas are the country's primary navigation landmarks.
    pattern: /\bglorietas?\b/gi,
    replace: (match) => (match.endsWith('s') ? 'rotondas' : 'rotonda'),
  },
  {
    // "Cambio de sentido" is the Spanish term; the Nicaraguan word for the
    // U-turn bays on Carretera a Masaya and Pista Juan Pablo II is "retorno".
    pattern: /\bcambios? de sentido\b/gi,
    replace: (match) => (match.startsWith('cambios') ? 'retornos' : 'retorno'),
  },
  {
    // Valhalla's es-ES narrative uses "coger" for taking an exit. In Nicaragua
    // that verb is obscene. This is the substitution that most needs to exist.
    pattern: /\bcoj([ae])\b/gi,
    replace: (_m, vowel) => (vowel === 'a' ? 'tome' : 'toma'),
  },
  { pattern: /\bcoger\b/gi, replace: () => 'tomar' },
  {
    // "1ª salida" is read by Android TTS as "uno a salida".
    pattern: /\b(\d+)\s*[ªº°]\b/g,
    replace: (_m, digits) => spellOrdinalEs(Number(digits)),
  },
  {
    // Bare unit abbreviations. Valhalla usually spells these out, but the
    // fallback instructions built in navmath.js and any street name containing
    // "Km 9½" do not.
    pattern: /\bkm\b\.?/gi,
    replace: () => 'kilómetros',
  },
  { pattern: /\bmts?\b\.?/gi, replace: () => 'metros' },
  {
    // Compass abbreviations on signage text.
    pattern: /\b(NO|NE|SO|SE)\b/g,
    replace: (match) =>
      ({ NO: 'noroeste', NE: 'noreste', SO: 'suroeste', SE: 'sureste' })[match],
  },
];

const ORDINALS_ES = [
  '',
  'primera',
  'segunda',
  'tercera',
  'cuarta',
  'quinta',
  'sexta',
  'séptima',
  'octava',
  'novena',
  'décima',
];

function spellOrdinalEs(n) {
  return ORDINALS_ES[n] ?? `${n}`;
}

/**
 * Rewrite router Spanish into Nicaraguan Spanish.
 *
 * Exported so the banner can show the same wording the driver hears; a banner
 * that says "glorieta" while the phone says "rotonda" reads as a bug.
 *
 * @param {string} text
 * @param {string} [lang='es'] substitutions apply to Spanish only
 * @returns {string}
 */
export function nicaraguanize(text, lang = 'es') {
  if (typeof text !== 'string' || !text) return '';
  if (!lang.toLowerCase().startsWith('es')) return text;
  let out = text;
  for (const rule of NICARAGUAN_PHRASING) {
    out = out.replace(rule.pattern, rule.replace);
  }
  return out.replace(/\s+/g, ' ').trim();
}

/** @returns {boolean} whether this browser can speak at all. */
export function isSpeechSupported() {
  return typeof globalThis !== 'undefined' && 'speechSynthesis' in globalThis;
}

/**
 * Turn spoken guidance on or off; the choice survives a reload.
 *
 * @param {boolean} enabled
 */
export function setVoiceEnabled(enabled) {
  state.enabled = Boolean(enabled);
  try {
    globalThis.localStorage?.setItem(STORAGE_KEY, state.enabled ? '1' : '0');
  } catch {
    // Private mode or blocked storage: the setting simply does not persist.
  }
  if (!state.enabled) cancelSpeech();
}

/** @returns {boolean} */
export function isVoiceEnabled() {
  return state.enabled;
}

/**
 * Set the guidance language.
 *
 * @param {string} lang BCP-47 tag, e.g. `es-NI` or `en-US`
 */
export function setLanguage(lang) {
  if (typeof lang === 'string' && lang.length >= 2) state.lang = lang;
}

/** @returns {string} */
export function getLanguage() {
  return state.lang;
}

/**
 * Unlock speech and start loading the voice list. **Call this from a tap.**
 *
 * Chrome for Android will not speak until the page has seen a user gesture, and
 * it fails silently — the first "girá a la derecha" of the drive just never
 * happens. Speaking one inaudible utterance inside the "Empezar" handler is the
 * documented way around it.
 *
 * @returns {Promise<SpeechSynthesisVoice[]>} the voices, possibly empty
 */
export function primeVoices() {
  if (!isSpeechSupported()) return Promise.resolve([]);
  const synth = globalThis.speechSynthesis;
  if (!state.primed) {
    state.primed = true;
    try {
      // A space, not an empty string: some engines discard empty utterances
      // without counting them as the unlocking gesture.
      const warmup = new globalThis.SpeechSynthesisUtterance(' ');
      warmup.volume = 0;
      warmup.lang = state.lang;
      synth.speak(warmup);
    } catch {
      state.primed = false;
    }
  }
  return loadVoices();
}

/**
 * Say something.
 *
 * @param {string} text
 * @param {{lang?: string, priority?: 'normal'|'urgent'}} [options]
 *   `urgent` clears anything queued and interrupts what is being said — use it
 *   for the last prompt before a maneuver and for "Recalculando".
 * @returns {void}
 */
export function speak(text, options = {}) {
  if (!state.enabled || !isSpeechSupported()) return;
  const lang = options.lang ?? state.lang;
  const spoken = nicaraguanize(String(text ?? ''), lang);
  if (!spoken) return;

  const item = { text: spoken, lang, priority: options.priority ?? 'normal', queuedAt: Date.now() };

  if (item.priority === 'urgent') {
    state.queue = state.queue.filter((queued) => queued.priority === 'urgent');
    state.queue.push(item);
    hardStop();
    state.speaking = false;
    pump();
    return;
  }

  // Never let a backlog build: if two prompts are already waiting, the oldest
  // is certainly stale by the time it would be spoken.
  state.queue.push(item);
  if (state.queue.length > 3) state.queue.splice(0, state.queue.length - 3);
  pump();
}

/** Stop talking and drop everything queued. */
export function cancelSpeech() {
  state.queue.length = 0;
  hardStop();
  state.speaking = false;
}

function hardStop() {
  try {
    globalThis.speechSynthesis.cancel();
  } catch {
    // Nothing to cancel.
  }
  if (state.watchdog) {
    clearTimeout(state.watchdog);
    state.watchdog = 0;
  }
}

function pump() {
  if (state.speaking || state.queue.length === 0) return;
  const synth = globalThis.speechSynthesis;

  let item = state.queue.shift();
  while (item && Date.now() - item.queuedAt > STALE_MS) item = state.queue.shift();
  if (!item) return;

  const utterance = new globalThis.SpeechSynthesisUtterance(item.text);
  // Chrome for Android ignores the selected voice's language unless `lang` is
  // set explicitly, and then falls back to the system locale — which on a phone
  // bought in Nicaragua is usually, but not always, Spanish.
  utterance.lang = item.lang;
  const voice = voiceFor(item.lang);
  if (voice) utterance.voice = voice;
  utterance.rate = 1.05; // marginally brisk: prompts are short and time-critical
  utterance.pitch = 1;
  utterance.volume = 1;

  const finish = () => {
    if (state.watchdog) {
      clearTimeout(state.watchdog);
      state.watchdog = 0;
    }
    state.speaking = false;
    pump();
  };
  utterance.onend = finish;
  utterance.onerror = finish;

  state.speaking = true;
  try {
    // Android pauses the synth when the screen blanks and does not always
    // resume it; a resume() before every utterance is cheap insurance.
    synth.resume();
    synth.speak(utterance);
  } catch {
    finish();
    return;
  }

  // `onend` is documented but does not always fire on Android — a dropped event
  // would wedge the queue for the rest of the drive, so the queue advances on a
  // timer sized to the utterance whether or not the event arrives.
  const estimatedMs = (item.text.length / CHARS_PER_SECOND) * 1000 + 2500;
  state.watchdog = setTimeout(finish, Math.min(20000, estimatedMs));
}

function voiceFor(lang) {
  const key = lang.toLowerCase().startsWith('en') ? 'en' : 'es';
  if (state.chosen[key]) return state.chosen[key];
  if (!state.voicesResolved) {
    // Fire and forget: the list will be there for the next prompt, and this one
    // still speaks using the utterance's `lang`.
    loadVoices();
    return null;
  }
  state.chosen[key] = pickVoice(state.voices, key === 'en' ? PREFERRED_EN : PREFERRED_ES);
  return state.chosen[key];
}

/**
 * Choose the closest available voice to our preference order.
 *
 * Latin American Spanish is preferred over peninsular: Android's Google TTS
 * ships es-US and es-MX, and es-ES reads as foreign here. Note that even when a
 * voice is selected, Android silently substitutes another Spanish region if the
 * requested voice pack is not installed — so never assume the voice you asked
 * for is the voice that speaks.
 *
 * @param {SpeechSynthesisVoice[]} voices
 * @param {string[]} preferred lowercase BCP-47 prefixes, best first
 * @returns {SpeechSynthesisVoice|null}
 */
export function pickVoice(voices, preferred) {
  if (!Array.isArray(voices) || voices.length === 0) return null;
  const normalized = voices.map((voice) => ({
    voice,
    tag: String(voice.lang ?? '').replace('_', '-').toLowerCase(),
  }));
  for (const wanted of preferred) {
    const exact = normalized.find((entry) => entry.tag === wanted);
    if (exact) return exact.voice;
    const prefixed = normalized.find((entry) => entry.tag.startsWith(`${wanted}-`));
    if (prefixed) return prefixed.voice;
  }
  return null;
}

let voicePromise = null;

/**
 * Resolve the voice list, tolerating an engine that never fires `voiceschanged`.
 *
 * @param {{maxTries?: number, intervalMs?: number}} [options]
 * @returns {Promise<SpeechSynthesisVoice[]>}
 */
export function loadVoices(options = {}) {
  if (voicePromise) return voicePromise;
  if (!isSpeechSupported()) return Promise.resolve([]);
  const { maxTries = 20, intervalMs = 150 } = options;
  const synth = globalThis.speechSynthesis;

  voicePromise = new Promise((resolve) => {
    let tries = 0;
    let settled = false;

    const settle = (voices) => {
      if (settled) return;
      settled = true;
      state.voices = voices;
      state.voicesResolved = true;
      state.chosen.es = pickVoice(voices, PREFERRED_ES);
      state.chosen.en = pickVoice(voices, PREFERRED_EN);
      try {
        synth.removeEventListener('voiceschanged', onChanged);
      } catch {
        // Older engines expose only the onvoiceschanged property.
      }
      resolve(voices);
    };

    const onChanged = () => {
      const voices = synth.getVoices();
      if (voices.length) settle(voices);
    };

    const poll = () => {
      const voices = synth.getVoices();
      if (voices.length) return settle(voices);
      tries += 1;
      // Give up rather than hang: with no list at all the utterance's `lang`
      // still gets us the system's default Spanish on most Android builds.
      if (tries >= maxTries) return settle([]);
      setTimeout(poll, intervalMs);
    };

    try {
      synth.addEventListener('voiceschanged', onChanged);
    } catch {
      synth.onvoiceschanged = onChanged;
    }
    poll();
  });

  return voicePromise;
}

function loadEnabled() {
  try {
    const stored = globalThis.localStorage?.getItem(STORAGE_KEY);
    if (stored === '0') return false;
  } catch {
    // Storage blocked: default to on, which is what a driver expects.
  }
  return true;
}
