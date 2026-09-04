# Valhalla configuration

## `default_speeds.json`

Nicaragua-specific default speeds, in the
[OpenStreetMapSpeeds](https://github.com/OpenStreetMapSpeeds/schema) format that
Valhalla's `mjolnir.default_speeds_config` reads. They apply during **tile
build** (graph enhancement), not per request, so changing them means rebuilding
tiles — or running `valhalla_assign_speeds`, which rewrites speeds on existing
tiles without a full rebuild.

Array index is the road class, highest to lowest:

| 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 |
|---|---|---|---|---|---|---|---|
| motorway | trunk | primary | secondary | tertiary | unclassified | residential | service_other |

The three tables are selected by OSM-derived road density, with hard-coded
thresholds: density ≤ 5 → `rural`, 6–10 → `suburban`, > 10 → `urban`. All three
must be present; the parser dereferences each unconditionally.

### Why these numbers

Nicaragua has no motorways; the fast roads are `trunk`/`primary` — Carretera a
Masaya, Carretera Norte, the Panamericana — which really do run 60–80 km/h
outside the city. Inside Managua the same classes are effectively 30–40 km/h
once traffic, reductores (speed bumps) and rotondas are accounted for, which is
why the urban table drops so sharply. Rotonda speeds are set low across the
board: Valhalla already applies its own roundabout factor, and Managua's
rotondas are genuinely slow.

These are **starting values**, to be tuned against `pipeline/qa/golden_routes.py`
until predicted times match how long the drive actually takes.

### Two traps

1. **A malformed file is silently ignored.** Valhalla logs a warning and
   disables the whole config — routes keep working with the compiled-in
   defaults, so the only symptom is that your tuning did nothing. Validate the
   JSON before a build (`make check-speeds`) and confirm afterwards that a known
   route's duration actually moved.
2. **The container will overwrite this file** if `use_default_speeds_config` is
   left `True`: the entrypoint downloads the upstream table to
   `/custom_files/default_speeds.json` on start. The compose file sets it to
   `False` and `valhalla.json` points at the mounted copy instead.

> `link_exiting`, `link_turning`, `roundabout`, `driveway`, `alley`,
> `parking_aisle` and `drive-through` are written here from the published schema
> shape. **UNVERIFIED against a running build** — confirm on first deploy that
> the config loads (no "unable to parse" warning in the Valhalla log) and that a
> golden route's duration changes when a value changes.
