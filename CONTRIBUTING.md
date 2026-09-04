# Contributing to nicanav

Two very different kinds of contribution matter here, and the second one matters
more.

## Contributing data (the one that matters)

The hardest problems in this project are not in the code. They are that
Managua's one-ways are half-mapped, that the retornos on Carretera a Masaya are
missing so the router sends people a kilometre past their turn, and that half
the businesses in the country exist only as a Facebook page.

**Road fixes go to OpenStreetMap, not into this repository.** That is not
bureaucracy: an edit in OSM reaches every map, every editor and every router,
and comes back into this stack within 24 hours through the nightly build. A
private table would help only us, and would rot.

What to survey, in rough priority order:

| What | Why it matters here | Tool |
|---|---|---|
| One-way direction on every primary and secondary in Managua and Granada | A wrong one-way sends a driver into oncoming traffic on a pista | StreetComplete, Vespucci |
| Retornos on dual carriageways | Without them the router routes past the turn and back | JOSM, Vespucci |
| Rotonda flow (`junction=roundabout`, correct direction) | Wrong flow means "toma la segunda salida" counts the wrong exits | JOSM |
| Turn restrictions at major intersections | Silently produces illegal routes | JOSM + Mapillary imagery |
| `surface` on everything | Drives both the ETA and the dashed-line rendering that warns a driver about dirt | StreetComplete |
| `traffic_calming` (reductores) | Real time cost, and locals want to see them | StreetComplete |
| Km posts as `highway=milestone` + `distance` | Calibrates the km-post geocoder against the ministry's signs | Every Door, JOSM |
| POI names, hours, phones | The place cards | Every Door |
| Dashcam imagery | Lets everything above be verified from a desk afterwards | Mapillary, Panoramax |

Imagery you may trace from in an OSM editor: Bing, Esri World Imagery, Maxar
where enabled, Mapillary. **Never Google.**

Curated data that *does* live in this repository, because OSM has no place for
it:

- `docs/gazetteer_seed.csv` — landmarks people navigate by, including the
  "donde fue" ghosts (the Cine Cabrera, the antigua Pepsi) that no longer exist.
  Adding one is a pull request; getting the coordinate right matters more than
  adding many.
- `docs/carreteras.csv` — the named highways km posts are quoted on, and every
  spelling people actually type.
- `docs/taxonomy.csv` — the category vocabulary.
- `docs/golden_routes.json` — origin/destination pairs with the route a local
  would take. If the router does something a Nicaraguan driver would not,
  **that is a golden route waiting to be written**, and it is the single most
  useful code-adjacent contribution.

## Contributing code

### Ground rules

* Python ≥ 3.11, `from __future__ import annotations`, full type hints.
* Import direction is one-way: `common` ← `pipeline`, `common` ← `api`.
  `pipeline` never imports `api`. The single documented exception is that `api`
  may import the pure `pipeline.geocode` modules.
* Coordinates are `(lat, lon)` in Python and `[lon, lat]` in GeoJSON. Never
  `lng`. Distances are metres (`_m`), durations seconds (`_s`), bearings degrees
  (`_deg`). A swapped pair looks plausible on a map, which is exactly why it
  needs a convention rather than care.
* No network at import time. `logging.getLogger(__name__)`, never `print()` in
  library code.
* Comments explain *why*, especially where Nicaraguan reality drives the code.
  Do not narrate what the code says.
* Anything unverifiable against a live service is marked `# UNVERIFIED:` with
  what to check on first deploy. `docs/RUNBOOK.md` collects them.

### Tests

```bash
make test                              # offline: no network, no database, no Docker
node --test "tests/js/*.test.mjs"      # the navigation geometry
make lint                              # ruff check + format --check
```

Tests never touch the network, a database or Docker. Anything that must is
marked `@pytest.mark.network` or `@pytest.mark.docker` and skipped by default.
A bug fix comes with the regression test that would have caught it.

### Adding things

**A pipeline job.** Put it under `pipeline/<area>/`, give it `argparse` and a
`main()` returning an exit code, publish every file through
`pipeline.common.io.atomic_output` (nginx serves the old file until a complete
one is renamed over it), and add it to `pipeline/nightly.sh` in the right place.

**A POI category.** Add the row to `docs/taxonomy.csv` — the id set is fixed by
`docs/SPEC.md` §7 and must not be renamed, because the style, the sprite sheet
and the search synonyms all key off it. Then run
`python3 scripts/build_sprites.py`, which fails if your category has no glyph.

**A golden route.** Add it to `docs/golden_routes.json` with `must_pass_near`
waypoints encoding the way a local drives it, and a `notes` line saying what the
case protects. A route without a note is a number nobody can maintain.

**A map style change.** Edit `scripts/build_style.py`, not the JSON — the JSON
is generated, and the night variant comes from the same description so the two
cannot drift. `python3 scripts/build_style.py` regenerates both.

**Front-end code.** There is no bundler and no build step, deliberately (see
`docs/PLAN.md` §6.1). ES modules, libraries from a CDN pinned to an exact
version, no framework, no `npm install`. `tests/test_web.py` checks that every
import resolves, every translation key exists and every DOM id the modules bind
to is in the shell.

### Spanish

The product speaks Nicaraguan Spanish. User-facing strings are Spanish first,
English behind a toggle, and the "vos" register is fine where it reads naturally
("Movelo un poco y probá de nuevo"). Identifiers and comments are English.

### Commits and pull requests

Say what changed and why the change is right, not what the diff already shows.
Where a decision is subtle — a threshold, a fallback, a refusal to cache
something — put the reason in a comment next to the code as well, because that
is where the next person will be standing.
