# NicaNav UX implementation brief

Status: proposed UX work; this document does not mean the screens are implemented.
Reviewed against commit `edad5337`, 6 September 2026. The review used interface
code; rendered layouts and real-device behavior remain unverified.

## Product direction

Make the journey clear: **find a place → confirm the destination → choose a
route → drive**. Each stage should have one obvious primary action, preserve
context, and explain uncertainty in ordinary language.

NicaNav's distinctive experience should be understanding local landmark
addresses and helping people reach the correct entrance. Spanish comes first,
with complete English translations. Retain the warm neutral background and
green accent; use consistent icons, typography, spacing and button hierarchy.

## Delivery order

| Phase | Scope | Outcome |
|---|---|---|
| 1 | Starting screen, search sheet, destination confirmation | Users find and confirm the right place without losing the map. |
| 2 | Place cards and route comparison | Users understand the destination and choose a route confidently. |
| 3 | Driving mode and offline management | Guidance stays legible and connection limits are clear. |
| Throughout | Accessibility, error recovery, localization, regression checks | Every phase works with touch, keyboard, enlarged text and slow connections. |

Implement phases as reviewable changes. Preserve the existing request
cancellation, navigation session isolation and server-side moderation behavior.
Keep the current plain JavaScript modules and no-bundler architecture.

## 1. Useful starting screen

**Current issue:** A map and multiple controls offer little guidance. An empty
recent-search history uses a failed-search message before the user has searched.

- Lead with “¿Adónde vamos?” / “Where are we going?”
- Show recent destinations, Home, Work and a short row of nearby essentials.
- Treat Home and Work as optional, locally stored shortcuts. Allow editing and
  removal; do not require an account or collect those locations as analytics.
- With no saved places, show examples such as “De la Rotonda El Güegüense,
  2 cuadras al sur.” Never label a fresh starting screen as a failed search.
- Request location when someone chooses “Mi ubicación” or asks to route from
  their current position. Browsing and manually choosing a starting point must
  remain possible without permission.
- Keep Locate and the primary search action prominent. Move secondary global
  actions into a clearly labeled menu.

**Acceptance:** A first-time visitor can start a search without dismissing an
error or permission prompt. A returning visitor can select a saved destination
in one tap. Missing history, denied location and unavailable storage each have
a usable path forward.

## 2. Search while keeping the map visible

**Current issue:** The results panel covers nearly the entire map, making it
hard to compare similarly named places geographically.

- Use a bottom sheet with compact, half-height and expanded states on phones.
  Use a side panel on wider screens. Provide buttons as well as drag gestures.
- Connect result rows with numbered or clearly matched map pins. Selecting a
  row highlights its pin; selecting a pin identifies its row.
- Display the place name, neighborhood/city, category, distance when known and
  evidence-based verification status. Distinguish distance from travel time.
- Preserve the query, scroll position, selected result and useful map viewport
  when returning from a place card.
- Retain debouncing and cancellation. Handle loading, no results, offline and
  service errors separately; offer retry without erasing the query.
- When the keyboard is open, keep the search input and useful results visible.
  Do not announce every transient keystroke response to screen readers.

**Acceptance:** Users can inspect results and their location on the map
simultaneously. Back returns to the same results. Rapid query changes never
show stale results. Keyboard users can reach and select each result.

## 3. Confirm a local address and entrance

**Current issue:** A numerical confidence score is hard to interpret. Confirming
“here” without an easy way to correct the pin does not resolve a wrong entrance.

- Show the original address and a plain-language interpretation: landmark,
  direction and distance. Distinguish a surveyed entrance from an estimate.
- Replace percentage badges with descriptions such as “Ubicación aproximada”
  / “Approximate location.” Do not equate parser scores with measured accuracy.
- Offer “Confirmar entrada” / “Confirm entrance” and “Ajustar ubicación” /
  “Adjust location.” Support dragging a pin and moving the map under a fixed
  crosshair; also provide keyboard-accessible adjustment controls.
- Preserve the initial point so adjustments can be undone or cancelled.
- Confirm the destination for this trip locally. Offer a separate, explicit
  action to submit the correction for other users; it enters moderation.
- Make successful submission, pending review and failed submission distinct.
  Do not silently publish a correction or retry a write without user intent.

**Acceptance:** A user can correct a pin before routing and cancel without
changing the destination. The router receives the confirmed coordinates.
Public corrections remain pending until reviewed; a failed submission does not
prevent using the corrected destination for the current trip.

## 4. Place cards with a clear hierarchy

**Current issue:** Directions, phone, WhatsApp, social links, website, sharing
and reporting can all appear as competing actions.

- Make Directions the primary action; keep Call and WhatsApp secondary.
- Put website, social profiles, sharing and reporting under “More,” with
  text labels. Show only actions supported by available data.
- Present name, category, entrance/address, hours and verification date in
  that order of decision value. Photos should not delay primary actions.
- Distinguish “closed now” from “permanently closed.” Unknown hours must not
  imply that a business is open. Avoid guessing WhatsApp availability from an
  ordinary phone number.
- Keep Directions accessible while the user scrolls through details.

**Acceptance:** Directions is visually dominant. Missing photos or hours do
not block the card. Contact links reflect the stored contact type. The card
remains readable with large text and returns to the previous search state.

## 5. Explain route differences

**Current issue:** Generic route numbers give little reason to choose one
option, even now that selection is functional.

