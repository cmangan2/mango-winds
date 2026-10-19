# Mango Wind Hub — project notes for Claude Code

Flask + Leaflet skydiving wind planning web app by Chris "Mango" Mangano (solo dev, MangoWorks3D, active skydiver).
Prod: Render, branch `main` (mango-winds.onrender.com). Dev: Render service tracking branch `dev`. Repo: cmangan2/mango-winds.
Chris likes direct numerical answers, scenario tables and frank recommendations. He tests on his Android phone (Chrome, mobile web/PWA), so mobile behavior matters as much as desktop. iOS still matters for other users (Home Screen required for push there).

## Files
- `mangowinds.py` — whole backend: Open-Meteo ensemble fetch + caching, `/data`, METAR (aviationweather.gov), ADS-B trails/poller, `/tails` (GET/POST/DELETE), `/lastload`, dropzone list (web-form dropzone commits land on `main` via the GitHub API).
- `templates/index.html` — whole frontend (HTML, CSS, JS in one file). Almost every feature change is here.
- `static/sw.js` — service worker (cache name carries the version). `static/manifest.json`, icons.
- `Dropzone list.txt` — DZ list. Runtime data files (`*.json`, user/pending dropzones) are git-ignored.
- `tests/` — `mock_server.py` (app with canned forecast, no network for weather) and `e2e.py` (Playwright smoke tests).

