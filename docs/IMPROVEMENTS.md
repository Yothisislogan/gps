# NicaNav improvement priorities

Review baseline: `ea4f3c95c49a45f776dd971c46141b35aa8fe5e3` on
`claude/nicanav-build-plan-e43t5t`, reviewed 6 September 2026.

The project has substantial parsing, routing and data-pipeline code. Its main
weakness is the gap between isolated algorithms and the complete journey a
driver experiences. The first implementation focuses on failures visible in
that journey, along with the deployment and moderation paths that support it.

## First implementation

| Priority | Finding | Change | Regression coverage |
|---|---|---|---|
| P0 | Displayed alternative routes had no selection handler; route labels contained an unresolved `{n}` placeholder. | Route choice now changes the summary, map and route handed to navigation; the original remains selectable. Added the existing unpaved-road preference to the route sheet. | `tests/js/directions.test.mjs` clicks the actual renderer's buttons and checks the navigation session. |
| P0 | A delayed reroute or wake-lock request could write into a stopped/replacement session. Stopping during a reroute could dereference `null`. | Each callback belongs to one session; ending a trip aborts its reroute, releases locks and cancels speech. Released locks are reacquired on visibility changes. Old or inaccurate GPS fixes are ignored, and denied permission stops navigation. | `tests/js/navigation.test.mjs` exercises cancellation, replacement, late fixes, permissions and lock release. |
| P0 | Reroutes silently dropped the selected road preferences. | Route options travel into guidance, reroutes and saved session state. | Navigation tests inspect the actual reroute request. |
| P0 | Request deadlines ended after headers; a stalled body could hang indefinitely. Body read failures became misleading JSON errors. | Timeout and caller cancellation remain active through body consumption; disconnects are reported as network errors. | `tests/js/api.test.mjs` stalls response bodies and then triggers timeout or cancellation. |
| P0 | The worker cached arbitrary same-origin GETs, including authenticated admin pages. Missing scripts could receive HTML; uncached hanging requests had no effective fallback deadline. | Explicit public-asset handling, admin/auth bypass, no-store/private protection, navigation-only HTML fallback, bounded fallback, and removal of old broad caches. Cache one shell instead of an entry per shared pin. | `tests/js/sw.test.mjs` executes the worker's fetch handler under offline, stalled, private and server-error responses. |
| P0 | A failed first database connection permanently disabled database features. Missing data and outages were conflated; failed closure lookups looked like zero closures. | Bounded, throttled reconnection with serialized attempts; failed pools are closed. Database-only requests return 503 on outage. Routes disclose `closures_status: checked / unavailable / skipped`; the UI warns when checks are unavailable. | Database lifecycle tests and API/UI closure-outage tests. |
| P0 | nginx required an htpasswd file that Compose never mounted; runtime config did not read `infra/.env`. | `make up` now prepares public config and hashed admin credentials. A read-only directory mount permits atomic credential replacement. Generated files are excluded from git. | Generated hashes are verified with OpenSSL; invalid credentials cannot replace a working file. |
| P0 | Browsers could submit cross-site moderation forms using saved Basic credentials; forged forwarded headers could vary the API rate-limit identity. | Admin writes reject cross-site origins, admin responses use no-store, nginx replaces forwarded IP headers, and fingerprinting uses the resolved client with a random process salt. | API tests verify denied cross-site writes, allowed same-origin writes, response headers and unchanged limits under forged forwarding headers. |
| P1 | Existing CI's curated-data job failed by treating a tuple of highways as a `(results, metadata)` pair. `make check` omitted several CI checks. | One explicit data-validation script handles each loader's real return type and fails on missing data. `make check` includes the JS, generated-artifact and curated-data gates. | Run the actual loaders against all three committed data files. |

## Next priorities and acceptance criteria

These are remaining work, not claims that this branch implements them.

1. **Prove a complete offline restart.** The worker still excludes cross-origin
   libraries, so browser HTTP cache can hide missing dependencies. Package and
   version the map renderer, its worker dependencies, PMTiles, required glyphs
   and sprites together. Validate downloads before replacing an existing
   archive; add cancellation, storage estimates and update age. Acceptance:
   download on a fresh device, close the app, disable connectivity and reopen
   the map successfully. Distinguish saved map viewing from route computation,
   which still requires the server.
2. **Run one real integration environment.** Bring up PostGIS, Meilisearch,
   Valhalla and nginx with a small licensed fixture. Exercise search → place →
   route → alternate → reroute → outage → recovery. Validate byte-range tiles,
   TLS, admin login, and resource use. The current offline tests use service
   fakes; they do not establish the whole stack's deployability or RAM budget.
3. **Make corrections durable from submission to publication.** Trace each
   moderation decision through the next POI export and search-index rebuild.
   Add a regression proving that an approved field correction survives the
   next import. Show when a decision is published, and when publication fails.
4. **Build a measured landmark-address advantage.** Gather consented,
   field-checked addresses with known entrance coordinates; split them into
   development and held-out evaluation sets. Measure resolution rate and error
   distance per city. Add a movable confirmation pin and distinguish an
   estimated address from a surveyed entrance. Do not display parser confidence
   as though it were measured accuracy.
5. **Improve trips with verified local information.** Prioritize one-ways,
   retornos, road surfaces and business entrances along the Managua–Masaya–
   Granada corridors. Feed road edits upstream to OSM and add golden routes.
   Expose the existing along-route POI search for fuel and food, with verified
   dates and measured detour time. Avoid claiming route superiority before
   field comparison.

## Validation and limits

Validation on this branch passed all **930 Python tests** and **54 JavaScript
tests**, lint, generated styles/sprite coverage and the three curated-data
loaders. The new JavaScript regressions were also run against the original
development commit and reproduced failures there.

`make check` runs lint, Python tests, executable JavaScript tests, generated
asset checks and curated-data validation. JS UI tests use an event-capable
DOM stand-in; GPS, wake locks and services are controlled fakes. They verify
logic and race conditions, not rendering or actual device behavior.

The review environment could not open its local preview in the cloud browser
and has no Docker runtime. Browser rendering, real service integration,
airplane-mode restart and road testing remain required before a public launch.

The connection recovery follows psycopg's documented behavior: a timed-out
initial pool wait closes that pool, and background retries have a finite budget.
See [psycopg pool API](https://www.psycopg.org/psycopg3/docs/api/pool.html) and
[connection-pool behavior](https://www.psycopg.org/psycopg3/docs/advanced/pool.html).
