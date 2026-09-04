"""Text normalisation shared by the geocoder, the conflator and the search indexer.

Everything here is deliberately dependency-free (stdlib only) so it can run
inside the API container, inside a pipeline job, or in a bare test run.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

__all__ = [
    "GENERIC_PLACE_PREFIXES",
    "collapse_ws",
    "name_key",
    "normalize",
    "parse_spanish_number",
    "strip_accents",
    "strip_generic_prefix",
    "tokenize",
]

# --------------------------------------------------------------------------- #
# Basic normalisation
# --------------------------------------------------------------------------- #

_WS_RE = re.compile(r"\s+")
# Keep letters, digits and the separators that carry meaning in addresses.
_PUNCT_RE = re.compile(r"[^\w\s/.,½¼¾-]", re.UNICODE)


def strip_accents(value: str) -> str:
    """Drop combining marks: ``"Rotonda Güegüense"`` -> ``"Rotonda Gueguense"``.

    Nicaraguan users type without accents about half the time, so every lookup
    key in this project is accent-folded.  ``ñ`` is folded to ``n`` on purpose:
    ``"Nandaime"`` and ``"Ñandaime"`` must collide.
    """
    decomposed = unicodedata.normalize("NFD", value)
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def collapse_ws(value: str) -> str:
    """Squeeze runs of whitespace and trim."""
    return _WS_RE.sub(" ", value).strip()


def normalize(value: str, *, keep_punct: bool = False) -> str:
    """Lowercase, accent-fold, de-punctuate and collapse whitespace.

    ``keep_punct`` preserves ``, . / -`` which the address grammar uses as
    segment separators.
    """
    folded = strip_accents(value).lower()
    if not keep_punct:
        folded = _PUNCT_RE.sub(" ", folded)
        folded = folded.replace(",", " ").replace(".", " ").replace("/", " ").replace("-", " ")
    else:
        folded = _PUNCT_RE.sub(" ", folded)
    return collapse_ws(folded)


def tokenize(value: str) -> list[str]:
    """Normalised whitespace tokens."""
    normalized = normalize(value)
    return normalized.split() if normalized else []


# --------------------------------------------------------------------------- #
# Business-name normalisation (conflation + search)
# --------------------------------------------------------------------------- #

#: Generic leading words that carry no identity in a Nicaraguan business name.
#: ``"Restaurante El Zaguán"`` and ``"El Zaguán"`` are the same place, so the
#: conflator compares the stripped forms.  Order matters: longest first.
GENERIC_PLACE_PREFIXES: tuple[str, ...] = (
    "bar y restaurante",
    "restaurante y bar",
    "hotel y restaurante",
    "centro comercial",
    "supermercado",
    "restaurante",
    "restaurant",
    "cafeteria",
    "cafetin",
    "comedor",
    "fritanga",
    "pulperia",
    "panaderia",
    "farmacia",
    "ferreteria",
    "gasolinera",
    "distribuidora",
    "taller",
    "clinica",
    "hostal",
    "hotel",
    "bar",
    "cafe",
    "pizzeria",
    "heladeria",
    "reposteria",
    "tienda",
    "libreria",
    "el",
    "la",
    "los",
    "las",
)

#: Trailing corporate noise.
_GENERIC_SUFFIXES: tuple[str, ...] = ("s a", "sa de cv", "s a de c v", "cia ltda", "ltda")


def strip_generic_prefix(name: str) -> str:
    """Strip category words that Nicaraguan listings prepend inconsistently.

    Returns the normalised remainder, or the normalised original when stripping
    would leave nothing (``"Fritanga"`` on its own is the whole name).
    """
    current = normalize(name)
    if not current:
        return ""
    changed = True
    while changed:
        changed = False
        for prefix in GENERIC_PLACE_PREFIXES:
            candidate = f"{prefix} "
            if current.startswith(candidate):
                remainder = current[len(candidate) :].strip()
                if remainder:
                    current = remainder
                    changed = True
                    break
    for suffix in _GENERIC_SUFFIXES:
        if current.endswith(f" {suffix}"):
            current = current[: -(len(suffix) + 1)].strip()
    return current or normalize(name)


#: Every token that appears in a generic prefix, plus Spanish stopwords.  These
#: are dropped from blocking keys wherever they occur, not only at the front:
#: listings write "Restaurante El Zaguán", "El Zaguán Restaurante" and
#: "El Zaguán" for the same door.
_GENERIC_TOKENS: frozenset[str] = frozenset(
    token for phrase in (*GENERIC_PLACE_PREFIXES, *_GENERIC_SUFFIXES) for token in phrase.split()
) | frozenset({"y", "de", "del", "las", "los", "el", "la"})


def name_key(name: str) -> str:
    """Blocking key for conflation: generic words dropped, tokens sorted.

    ``"Bar y Restaurante El Zaguán"`` and ``"El Zaguan Restaurante"`` share a key.
    Falls back to the plain normalised name when a listing is *only* generic
    words (``"Fritanga"``), which must still block against other fritangas.
    """
    tokens = sorted({t for t in strip_generic_prefix(name).split() if t not in _GENERIC_TOKENS})
    return " ".join(tokens) or normalize(name)


# --------------------------------------------------------------------------- #
# Spanish number parsing (used by the relative-address and km-post geocoders)
# --------------------------------------------------------------------------- #

_UNITS: dict[str, float] = {
    "cero": 0,
    "un": 1,
    "uno": 1,
    "una": 1,
    "dos": 2,
    "tres": 3,
    "cuatro": 4,
    "cinco": 5,
    "seis": 6,
    "siete": 7,
    "ocho": 8,
    "nueve": 9,
    "diez": 10,
    "once": 11,
    "doce": 12,
    "trece": 13,
    "catorce": 14,
    "quince": 15,
    "dieciseis": 16,
    "diecisiete": 17,
    "dieciocho": 18,
    "diecinueve": 19,
    "veinte": 20,
    "veintiun": 21,
    "veintiuno": 21,
    "veintiuna": 21,
    "veintidos": 22,
    "veintitres": 23,
    "veinticuatro": 24,
    "veinticinco": 25,
    "veintiseis": 26,
    "veintisiete": 27,
    "veintiocho": 28,
    "veintinueve": 29,
}

_TENS: dict[str, float] = {
    "treinta": 30,
    "cuarenta": 40,
    "cincuenta": 50,
    "sesenta": 60,
    "setenta": 70,
    "ochenta": 80,
    "noventa": 90,
}

_HUNDREDS: dict[str, float] = {
    "cien": 100,
    "ciento": 100,
    "doscientos": 200,
    "doscientas": 200,
    "trescientos": 300,
    "trescientas": 300,
    "cuatrocientos": 400,
    "cuatrocientas": 400,
    "quinientos": 500,
    "quinientas": 500,
    "seiscientos": 600,
    "seiscientas": 600,
    "setecientos": 700,
    "setecientas": 700,
    "ochocientos": 800,
    "ochocientas": 800,
    "novecientos": 900,
    "novecientas": 900,
    "mil": 1000,
}

#: Fraction words.  ``"media cuadra"`` = half a block, ``"cuadra y media"`` = 1.5.
_FRACTIONS: dict[str, float] = {
    "medio": 0.5,
    "media": 0.5,
    "cuarto": 0.25,
    "tercio": 1 / 3,
    "½": 0.5,
    "¼": 0.25,
    "¾": 0.75,
    "1/2": 0.5,
    "1/4": 0.25,
    "3/4": 0.75,
}

_NUMERIC_RE = re.compile(r"^\d+(?:[.,]\d+)?$")
_MIXED_FRACTION_RE = re.compile(r"^(\d+)\s*(?:y\s*)?(½|¼|¾|1/2|1/4|3/4)$")


def parse_spanish_number(text: str) -> float | None:
    """Parse a Nicaraguan quantity into a float, or ``None`` if it isn't one.

    Handles digits (``"2"``, ``"2.5"``, ``"12,5"``), mixed fractions
    (``"9½"``, ``"1 1/2"``), bare fractions (``"media"``), plain words
    (``"dos"``, ``"veinticinco"``) and compounds (``"treinta y cinco"``,
    ``"dos y media"``, ``"ciento cincuenta"``).

    Returns ``None`` for anything it does not recognise so callers can treat a
    failed parse as "this token was not a number" rather than as zero.
    """
    raw = collapse_ws(strip_accents(str(text)).lower())
    if not raw:
        return None

    # "9½" / "1 1/2" — glued or spaced mixed fractions.
    glued = _MIXED_FRACTION_RE.match(raw.replace(" ", "")) or _MIXED_FRACTION_RE.match(raw)
    if glued:
        return float(glued.group(1)) + _FRACTIONS[glued.group(2)]

    if _NUMERIC_RE.match(raw):
        return float(raw.replace(",", "."))

    if raw in _FRACTIONS:
        return _FRACTIONS[raw]

    tokens = [t for t in raw.replace("½", " ½").split() if t]
    return _accumulate_words(tokens)


def _accumulate_words(tokens: Iterable[str]) -> float | None:
    total = 0.0
    current = 0.0
    matched = False
    pending_y = False

    for token in tokens:
        if token == "y":
            pending_y = True
            continue
        if _NUMERIC_RE.match(token):
            current += float(token.replace(",", "."))
            matched = True
        elif token in _FRACTIONS:
            current += _FRACTIONS[token]
            matched = True
        elif token in _UNITS:
            current += _UNITS[token]
            matched = True
        elif token in _TENS:
            current += _TENS[token]
            matched = True
        elif token in _HUNDREDS:
            value = _HUNDREDS[token]
            if value == 1000:
                total += (current or 1) * 1000
                current = 0.0
            else:
                current += value
            matched = True
        else:
            # Unknown token ends the number: "dos cuadras" stops at "cuadras".
            break
        pending_y = False

    if pending_y and not matched:
        return None
    result = total + current
    return result if matched else None