## Run locally
```
pip install -r requirements.txt -r requirements-dev.txt && playwright install chromium
python mangowinds.py            # http://localhost:5000 (open from the phone via the machine's LAN IP)
python tests/e2e.py             # browser smoke tests; set LEAFLET_DIR=<folder with leaflet.js/css> if the CDN is unreachable
```
Weather needs outbound access to Open-Meteo; ADS-B goes through an adsb-proxy endpoint. Cloud sandboxes can't reach either, so live-data behavior (forecast scrubbing speed, cloud popup, plane tracking, Fetch Plane Data) has only ever been tested against mocks — verify those against real data when working locally.
Optional env: `OPENMETEO_API_KEY` (paid fallback on 429), `ENV=dev` (dev banner), `*_FILE` overrides for data files.
Admin env: `ADMIN_EMAILS` (comma-separated). A signed-in account with one of these emails gets /admin, /admin/dropzones, /models, /adsb_trails/refresh. `CACHE_TOKEN` / `ADMIN_TOKEN` still work as `?token=` only when set in the env to something other than the old public defaults (`mango`, `mango-admin-2024`). /clearcache is open to all (once a minute). Admins get a 🛠️ button in the header (class `adminOnly`, shown by `updateAdminButtons()` from `/auth/me`'s `admin` flag) that opens /admin inside the app; no separate admin app (Chrome Android: one app per site scope). Shared admin head/nav: `ADMIN_HEAD`/`ADMIN_NAV`. Escape anything user-submitted (DZ names, query params) on admin pages: they run with the admin's session.
Accounts + takeoff alerts env: `DATABASE_URL` (Supabase Postgres, use the **pooler** connection string: Render has no IPv6), `SMTP_HOST/PORT/USER/PASS`, `MAIL_FROM`, `VAPID_PUBLIC_KEY`/`VAPID_PRIVATE_KEY` (raw base64url). Without `DATABASE_URL` the app uses a local SQLite file (`mwh.db`, dev/tests only); on Render (`RENDER` env set) it switches accounts/alerts off instead, since Render's disk is wiped on deploy. Without SMTP locally, sign-in codes are printed to the console.

## Standing workflow (do this unless Chris says otherwise)
1. Develop on `main`, commit, push `main`.
2. Merge `main` into `dev` and push `dev` (so the dev site tracks it).
3. **Bump the app version with every change that gets pushed** — three places must match:
   `<meta name="app-version">` and `const CURRENT_VERSION` in `templates/index.html`, and `CACHE_NAME = "mwh-v<ver>"` in `static/sw.js`. Currently 4.6.x; patch bump per change.
4. If a push is rejected, the web-form dropzone commits landed on `main`: `git fetch` then `git rebase origin/main` and push again.
5. Update the Quick Start (welcome modal "wc-section" blocks in index.html) whenever behavior changes.
6. Commit message trailer / PR text: follow the attribution lines the session gives.

## Domain model (keep consistent when editing)
- Wind vector: `wVx = spd*sin(from)`, `wVy = spd*cos(from)` points toward the wind's FROM (upwind) side. Ground speed flying home from a point in direction u = airspeed + wV·u.
- Canopy model (`getCanopyParams`): airspeed/descent from wing loading by type (student / sport / hp), optional known glide overrides airspeed. Defaults: 150 sq ft, 240 lb exit weight, sport, open 3,000 ft. `initCanopyDefaults()` derives the default globals from the form (so the default circle matches the form).
- Pattern circle: 1,200 ft diameter dashed white circle at DZ center (`PATTERN_CIRCLE_DIA_FT`). Green polygon = how far you can open and still reach that circle at 1,000 ft (`PATTERN_START_ALT_FT`): r(θ) = patternR + max(groundSpeed,0)·flightTime, 144 directions, with the downwind end clipped flat (half-plane perpendicular to the wind at `patternR + max(A−wind,0)·T`) — only matters when strong wind pinches lobes.
- Reserve range (dashed green): same shape for a 7-cell reserve opened at 2,000 ft reaching the circle at 500 ft. Defaults: PD Optimum 143, 2.2:1 glide (airspeed derived). Flight numbers other than glide are estimates, not manufacturer data.
- Landing pattern uses `canopyDescentFps` (open → 500 ft descent time still in `canopyDescentTime`).
- Freefall drift box: orange box beside the jump run from 40 s (`FF_MIN_S`) to 65 s (`FF_MAX_S`) of freefall drift (4k–14k averaged wind; `ffDriftM(sec)`); numbered jumper boxes land on its 52.5 s centerline (`FF_MID_S`). Wind widget Freefall shows drift for freefly 45 s (`FF_FREEFLY_S`) and belly 60 s (`FF_BELLY_S`) from `freefall.speed`; the server's `freefall.distance` (60 s) is only a fallback.
- Suggested Jump Run: longest run on a heading whose simulated landings (exit + 52.5 s freefall drift, the drift box centerline) stay inside the green shape shrunk by 15% (45% toward straight downwind). Jump-run speed = 85 kt airspeed vs the 14k wind.
- Recommended jumper separation = 1,500 ft of ground distance ÷ jump-run ground speed (USPA SIM 4-7), rounded to the nearest second.
- Jump run info box: heading, Prior/Past (along-track from DZ center), Offset (nearest cardinal perpendicular), est. speed, rec. separation; draggable.
- Canopy Settings (main + reserve) persist in localStorage `mwh-canopy-settings` (per device; survives app updates).
- Forecast cache is per location/model (not per hour): each Open-Meteo response holds the whole timeline, so scrubbing the slider costs no extra API calls. Slider waits 2 s after the last move before loading.
- Plane list per DZ (`/tails`), recorded flights and last loads live in DB tables `dz_tails`, `recorded_tracks`, `lastload` (k, JSON v) when the DB is on, written through from the in-memory dicts; the JSON files (`tails.json` etc.) are only used with the DB off, and imported once into an empty table; ✕ in the Track Plane list removes from it and does NOT stop tracking. Auto-track only follows planes already entered for that DZ.
- Accounts are optional: email → 6-digit code (or the link in the same email) → `mwh_session` cookie. The code exists because an iPhone Home Screen app has its own cookie jar; the link only signs in Safari. Synced settings: `canopy` (the localStorage canopy object; server copy wins on load) and `dz` (taken from the server only at sign-in). Tables: users, login_codes, sessions, push_subs, push_dz (SQL written to run on both SQLite and Postgres).
- Takeoff alerts (web push): one switch per DZ at the bottom of Track Plane. The track recorder calls `notify_airborne()` on a new flight it saw start (not on the first check after a restart); one alert per plane per 20 min. iOS needs the app on the Home Screen (16.4+).
- Cloud forecast: Open-Meteo GFS only, 5 hourly columns following the slider; METAR box only at hour offset 0.

## Gotchas
- Leaflet popups/markers on mobile: the bottom drawer (`#panel`) is `position: fixed` with `--drawer-h`; floating UI offsets from it. A "drawer disappears on mobile" report was never reproduced (defensive `healDrawer()` exists).
- Don't use `localStorage` assumptions in tests without a fresh browser context.
- Playwright tests: a stale mock server on the same port serves old HTML and gives false failures — restart it (kill by PID, not `pkill`).
- Open-Meteo requests: ≤10 variables each (fractional call counting).
- Pressure levels: 1000/975/950/925/850/700/600/500 hPa for every model, plus 800/750/650 hPa (wind + height only, one extra ≤9-var request) for the models in `EXTRA_LEVELS` (GFS, HRRR, HRDPS all three; ICON 800 only; ECMWF none). Without them the 5k→10k ft blend interpolated straight across a 5,000 ft gap and missed wind layers. Cost per location refresh: 24 calls short range (was 20), 14 long range (was 12).
- `Cache-Control: no-store` on `/`; the service worker still caches static assets by version.
