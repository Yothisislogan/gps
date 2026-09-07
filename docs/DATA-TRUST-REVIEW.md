# Data trust and usability review

Reviewed 2026-09-06 against the NicaNav reliability branch. This is a source-code
review, not evidence that production imports have run successfully. The existing
[UX brief](UX-BRIEF.md) covers home, search, place cards, navigation and offline UI.

## Product standard

A user should find the correct destination, understand material uncertainty and
reach an accessible entrance. Measure completed journeys and wrong destinations,
not just record counts, map rendering or successful API requests.

Start with a launch-area audit: sample missing businesses, duplicates, incorrect
entrances, dead contacts and outdated hours. Add another source only where it
measurably fills a gap. Overture already combines multiple providers; another
large feed can primarily add overlap and moderation work.

## Source freshness

| Source | Current evidence | Required improvement |
| --- | --- | --- |
| OSM / Geofabrik | Most extracts update daily; nightly import configured. No deployed snapshot verified. | Preserve the source timestamp separately from import/build time. |
| Overture Places | Official current release is 2026-08-19.0, matching the importer fallback. | Check daily for a new release; avoid re-importing unchanged data. Record the selected release and successful import. |
| Local surveys and seeds | Curated files exist; gazetteer seed includes a coordinate marked for verification. | Keep evidence, review dates, unresolved locations and re-verification tasks. |
| Reports and closures | Reporting and expiration mechanisms exist. | Describe coverage and review time honestly; do not imply comprehensive live conditions. |

A fresh release does not mean every business was recently checked. A fresh tile
mtime does not establish source freshness. Track source version, import time,
pipeline result and human verification time separately. Unknown must remain
unknown; failed checks must not manufacture a new success timestamp.

Overture plans to remove `categories` in September 2026. The importer anticipates
`basic_category` and `taxonomy`; validate real release compatibility when available.
The proposed next release is September 23, subject to upstream changes.

## Engineering findings and acceptance criteria

### P0: Stop publishing from failed dependencies

The nightly script currently continues after failed steps. QA occurs after
publication. Individual atomic renames do not make tiles, PostGIS, search and the
routing service one atomic release. Its blanket claim that yesterday's data is
still served after any failure is incorrect.

Immediate mitigation: stop on failure, identify the failed stage and preserve the
last successful run record. Explicitly report partial publication as possible.
A skipped build must not count as a complete fresh deployment.

Full solution: build a candidate generation, run data and route checks against
candidate services, then coordinate publication across services. Support rollback
of the whole generation. Test failure at every publication boundary using real
PostGIS, Meilisearch and Valhalla. This remains a separate integration project;
fail-fast alone does not solve coordinated publication.

### P0: Make freshness observable

Persist source metadata and pipeline run state, expose it to operators, and keep
source/import timestamps distinct. Corrupt or absent metadata must be reported as
unknown. Keep deployment diagnostics compatible with existing health consumers.
Record metadata only after successful work; indicate failed or interrupted runs.
Acceptance: a failed import, missing metadata and partial run cannot look like a
successful full refresh.

### P1: Make moderation decisions effective

The merge endpoint records a queue decision and displays "Unidos", but the reviewed
pipeline has no reader applying saved decisions to the published POIs. Change the
confirmation to say that the decision is saved until publication actually occurs.
Then implement durable merge/separate overrides, preserving POI identities and
references. Verify that accepted changes affect exports/search and survive both
nightly and Overture refreshes. A separate decision must prevent later auto-merges.

### P1: Revisit aging verification

The loader indefinitely preserves verified records' status and nonempty contact
fields. This protects local edits but can keep stale business details forever.
Retain human edits, capture conflicting upstream evidence, and queue re-review
based on field age and significance. Do not blindly let imports overwrite surveys
or assume a place disappearing from one feed means it has closed.
Acceptance: old verification cannot silently suppress a new closure signal; local
corrections survive refresh; changed contacts can be reconciled with evidence.

### P1: Refresh Overture on release availability

The fifth-of-month cron can lag mid-month upstream releases by weeks. Check daily,
validate release identifiers, skip a proven unchanged successful extract, and retry
failed imports. Include survey input consistently in both refresh paths. Preserve
last good data on empty or failed extraction. Do not describe full re-conflation
as incremental processing.

## Additional user journeys

1. **Arrival:** store the accessible entrance separately from the building point;
   show a verified landmark or entrance note. Test actual door-finding.
2. **Uncertainty:** attach "Entrance unconfirmed" or hours verification date to the
   affected action, with call-to-confirm and adjust-pin options.
3. **Recovery:** preserve failed search input; offer landmarks, wider search or pin
   placement. Allow manual origin selection when GPS is unavailable.
4. **Corrections:** make reporting short and distinguish pending, accepted and
   published states. Never imply a submitted report immediately changes routing.
5. **Field evaluation:** test a locally described address through arrival with
   unfamiliar users, ordinary phones and weak connectivity. Record completion,
   wrong destinations, recoverability and time to identify the entrance.

## Delivery order

1. Observable source freshness and honest pipeline state.
2. Fail-fast dependency handling, followed by coordinated candidate publication.
3. Effective moderation and aging-record reconciliation.
4. Arrival and recovery flows, evaluated in the launch area.

Implementation status belongs in the improvement log and commit descriptions;
this review is the baseline, not a claim these items are all complete.

## Primary references

- [Geofabrik update policy](https://www.geofabrik.de/data/download.html)
- [Nicaragua extract](https://download.geofabrik.de/central-america/nicaragua.html)
- [Overture releases and retention](https://docs.overturemaps.org/release-calendar/)
- [Overture Places sources and category migration](https://docs.overturemaps.org/guides/places/)
