"""Nicaraguan search synonyms and stop words.

Search only feels good when it speaks the way people do.  A Nicaraguan looking
for fuel types "bomba", not "gasolinera"; tyre repair is "vulca"; a convenience
shop is a "pulpería" and never a "convenience store".  These mappings are as
much of the product as the map itself.

Meilisearch synonyms are directional: ``{"bomba": ["gasolinera"]}`` means a
search for *bomba* also matches documents containing *gasolinera*.  Every pair
here is registered both ways unless the reverse would be wrong.
"""

from __future__ import annotations

__all__ = ["SPANISH_STOP_WORDS", "SYNONYM_GROUPS", "build_synonyms"]

#: Words that mean the same thing to a searcher.  Every member of a group is
#: expanded to every other member.
SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    # Fuel. "bomba" is what a Nicaraguan actually says.
    ("gasolinera", "bomba", "gas", "combustible", "estacion de servicio", "puma", "uno"),
    # Cash. Bank brands double as landmarks in addresses.
    ("cajero", "atm", "cajero automatico", "efectivo"),
    ("banco", "bac", "lafise", "banpro", "ficohsa", "avanz"),
    ("casa de cambio", "cambio", "coyote", "dolares"),
    ("remesas", "western union", "moneygram", "envios"),
    # Tyres: the single most useful category on a Nicaraguan road.
    ("vulcanizacion", "vulca", "llantera", "llantas", "ponchera", "reparacion de llantas"),
    ("taller mecanico", "taller", "mecanico", "automotriz"),
    ("lavado de autos", "car wash", "lavacar", "lavado"),
    # Food.
    ("restaurante", "restaurant", "comida", "almuerzo", "cena"),
    ("fritanga", "fritanguera", "asados", "comida corriente"),
    ("comedor", "buffet", "comida casera", "almuerzo corriente"),
    ("cafetin", "cafeteria", "cafe", "coffee"),
    ("comida rapida", "fast food", "hamburguesas", "pollo frito"),
    ("heladeria", "helados", "eskimo", "sorbete"),
    ("panaderia", "pan", "reposteria", "pasteleria"),
    # Shops.
    ("pulperia", "miscelanea", "venta", "abarrotes"),
    ("supermercado", "super", "la colonia", "pali", "maxi pali", "walmart"),
    ("mercado", "mercadito", "huembes", "oriental", "mayoreo"),
    ("ferreteria", "ferreteria", "materiales", "construccion"),
    ("farmacia", "botica", "medicinas", "farmacias"),
    # Lodging and places.
    ("hotel", "hostal", "hospedaje", "alojamiento", "posada"),
    ("mirador", "vista", "punto panoramico"),
    ("laguna", "lago", "playa"),
    # Health, civic.
    ("hospital", "clinica", "centro de salud", "emergencias"),
    ("policia", "estacion de policia", "comisaria"),
    ("terminal de buses", "terminal", "buses", "parada de buses", "expresos"),
    # Landmarks, because addresses are built from them.
    ("rotonda", "redondel", "glorieta"),
    ("parqueo", "estacionamiento", "parking"),
    ("gimnasio", "gym"),
    ("universidad", "uni", "uca", "unan", "uam", "upoli"),
)

#: Words with no discriminating power in Nicaraguan place names.  Stop words are
#: kept deliberately short: dropping "el"/"la" is safe, but dropping "san" would
#: ruin half the country's toponyms.
SPANISH_STOP_WORDS: tuple[str, ...] = (
    "el",
    "la",
    "los",
    "las",
    "de",
    "del",
    "y",
    "en",
    "un",
    "una",
    "para",
    "por",
    "al",
)


def build_synonyms() -> dict[str, list[str]]:
    """Expand :data:`SYNONYM_GROUPS` into Meilisearch's directional mapping."""
    synonyms: dict[str, set[str]] = {}
    for group in SYNONYM_GROUPS:
        for term in group:
            synonyms.setdefault(term, set()).update(other for other in group if other != term)
    return {term: sorted(values) for term, values in synonyms.items() if values}
