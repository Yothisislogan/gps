# Licences, attribution and the rules

**Not legal advice.** This is what the licences say and how this project is
structured to respect them. If nicanav is ever operated commercially, get a
lawyer to read the ODbL question in §2 specifically.

## 1. The code

Apache-2.0 (see `LICENSE`). That covers this repository's source. It does **not**
cover the data the code processes, which is the part that matters.

## 2. OpenStreetMap — ODbL 1.0

The road network, most POIs and the base map come from OSM, under the
[Open Database License](https://opendatacommons.org/licenses/odbl/1-0/).

**Attribution.** "© OpenStreetMap contributors" must be visible wherever the
data is shown. It is in the app shell (`web/index.html`), in the map style's
`attribution` field, and in the POI tile layer's attribution.

**Share-alike** applies to a *derivative database* — a database produced by
modifying or deriving from the OSM database. It does not apply to a *collective
database*, where OSM data sits alongside other data without being merged into
it, nor to a produced work (a rendered map image).

Where nicanav lands on that line is a real question, because it conflates OSM
POIs with Overture ones. The project's answer, in the schema rather than in a
paragraph:

- `poi_source` holds **one row per source contribution**, with that source's
  own licence in `poi_source.license` and its raw payload in `poi_source.raw`.
  Nothing is discarded in the merge.
- `poi` is the published, merged view, and `poi.sources` records exactly which
  source ids it came from.

That keeps every contribution attributable and separable. The plan's stated
position, which this repository follows, is the simple and honest one: **publish
the conflated POI dataset openly under ODbL anyway**, and push road fixes
straight back to OSM. Nobody has to litigate the boundary if you share alike by
default.

## 3. Overture Maps Foundation

Overture's places theme is a bundle of differently-licensed sources, and the
licence travels per record:

| Source | Licence |
|---|---|
| Meta | CDLA-Permissive-2.0 |
| Microsoft | CDLA-Permissive-2.0 |
| Foursquare | Apache-2.0 |
| AllThePlaces | CC0-1.0 |

`pipeline/pois/fetch_overture.py` reads each row's `sources[].license` and
carries it through to `poi_source.license`. That is why the loader does not use
one blanket licence string: a CC0 row and a CDLA row must not be laundered into
the same undifferentiated blob.

Overture's own documentation notes that joining CDLA-licensed data to OSM data
may make the result a derivative database under ODbL. See §2.

## 4. Imagery

- **Mapillary** and **Panoramax**: CC BY-SA. Photos shown on a POI card carry
  the credit and the licence (`poi_photo.credit`, `poi_photo.license`, rendered
  under the image). Deriving OSM edits from this imagery is permitted.
- **Bing, Esri World Imagery, Maxar** (where enabled): permitted as *tracing
  sources in OSM editors* under their arrangements with the OSM Foundation.
  They are not used as basemaps here.
- **EOX Sentinel-2 cloudless**: CC BY-NC-SA — non-commercial only. Not used.

## 5. Wikidata / Wikimedia

Wikidata is CC0. Commons images are per-file, usually CC BY-SA; carry the
per-file credit if any are ever used.

## 6. The hard rule: no Google data, ever

Not Google Places. Not Google imagery. Not "just for reference".

- Google's terms forbid displaying their data on a non-Google map, and forbid
  caching it.
- Google imagery may **not** be traced from in an OSM editor. An edit derived
  from it poisons the OSM database and has to be reverted.

This applies to every part of the project, including a quick check while
surveying. If a place cannot be verified without Google, it stays unverified.

## 7. What must appear in the UI

Present in `web/index.html`, `web/style/nicanav.json` and the POI card:

```
© OpenStreetMap contributors · Overture Maps Foundation
Fotos: Mapillary (CC BY-SA)
```

The map style also carries an `attribution` string on each source, so any other
client rendering these tiles inherits it.

## 8. Contributing data back

Road fixes go upstream to OSM. That is both the licence-clean path and the
useful one — see `CONTRIBUTING.md`.

The curated pieces that have no home in OSM — the "donde fue" gazetteer, the
learned address aliases, the Nicaraguan category taxonomy — are in this
repository under Apache-2.0 as *code-adjacent data*, and the intent is to
publish the conflated POI dataset and the address gazetteer openly. Nobody else
in Nicaragua has them, and a map nobody can build on is a map that dies with its
maintainer.
