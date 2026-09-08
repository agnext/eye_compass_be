# 3. Frontend: Base Architecture & Setup Walkthrough

This document records the frontend's initial scaffolding and architectural
decisions. It describes the shape of `eye_compass_fe` as a React application;
for how closely its screens actually match legacy pixel-for-pixel and
behavior-for-behavior, see `9 - post_remediation_session_log.md`, which covers
a long series of side-by-side comparisons against real legacy screenshots and
the fixes that came out of them.

## 1. Core Scaffolding
- **Tooling:** Initialized with `Vite` using the `react` template.
- **Key dependencies:** `react-router-dom` (client-side routing between every
  screen), `@reduxjs/toolkit` + `react-redux` (global state and RTK Query for
  API data fetching/caching), `vite-plugin-pwa` (service worker and web
  manifest generation for installable/Kiosk-mode use).

## 2. Styling
Each page owns its own CSS file (`Login.css`, `Home.css`, `Dashboard.css`,
etc.) rather than a single shared design system — this turned out to matter in
practice, because matching legacy's actual look required page-by-page,
pixel-level comparison against real screenshots from the production device
(colors, fonts, field styling all pulled directly from the legacy Qt
stylesheets embedded in `eye_compass_ui.py`), not a single reusable theme.
`src/styles/global.css` holds the handful of classes genuinely shared across
pages (`.btn-nav`, `.btn-logout`).

## 3. Component Architecture
- **`src/layouts/MainLayout.jsx`** exists but is used by only one page
  (`History.jsx`) — most pages (`Home.jsx`, `NewBatch.jsx`, `Dashboard.jsx`,
  `Login.jsx`) each render their own header directly rather than sharing this
  layout. This is not an oversight to "fix" by forcing everything through one
  shared shell — different legacy pages genuinely have different headers
  (see `9 - post_remediation_session_log.md` for concrete examples: the New
  Batch page's header differs from the live-scan page's, which itself changes
  shape depending on whether a foreign-matter detection is in progress).
- **`src/pages/Dashboard.jsx`**: the live inspection screen — camera feed,
  foreign-matter detection overlay, and the interlock/labeling flow. The most
  behaviorally complex screen in the app; see `9 -
  post_remediation_session_log.md` for how its header and controls were
  brought in line with legacy's actual (and non-obvious) two-state layout.

## 4. Progressive Web App (PWA) Configuration
`vite.config.js` configures `vite-plugin-pwa` with:
- `display: 'standalone'` — so an installed/kiosk-launched instance has no
  browser toolbar or URL bar.
- `theme_color` / `background_color` — currently placeholder values, not yet
  finalized against the actual legacy branding ("Compass Eye").
- A manifest icon pointing at `/logo.png` — also a placeholder; the real app
  icon has not been finalized.

This PWA configuration exists in the build but **has not yet been tested as an
installed/kiosk app on the target device** — see `10 -
pwa_and_deployment_rollout.md` for what that actually involves and what is
still outstanding, and `todos.md` for the open item to test it for real.

## 5. Page Inventory
- **`Login.jsx`** — authenticates against the backend; matches legacy's actual
  branding ("Compass Eye" header, field styling from `Lineedit_username`/
  `Lineedit_password`'s Qt stylesheet) after direct comparison with a
  production screenshot.
- **`Home.jsx`** — the main menu: New Batch, History, Data Collection.
- **`NewBatch.jsx`** — the 12-field batch creation form, server-populated
  dropdowns (commodity, variety, vendor, brand, sorter), matched field-by-field
  and grouping-by-grouping against a real device screenshot.
- **`DetailsEntry.jsx`** — the Data Collection entry form (sample id,
  commodity, variety) and its own conveyor Start/Stop controls, distinct from
  the live-scan page's controls.
- **`DataCollection.jsx`** — the raw frame-capture screen (Start/Stop/Forward/
  Capture Image/Submit), a completely separate flow from the main scan/
  detection pipeline — see `9 - post_remediation_session_log.md` for why this
  page did not exist at all before this session, despite legacy having one.
- **`Dashboard.jsx`** — the live inspection screen (camera feed, FM detection,
  labeling, submit).
- **`ResultsViewer.jsx`** — the post-scan results/breakdown screen, including
  the two-step submit-then-confirm flow described in `9 -
  post_remediation_session_log.md`.
- **`History.jsx`** — past-results table with paging and re-sync.