- Lead with duration and arrival time, then distance and useful differences.
  Examples: “4 minutes longer · 2 km shorter” or a known unpaved segment.
- Compute comparisons from returned route data. Do not invent traffic,
  safety, road-surface coverage or “best route” claims.
- Synchronize selected route styling, summary, maneuver list and Start action.
- Keep the avoid-unpaved option visible and explain that it is a preference;
  some destinations may require an unpaved approach.
- Preserve the warning when closures could not be checked. Show unknown
  conditions explicitly rather than presenting missing data as reassurance.

**Acceptance:** A user can understand the measurable difference between
alternatives without opening each turn list. Selection updates every route
representation, and guidance starts on that exact route with those preferences.

## 6. Focused driving mode

- Make the next maneuver and distance to it the strongest elements, followed
  by arrival time and remaining trip duration.
- Hide browsing categories and general settings during guidance. Keep voice,
  recenter, stop and a limited trip-actions menu accessible.
- After manual map movement, show a clear Recenter control. Resume following
  only when selected; do not repeatedly pull the map away from the user.
- Use persistent, understandable states for weak GPS, rerouting and a lost
  connection. Avoid repeated toasts or stale turn announcements.
- Make stop/restart behavior explicit and preserve existing callback isolation.
- Support landscape, night mode, display cutouts and enlarged text. Do not
  require typing or fine gestures to operate core trip controls.

**Acceptance:** Next-turn information is readable at a glance. Recenter and
voice controls are discoverable. Ending a trip cancels its pending guidance;
background/foreground transitions do not revive an old session.

## 7. Honest offline management

- Show the covered area on a small map plus a text description, download size,
  available storage when known, and last successful update date.
- Offer download, progress, cancellation, update and delete actions. Ask before
  replacing or deleting a saved map; retain the working archive if an update
  fails. Never download a large archive automatically on metered data.
- Label capabilities precisely: saved map viewing is different from search or
  calculating a new route, which currently need the server.
- Show a visible offline status with useful available actions.
- Bundle/cache required renderer dependencies and validate archive integrity
  before claiming offline readiness. A warm browser cache is not proof.

**Acceptance:** On a fresh supported device, download the region, close the
app, disable connectivity and reopen it. Verify the map actually renders.
Cancelled, interrupted and invalid updates leave the previous archive usable.

## 8. Accessibility and visual consistency

- Expand currently 36px controls to at least 48×48 CSS-pixel interactive areas,
  with sufficient spacing; icons may be smaller inside those targets.
- Add visible focus indicators and meaningful text labels. Do not communicate
  selection, verification, warnings or route alternatives through color alone.
- Restore focus to the opening control when closing a sheet. If a panel becomes
  modal, manage focus and background interaction accordingly; do not trap focus
  in a nonmodal panel that leaves the map usable.
- Provide tap/keyboard alternatives for dragging, pin adjustment and sheet
  resizing. Announce relevant asynchronous results and status changes.
- Permit browser zoom, support enlarged text, and prevent clipped primary
  actions at narrow widths. Test text contrast and day/night states.
- Use one icon set and a small, consistent spacing/type scale. Reserve strong
  accent fills for primary actions; use quieter styling for secondary actions.
- Keep all new user-facing strings in the shared Spanish/English dictionary.

**Acceptance:** Complete search, confirmation and route selection by keyboard.
Test at 320, 375 and 430 CSS-pixel widths, landscape and a desktop width, with
enlarged text, both languages, day/night themes and reduced motion enabled.

## Implementation touchpoints

| Area | Likely files |
|---|---|
| Starting screen and saved shortcuts | `web/index.html`, `web/js/search.js`, `web/js/map.js` |
| Search results, map pins and panels | `web/js/search.js`, `web/js/map.js`, `web/js/ui.js` |
| Destination confirmation and place cards | `web/js/poi.js`, `web/js/map.js`, `web/js/ui.js` |
| Route comparison and trip state | `web/js/poi.js`, `web/js/nav.js`, `web/js/navmath.js` |
| Offline status and download lifecycle | `web/js/offline.js`, `web/sw.js`, `web/js/map.js` |
| Shared layout and accessibility | `web/css/app.css`, `web/index.html`, `web/js/ui.js` |

Confirm actual contracts before changing them. Keep public submissions behind
existing moderation and preserve server error codes and cancellation behavior.

## How to validate the redesign

Use these tasks in a prototype review and then on real devices:

1. Find a previously visited business and begin a route.
2. Find a landmark address, correct its entrance and confirm the destination.
3. Compare alternatives, enable avoid-unpaved, and start the selected route.
4. Recover after denying location permission or losing connectivity.
5. Download a region, restart offline, and identify which functions still work.

Measure task completion, wrong-destination selections, time to a confirmed
route, correction success, and recovery from errors. Establish a baseline before
setting improvement targets. Do not log precise coordinates, raw address
queries, Home/Work locations or personal route histories for these measurements.

Add executable regression tests for state transitions and accessible controls,
then verify actual rendering on Android and iOS. Run the existing `make check`
gate before publishing each implementation phase. Unit tests alone do not
validate touch ergonomics, browser permissions or offline startup.

## Supporting guidance

- [Material web touch targets](https://m2.material.io/develop/web/supporting/touch-target)
- [Progressive disclosure](https://www.nngroup.com/articles/progressive-disclosure/)
- [Recognition and recall](https://www.nngroup.com/articles/recognition-and-recall/)

These references inform the proposed design; they do not replace testing with
people navigating the actual service area.
