from flask import Flask, render_template_string, jsonify, request
import requests
import math
import re
import os
from datetime import datetime, timezone, timedelta

app = Flask(__name__)

# =====================================================
# 🗄️ CACHE
# =====================================================
_forecast_cache = {}
CACHE_TTL = timedelta(minutes=60)

# =====================================================
# 🪂 DROPZONES
# =====================================================

def load_dropzones(path="Dropzone list.txt"):
    dz = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if ":" not in line:
                    continue
                name, coords = line.split(":", 1)
                lat, lon = coords.split(",")
                dz[name.strip()] = (float(lat), float(lon))
    except Exception:
        dz = {"default DZ": (43.3712, -70.9259)}
    return dz


DROPZONES = load_dropzones()


# =====================================================
# 🌐 FORECAST  —  NWS api.weather.gov + aviationweather.gov
#
# No API key. No rate limits. Official US government data.
#
# Surface wind  → NWS gridded hourly forecast
#                 api.weather.gov/points/{lat},{lon}  →  gridpoints URL
#                 gridpoints URL/forecast/hourly      →  hourly wind speed/direction
#
# Upper winds   → aviationweather.gov windtemp text product
#                 /api/data/windtemp?region=bos&level=low|high&fcst=06|12|24
#                 Parsed into altitudes 3000-39000 ft
# =====================================================

HEADERS = {"User-Agent": "MangoWindHub/1.0 contact@skydiveapp.example"}

# Cache the NWS gridpoint URL per location (changes rarely)
_nws_grid_cache = {}


def get_nws_grid_url(lat, lon):
    """Resolve lat/lon → NWS gridpoint forecast/hourly URL. Cached permanently."""
    key = (round(lat, 3), round(lon, 3))
    if key in _nws_grid_cache:
        return _nws_grid_cache[key]
    try:
        r = requests.get(f"https://api.weather.gov/points/{lat},{lon}",
                         timeout=10, headers=HEADERS)
        r.raise_for_status()
        url = r.json()["properties"]["forecastHourly"]
        _nws_grid_cache[key] = url
        print(f"NWS grid URL resolved for {key}: {url}")
        return url
    except Exception as e:
        print(f"NWS points error: {e}")
        return None


def fetch_nws_surface(lat, lon):
    """
    Fetch hourly surface wind from NWS for the next 72 hours.
    Returns list of dicts: [{time, speed_kts, direction}, ...]
    """
    url = get_nws_grid_url(lat, lon)
    if not url:
        return []
    try:
        r = requests.get(url, timeout=15, headers=HEADERS)
        r.raise_for_status()
        periods = r.json()["properties"]["periods"]
        result = []
        for p in periods[:72]:
            spd = p.get("windSpeed", "0 mph")
            # NWS returns "10 mph" or "10 to 20 mph" — take first number
            spd_val = float(spd.split()[0]) * 0.868976   # mph → knots
            dir_str = p.get("windDirection", "N")
            dir_map = {"N":0,"NNE":22.5,"NE":45,"ENE":67.5,"E":90,"ESE":112.5,
                       "SE":135,"SSE":157.5,"S":180,"SSW":202.5,"SW":225,
                       "WSW":247.5,"W":270,"WNW":292.5,"NW":315,"NNW":337.5}
            dir_val = dir_map.get(dir_str, 0)
            result.append({
                "time":      p["startTime"],
                "speed_kts": round(spd_val, 1),
                "direction": dir_val,
            })
        print(f"NWS surface: {len(result)} hours fetched")
        return result
    except Exception as e:
        print(f"NWS surface fetch error: {e}")
        return []


# FD winds-aloft altitude columns in order
FD_ALTS = [3000, 6000, 9000, 12000, 18000, 24000, 30000, 34000, 39000]


def decode_fd_group(raw):
    """
    Decode a single FD wind group string into (direction_deg, speed_kts).
    Formats:
      4-char: DDSS          e.g. "2726" → 270°, 26kt
      6-char: DDSSTT        e.g. "2726-09" stripped to "2726" then temp part
      "9900" → light & variable → (0, 0)
      "////" or blank → None
    """
    raw = raw.strip().lstrip("+").replace(" ", "")
    # Strip temperature suffix (sign + 2 digits)
    raw = re.split(r'[+-]\d{2}$', raw)[0]
    if not raw or raw in ("9900", "////", "0000", ""):
        return None
    if len(raw) < 4:
        return None
    try:
        dd = int(raw[0:2])
        ss = int(raw[2:4])
        # Speed ≥ 100kt encoded: dd > 36, subtract 50 from dd, add 100 to ss
        if dd > 36:
            dd -= 50
            ss += 100
        direction = (dd * 10) % 360
        return direction, ss
    except Exception:
        return None


def fetch_aviation_winds(lat, lon):
    """
    Fetch FAA winds-aloft text product from aviationweather.gov.
    Parses the nearest station and returns {alt_ft: {speed_kts, direction}}.
    """
    # Pick region based on lon
    if lon < -120:
        region = "slc"
    elif lon < -105:
        region = "dfw"
    elif lon < -90:
        region = "chi"
    elif lon < -75:
        region = "pit"
    else:
        region = "bos"

    for fcst in ["06", "12", "24"]:
        for level in ["low", "high"]:
            try:
                url = "https://aviationweather.gov/api/data/windtemp"
                params = {"region": region, "level": level, "fcst": fcst}
                r = requests.get(url, timeout=10, headers=HEADERS, params=params)
                if r.status_code != 200:
                    continue
                text = r.text
                result = parse_fd_text(text, level)
                if result:
                    print(f"Aviation winds OK: {len(result)} levels from {region}/{level}/{fcst}")
                    return result
            except Exception as e:
                print(f"Aviation winds error ({region}/{level}/{fcst}): {e}")

    print("Aviation winds: all attempts failed")
    return {}


def parse_fd_text(text, level):
    """
    Parse the FD plain-text product and average all station values per altitude.
    Returns {alt_ft: {speed_kts, direction}}.
    
    Example line:
      BOS 2107 1908+08 1710+02 1809-04 2907-15 2809-25 341140 011349 011660
    Columns after station ID map to FD_ALTS: 3000 6000 9000 12000 18000 24000 30000 34000 39000
    """
    # Alts included in low vs high products
    if level == "low":
        alts = [3000, 6000, 9000, 12000, 18000, 24000]
        n_cols = 6
    else:
        alts = [24000, 30000, 34000, 39000]
        n_cols = 4

    # Accumulate sin/cos sums and speeds for averaging across stations
    sums = {a: {"sin": 0.0, "cos": 0.0, "spd": 0.0, "n": 0} for a in alts}

    lines = text.splitlines()
    data_started = False
    for line in lines:
        line = line.strip()
        # Header line marks start of data
        if line.startswith("FT") or "3000" in line and "6000" in line:
            data_started = True
            continue
        if not data_started:
            continue
        # Station data lines: 3-letter ID followed by wind groups
        parts = line.split()
        if len(parts) < 2:
            continue
        station_id = parts[0]
        if not (len(station_id) == 3 and station_id.isalpha()):
            continue
        groups = parts[1:]
        for i, alt in enumerate(alts):
            if i >= len(groups):
                break
            decoded = decode_fd_group(groups[i])
            if decoded is None:
                continue
            direction, speed = decoded
            r = math.radians(direction)
            sums[alt]["sin"] += math.sin(r)
            sums[alt]["cos"] += math.cos(r)
            sums[alt]["spd"] += speed
            sums[alt]["n"]   += 1

    result = {}
    for alt, s in sums.items():
        if s["n"] == 0:
            continue
        avg_dir = math.degrees(math.atan2(s["sin"], s["cos"])) % 360
        avg_spd = s["spd"] / s["n"]
        result[alt] = {"speed_kts": round(avg_spd, 1), "direction": round(avg_dir, 0)}

    return result


def fetch_forecast(lat, lon):
    """
    Main fetch: combines NWS surface winds + FAA upper winds into a unified
    data structure that format_winds() can consume.
    Returns {"surface": [...], "upper": {alt_ft: {speed_kts, direction}}, "time": [...]}
    """
    key = (round(lat, 3), round(lon, 3))
    now = datetime.now(timezone.utc)

    cached = _forecast_cache.get(key)
    if cached and cached["expires"] > now:
        print(f"Cache HIT for {key}")
        return cached["data"]

    surface = fetch_nws_surface(lat, lon)
    upper   = fetch_aviation_winds(lat, lon)

    if not surface and not upper:
        if cached:
            print("Fetch failed — returning stale cache")
            return cached["data"]
        return None

    data = {"surface": surface, "upper": upper}
    _forecast_cache[key] = {"data": data, "expires": now + CACHE_TTL}
    print(f"Forecast assembled: {len(surface)} surface hours, {len(upper)} upper levels")
    return data


# =====================================================
# 📊 WIND MODEL
# =====================================================

def wind_arrow(d):
    return ["↑","↗","→","↘","↓","↙","←","↖","↑"][int((d % 360) / 45)]


def color(s):
    if s < 10:  return "green"
    if s < 25:  return "orange"
    return "red"


def interpolate(base, alt):
    """
    Linear interpolation between the two bracketing pressure levels.

    base: list of (altitude_ft, speed, direction) tuples, sorted ascending.
    Returns (speed, direction) at `alt`.
    """
    # Below lowest level — return surface value
    if alt <= base[0][0]:
        return base[0][1], base[0][2]

    # Above highest level — return top value
    if alt >= base[-1][0]:
        return base[-1][1], base[-1][2]

    # Find the bracketing pair
    for i in range(len(base) - 1):
        a0, s0, d0 = base[i]
        a1, s1, d1 = base[i + 1]

        if a0 <= alt <= a1:
            # t = 0 → at a0, t = 1 → at a1
            t = (alt - a0) / (a1 - a0)

            # Interpolate speed linearly
            speed = s0 + (s1 - s0) * t

            # Interpolate direction via unit-vector averaging to handle 0/360 wrap
            r0 = math.radians(d0)
            r1 = math.radians(d1)
            sin_avg = math.sin(r0) + (math.sin(r1) - math.sin(r0)) * t
            cos_avg = math.cos(r0) + (math.cos(r1) - math.cos(r0)) * t
            direction = math.degrees(math.atan2(sin_avg, cos_avg)) % 360

            return speed, direction

    # Fallback (should not reach here)
    return base[-1][1], base[-1][2]


def format_winds(data, hour):
    """
    Converts the NWS+aviation data structure into the altitude dict
    that the rest of the app expects.
    data = {"surface": [{time, speed_kts, direction}, ...],
            "upper":   {alt_ft: {speed_kts, direction}}}
    hour = index 0-71
    """
    if not data:
        print("format_winds: data is None")
        return {}

    try:
        surface_list = data.get("surface", [])
        upper        = data.get("upper", {})

        # Pick the surface hour (clamp to available)
        idx = min(hour, len(surface_list) - 1) if surface_list else 0
        if surface_list:
            surf = surface_list[idx]
            surf_speed = surf["speed_kts"]
            surf_dir   = surf["direction"]
        else:
            surf_speed, surf_dir = 0, 0

        print(f"format_winds: hour={hour}, surf={surf_speed}kt@{surf_dir}°, upper levels={list(upper.keys())}")

        # Build interpolation base from surface + upper wind levels
        # Upper winds are a snapshot (not time-indexed) — best available
        base = [(0, surf_speed, surf_dir)]
        for alt_ft in sorted(upper.keys()):
            w = upper[alt_ft]
            base.append((alt_ft, w["speed_kts"], w["direction"]))

        if len(base) < 2:
            # Fallback: flat wind profile from surface
            for alt_ft in [3000, 6000, 9000, 12000]:
                base.append((alt_ft, surf_speed, surf_dir))

        result = {}

        # Surface entry at 0 ft
        result[0] = {
            "speed":     round(surf_speed, 1),
            "direction": round(surf_dir % 360, 0),
            "arrow":     wind_arrow(surf_dir),
            "color":     color(surf_speed),
        }

        # Interpolate every 1000 ft from 1000 to 14000
        for alt in range(1000, 15000, 1000):
            speed, direction = interpolate(base, alt)
            result[alt] = {
                "speed":     round(speed, 1),
                "direction": round(direction % 360, 0),
                "arrow":     wind_arrow(direction),
                "color":     color(speed),
            }

        print(f"format_winds: OK, {len(result)} levels")
        return result

    except Exception as e:
        import traceback
        print(f"format_winds ERROR: {e}")
        traceback.print_exc()
        return {}


# =====================================================
# 🧠 VECTOR-AVERAGED LAYER WIND
# =====================================================

def avg_wind_display(winds, low, high):
    speeds = []
    sin_sum = 0.0
    cos_sum = 0.0

    for alt in sorted(winds.keys()):
        if low <= alt < high:
            w = winds[alt]
            speeds.append(w["speed"])
            r = math.radians(w["direction"])
            sin_sum += math.sin(r)
            cos_sum += math.cos(r)

    if not speeds:
        return 0, 0

    avg_speed = sum(speeds) / len(speeds)
    avg_dir   = math.degrees(math.atan2(sin_sum, cos_sum)) % 360

    return avg_speed, avg_dir


# =====================================================
# 🚀 PHYSICS
# =====================================================

def canopy_data(wind_speed_kts, wind_dir):
    """
    Canopy reach model (2:1 glide ratio, ~1500 ft/min descent from deployment at ~4000 ft):
      - Descent time  ≈ 120 s  (3000 ft at 1500 fpm: 4000 ft deployment down to 1000 ft)
      - Canopy airspeed ≈ 22 kt forward speed → glide radius = airspeed × time
      - Wind drift = wind vector × descent time, shifts the circle center
    Returns glide_radius_m, wind_drift_m, wind_dir_deg
    """
    descent_time   = 120          # seconds (3000ft at 1500fpm: deployment at 4000ft down to 1000ft)
    canopy_kts     = 22           # canopy forward airspeed in knots
    canopy_ms      = canopy_kts * 0.514444
    glide_radius   = canopy_ms * descent_time   # metres — pure canopy reach

    wind_ms        = wind_speed_kts * 0.514444
    wind_drift     = wind_ms * descent_time     # metres — wind pushes the circle

    return glide_radius, wind_drift, wind_dir


def freefall_distance(wind_speed_kts, wind_dir):
    """Estimated drift in freefall (≈60 s, 10 000 ft jump)."""
    seconds = 60
    wind_ms = wind_speed_kts * 0.514444
    return wind_ms * seconds     # metres


def surface_drift(wind_speed_kts, wind_dir):
    """Estimated surface drift during landing roll / run-out (15 s)."""
    seconds = 15
    wind_ms = wind_speed_kts * 0.514444
    return wind_ms * seconds     # metres


# =====================================================
# API
# =====================================================

@app.route("/data")
def data():
    lat  = request.args.get("lat",  type=float)
    lon  = request.args.get("lon",  type=float)
    hour = request.args.get("hour", 0, type=int)

    if lat is None or lon is None:
        return jsonify({"error": "lat and lon are required"}), 400

    raw   = fetch_forecast(lat, lon)
    winds = format_winds(raw, hour)

    if not winds:
        print(f"ERROR: winds empty for lat={lat} lon={lon} hour={hour}, raw={raw is not None}")
        resp = jsonify({"error": "Could not fetch forecast — Open-Meteo may be rate limiting. Try again in 60 seconds."})
        resp.headers["Cache-Control"] = "no-store"
        return resp, 503

    # Layer averages
    canopy_speed,  canopy_dir  = avg_wind_display(winds, 0, 3000)    # SFC - 3K ft
    free_speed,    free_dir    = avg_wind_display(winds, 4000, 14000) # 4K - 14K ft

    # Time label — derive from NWS surface period time if available
    time_label = ""
    try:
        surface_list = raw.get("surface", []) if raw else []
        idx = min(hour, len(surface_list) - 1) if surface_list else -1
        if idx >= 0:
            time_label = surface_list[idx]["time"][:16].replace("T", " ") + " (local)"
    except Exception:
        pass

    response = jsonify({
        "winds": winds,
        "canopy": {
            "speed":       canopy_speed,
            "direction":   canopy_dir,
            "glide_radius": canopy_data(canopy_speed, canopy_dir)[0],
            "wind_drift":   canopy_data(canopy_speed, canopy_dir)[1],
            "wind_dir":     canopy_data(canopy_speed, canopy_dir)[2],
        },
        "freefall": {
            "speed":     free_speed,
            "direction": (free_dir + 180) % 360,   # convert met "from" → drift "to" direction
            "distance":  freefall_distance(free_speed, free_dir),
        },
        "wind_14k": {
            "speed":     winds.get(14000, {}).get("speed", 0),
            "direction": winds.get(14000, {}).get("direction", 0),
        },
        "time_label": time_label,
    })
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


# =====================================================
# FRONTEND
# =====================================================

HTML = r"""
<!DOCTYPE html>
<html>
<head>
<title>Mango Wind Hub</title>
<meta name="viewport" content="width=device-width, initial-scale=1">

<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Barlow+Condensed:wght@300;400;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.3/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.3/dist/leaflet.js"></script>

<style>
:root {
    --bg:        #070b10;
    --panel-bg:  #0d1520;
    --card-bg:   #111d2b;
    --card-alt:  #0c1825;
    --border:    #1e3045;
    --text:      #c8daea;
    --muted:     #5a7a96;
    --accent:    #00d4ff;
    --canopy:    #39ff89;
    --freefall:  #ffaa00;
    --surface:   #ffd166;
    --green:     #39ff89;
    --orange:    #ffaa00;
    --red:       #ff4f4f;
    --panel-w:   290px;
    --drawer-h:  56px;   /* collapsed handle height on mobile */
}

* { box-sizing: border-box; margin: 0; padding: 0; }

body {
    font-family: 'Barlow Condensed', sans-serif;
    background: var(--bg);
    color: var(--text);
    height: 100vh;
    height: 100dvh;
    overflow: hidden;
    -webkit-tap-highlight-color: transparent;
}

/* ═══════════════════════════════════════
   DESKTOP  (≥ 600 px wide)
   Panel on the right, map fills the rest
═══════════════════════════════════════ */
#wrap {
    display: flex;
    height: 100vh;
    height: 100dvh;
}
#map { flex: 1; min-width: 0; }

#panel {
    width: var(--panel-w);
    background: var(--panel-bg);
    display: flex;
    flex-direction: column;
    border-left: 1px solid var(--border);
    overflow: hidden;
    transition: transform 0.3s ease;
}

/* ═══════════════════════════════════════
   MOBILE  (< 600 px wide)
   Map full-screen, panel slides up from bottom
═══════════════════════════════════════ */
@media (max-width: 599px) {
    #wrap { flex-direction: column; position: relative; }
    #map  { position: absolute; inset: 0; z-index: 1; }

    #panel {
        position: absolute;
        bottom: 0; left: 0; right: 0;
        width: 100%;
        max-height: 88dvh;
        border-left: none;
        border-top: 2px solid var(--border);
        border-radius: 18px 18px 0 0;
        z-index: 10;
        transform: translateY(calc(100% - var(--drawer-h)));
        transition: transform 0.35s cubic-bezier(.4,0,.2,1);
        box-shadow: 0 -8px 32px rgba(0,0,0,0.6);
    }

    #panel.open {
        transform: translateY(0);
    }

    /* Drag handle */
    #panel-handle {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 10px 16px 6px;
        cursor: pointer;
        flex-shrink: 0;
        height: var(--drawer-h);
        border-bottom: 1px solid var(--border);
        user-select: none;
    }
    #panel-handle .handle-bar {
        width: 36px; height: 4px;
        background: var(--muted);
        border-radius: 2px;
        margin: 0 auto 4px;
    }
    #panel-handle .handle-title {
        font-weight: 700;
        font-size: 1rem;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: var(--accent);
    }
    #panel-handle .handle-summary {
        font-family: 'Share Tech Mono', monospace;
        font-size: 0.68rem;
        color: var(--muted);
    }
    #panel-handle-inner {
        display: flex;
        flex-direction: column;
        align-items: center;
        width: 100%;
    }

    /* Hide normal header on mobile — info shown in handle */
    #panel-header { display: none; }

    /* Scrollable body on mobile */
    #panel-body {
        overflow-y: auto;
        -webkit-overflow-scrolling: touch;
        flex: 1;
    }

    /* Larger touch targets */
    select { padding: 10px 12px; font-size: 1rem; }
    #hour  { height: 28px; }
    #drawBtn, #replayBtn { padding: 12px 10px; font-size: 1.05rem; }
    .wind-card { padding: 8px 10px; }
    .summary-card { padding: 10px 14px; }
    .sc-data { font-size: 0.85rem !important; gap: 10px !important; }
}

@media (min-width: 600px) {
    #panel-handle { display: none; }
    #panel-body   { display: contents; }
}

/* ── PANEL HEADER (desktop) ── */
#panel-header {
    padding: 14px 16px 10px;
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
}
#panel-header h1 {
    font-weight: 700;
    font-size: 1.35rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--accent);
}
#panel-header p {
    font-family: 'Share Tech Mono', monospace;
    font-size: 0.7rem;
    color: var(--muted);
    margin-top: 2px;
}

/* ── CONTROLS ── */
#controls {
    padding: 12px 14px;
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
}

label {
    font-size: 0.72rem;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--muted);
    display: block;
    margin-bottom: 4px;
}

select {
    width: 100%;
    background: var(--card-bg);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 7px 10px;
    font-family: 'Barlow Condensed', sans-serif;
    font-size: 0.95rem;
    outline: none;
    cursor: pointer;
    appearance: none;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%235a7a96' stroke-width='1.5' fill='none' stroke-linecap='round'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: right 10px center;
}
select:focus { border-color: var(--accent); }

.time-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-top: 10px;
    margin-bottom: 4px;
}
.time-row label { margin: 0; }

#timeLabel {
    font-family: 'Share Tech Mono', monospace;
    font-size: 0.72rem;
    color: var(--accent);
    white-space: nowrap;
}

#hour {
    width: 100%;
    accent-color: var(--accent);
    cursor: pointer;
    height: 6px;
    border-radius: 3px;
}

/* ── SUMMARY CARDS ── */
#summaries {
    padding: 10px 14px;
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
}

.summary-card {
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 9px 12px;
    position: relative;
    overflow: hidden;
}
.summary-card::before {
    content: '';
    position: absolute;
    left: 0; top: 0; bottom: 0;
    width: 3px;
}
.summary-card.canopy::before   { background: var(--canopy); }
.summary-card.freefall::before { background: var(--freefall); }
.summary-card .sc-label {
    font-size: 0.68rem;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    margin-bottom: 5px;
}
.summary-card.canopy   .sc-label { color: var(--canopy); }
.summary-card.freefall .sc-label { color: var(--freefall); }
.summary-card .sc-data {
    display: flex;
    gap: 16px;
    font-family: 'Share Tech Mono', monospace;
    font-size: 0.8rem;
    color: var(--text);
    flex-wrap: wrap;
}
.summary-card .sc-data span { color: var(--muted); font-size: 0.7rem; margin-right: 2px; }

/* ── WIND TABLE ── */
#cards-wrap {
    flex: 1;
    overflow-y: auto;
    padding: 10px 14px 14px;
    -webkit-overflow-scrolling: touch;
}
#cards-wrap::-webkit-scrollbar { width: 4px; }
#cards-wrap::-webkit-scrollbar-track { background: transparent; }
#cards-wrap::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }

.alt-section-title {
    font-size: 0.64rem;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--muted);
    margin: 10px 0 4px;
}
.alt-section-title.upper { color: var(--freefall); }
.alt-section-title.lower { color: var(--canopy); }

.wind-card {
    display: grid;
    grid-template-columns: 56px 1fr auto auto;
    align-items: center;
    gap: 8px;
    padding: 6px 10px;
    margin: 2px 0;
    border-radius: 6px;
    background: var(--card-alt);
    border: 1px solid transparent;
    transition: border-color 0.15s;
    font-family: 'Share Tech Mono', monospace;
    font-size: 0.78rem;
}
.wind-card:hover { border-color: var(--border); }
.wind-card.upper { background: rgba(255,170,0,0.06); }
.wind-card.upper .alt { color: var(--freefall); opacity: 0.75; }
.wind-card .alt { color: var(--muted); font-size: 0.7rem; }
.wind-card:not(.upper) .alt { color: var(--canopy); opacity: 0.85; }
.wind-card .arrow { font-size: 1rem; }
.wind-card .speed { text-align: right; font-weight: bold; }
.wind-card .dir   { color: var(--muted); font-size: 0.72rem; text-align: right; }

.dot-green  { color: var(--green); }
.dot-orange { color: var(--orange); }
.dot-red    { color: var(--red); }

/* ── LOADING OVERLAY ── */
#loader {
    position: fixed;
    inset: 0;
    background: rgba(7,11,16,0.75);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 9999;
    backdrop-filter: blur(2px);
    transition: opacity 0.3s;
}
#loader.hidden { opacity: 0; pointer-events: none; }
.spinner {
    width: 32px; height: 32px;
    border: 3px solid var(--border);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.7s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* ── BUTTONS ── */
#drawBtn {
    margin-top: 10px;
    width: 100%;
    padding: 7px 10px;
    background: var(--card-bg);
    color: var(--accent);
    border: 1px solid var(--accent);
    border-radius: 6px;
    font-family: 'Barlow Condensed', sans-serif;
    font-size: 0.95rem;
    cursor: pointer;
    letter-spacing: 0.06em;
    transition: background 0.15s, color 0.15s;
    -webkit-tap-highlight-color: transparent;
}
#drawBtn:hover, #drawBtn:active { background: var(--accent); color: var(--bg); }
#drawBtn.active { background: var(--accent); color: var(--bg); }

#replayBtn {
    margin-top: 6px;
    width: 100%;
    padding: 6px 10px;
    background: var(--card-bg);
    color: var(--freefall);
    border: 1px solid var(--freefall);
    border-radius: 6px;
    font-family: 'Barlow Condensed', sans-serif;
    font-size: 0.95rem;
    cursor: pointer;
    letter-spacing: 0.06em;
    transition: background 0.15s, color 0.15s;
    -webkit-tap-highlight-color: transparent;
}
#replayBtn:hover, #replayBtn:active { background: var(--freefall); color: var(--bg); }

#clearBtn {
    margin-top: 6px;
    width: 100%;
    padding: 6px 10px;
    background: var(--card-bg);
    color: var(--muted);
    border: 1px solid var(--border);
    border-radius: 6px;
    font-family: 'Barlow Condensed', sans-serif;
    font-size: 0.95rem;
    cursor: pointer;
    letter-spacing: 0.06em;
    transition: background 0.15s, color 0.15s;
    -webkit-tap-highlight-color: transparent;
}
#clearBtn:hover, #clearBtn:active { background: var(--border); color: var(--text); }

#jumpRunInfo {
    margin-top: 6px;
    font-family: 'Share Tech Mono', monospace;
    font-size: 0.72rem;
    color: var(--muted);
    line-height: 1.6;
}
</style>
</head>

<body>

<div id="loader"><div class="spinner"></div></div>

<div id="wrap">

    <div id="map"></div>

    <div id="panel">

        <!-- Mobile drag handle (hidden on desktop) -->
        <div id="panel-handle" onclick="togglePanel()">
            <div id="panel-handle-inner">
                <div class="handle-bar"></div>
                <div style="display:flex;justify-content:space-between;width:100%;align-items:center">
                    <span class="handle-title">🪂 Mango Wind Hub</span>
                    <span class="handle-summary" id="handleSummary">Tap for details</span>
                </div>
            </div>
        </div>

        <!-- Desktop header (hidden on mobile) -->
        <div id="panel-header">
            <h1>🪂 Mango Wind Hub</h1>
            <p id="utcLabel">Fetching forecast...</p>
        </div>

        <!-- panel-body wraps scrollable content on mobile -->
        <div id="panel-body">

            <div id="controls">
                <label>Dropzone</label>
                <select id="dz"></select>

                <div class="time-row">
                    <label>Forecast Offset</label>
                    <span id="timeLabel">+0h</span>
                </div>
                <input type="range" min="0" max="71" value="0" id="hour">

                <button id="drawBtn" onclick="toggleDrawMode()">✏️ Draw Jump Run</button>
                <button id="replayBtn" onclick="startPlaneAnimation()" style="display:none">▶ Replay</button>
                <button id="clearBtn" onclick="clearJumpRun()" style="display:none">✕ Clear</button>
                <div id="jumpRunInfo" style="display:none"></div>
            </div>

            <div id="summaries">
                <div class="summary-card canopy" id="canopyBlock">
                    <div class="sc-label">Canopy  •  SFC – 3 000 ft</div>
                    <div class="sc-data">—</div>
                </div>
                <div class="summary-card freefall" id="freefallBlock">
                    <div class="sc-label">Freefall  •  4 000 – 14 000 ft</div>
                    <div class="sc-data">—</div>
                </div>
            </div>

            <div id="cards-wrap">
                <div id="cards"></div>
            </div>

        </div><!-- /panel-body -->

    </div>
</div>

<script>
let dz = {{ dz | tojson }};
let lat, lon;
let map, marker, canopyCircle, canopyCircleShifted, highLine;
let loadTimeout = null;
let firstLoad    = true;

// ── JUMP RUN STATE ──
let drawMode      = false;
let jumpRunLine   = null;   // drawn line on map
let freefallLine  = null;   // parallel freefall drift line
let jrStart       = null;
let jrEnd         = null;
let lastFreefall  = null;   // {distance, direction} from most recent load()
let lastWind14k   = null;   // {speed, direction} at 14 000 ft
let planeMarker   = null;   // animated plane SVG marker
let planeAnimId   = null;   // requestAnimationFrame id

// ── TIME LABEL ──
function renderTime(){
    const h      = +document.getElementById("hour").value;
    const now    = new Date();
    const future = new Date(now.getTime() + h * 3600 * 1000);
    // Round base time to nearest hour
    future.setMinutes(0, 0, 0);
    const days   = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
    const day    = days[future.getDay()];
    const raw    = future.getHours();
    const ampm   = raw >= 12 ? 'pm' : 'am';
    const h12    = raw % 12 === 0 ? 12 : raw % 12;
    const label  = h === 0 ? `${day} ${h12}${ampm} (now)` : `${day} ${h12}${ampm} (+${h}h)`;
    document.getElementById("timeLabel").innerText = label;
}

// ── VECTOR from arbitrary origin ──
function vecFrom(originLat, originLon, distance, dir, color, opts={}){
    const r    = dir * Math.PI / 180;
    const len  = distance;
    const dlat = Math.cos(r) * len / 111000;
    const dlon = Math.sin(r) * len / (111000 * Math.cos(originLat * Math.PI / 180));
    return L.polyline(
        [[originLat, originLon],[originLat + dlat, originLon + dlon]],
        { color, weight: opts.weight || 4, opacity: opts.opacity || 0.9, dashArray: opts.dash || null }
    ).addTo(map);
}

// ── ORIGINAL DZ-ANCHORED VECTOR (canopy line) ──
function vec(distance, dir, color){
    return vecFrom(lat, lon, Math.min(distance, 8000), dir, color);
}

// ── JUMP RUN: draw freefall parallel line ──
// The jump run line defines where the plane flies.
// The freefall line shows where jumpers will land relative to exit,
// drawn as a parallel line offset by the freefall drift vector,
// with the same length as the jump run line.
function drawFreefallParallel(){
    if (!jrStart || !jrEnd || !lastFreefall) return;
    if (freefallLine) { freefallLine.forEach(l => l.remove()); freefallLine = null; }

    const ff      = lastFreefall;
    const ffDist  = Math.min(ff.distance, 8000);   // capped same as canopy vec
    const ffRad   = ff.direction * Math.PI / 180;

    // Offset vector in degrees
    const cosLat  = Math.cos(jrStart[0] * Math.PI / 180);
    const dlatFF  = Math.cos(ffRad) * ffDist / 111000;
    const dlonFF  = Math.sin(ffRad) * ffDist / (111000 * cosLat);

    // Parallel line = jump run shifted by freefall drift
    const pStart  = [jrStart[0] + dlatFF, jrStart[1] + dlonFF];
    const pEnd    = [jrEnd[0]   + dlatFF, jrEnd[1]   + dlonFF];

    // Draw: dashed connector from JR start → parallel start, then the parallel line
    const connector = L.polyline([jrStart, pStart], {
        color: '#ffaa00', weight: 2, opacity: 0.45, dashArray: '5,6'
    }).addTo(map);

    const parallel = L.polyline([pStart, pEnd], {
        color: '#ffaa00', weight: 4, opacity: 0.9
    }).addTo(map);

    freefallLine = [connector, parallel];

    // Info panel
    const jrLenM = distMeters(jrStart, jrEnd);
    const bear   = bearing(jrStart, jrEnd);
    document.getElementById("jumpRunInfo").style.display = "block";
    document.getElementById("jumpRunInfo").innerHTML =
        `JR heading: ${bear.toFixed(0)}°<br>` +
        `JR length:  ${(jrLenM / 1609.344).toFixed(2)} mi<br>` +
        `FF drift:   ${(ffDist / 1609.344).toFixed(2)} mi @ ${ff.direction.toFixed(0)}°`;
}

// ── DRAW JUMP RUN (line + arrowhead + animation) ──
function drawJumpRun(){
    if (jumpRunLine) jumpRunLine.forEach(l => l.remove());
    stopPlane();

    const jrLine = L.polyline([jrStart, jrEnd], {
        color: '#fff', weight: 3, opacity: 0.85, dashArray: '8,5'
    }).addTo(map);

    // Arrowhead at jrEnd
    const bear  = bearing(jrStart, jrEnd);
    const arrow = makeArrowhead(jrEnd, bear);

    jumpRunLine = [jrLine, arrow];

    drawFreefallParallel();
    startPlaneAnimation();

    // Show replay + clear buttons
    document.getElementById("replayBtn").style.display = "block";
    document.getElementById("clearBtn").style.display  = "block";
}

function makeArrowhead(tip, hdg){
    // Draw a small filled triangle at `tip` pointing in direction `hdg`
    const size  = 14;  // metres approximate — we'll use pixel offset via map project/unproject
    const tipPx = map.latLngToLayerPoint(L.latLng(tip));
    const r     = (hdg - 90) * Math.PI / 180;  // rotate so 0° = north

    function pt(dist, ang){
        return map.layerPointToLatLng(L.point(
            tipPx.x + dist * Math.cos(r + ang),
            tipPx.y + dist * Math.sin(r + ang)
        ));
    }

    const left  = pt(14,  2.5);
    const right = pt(14, -2.5);
    const poly  = L.polygon([tip, left, right], {
        color: '#fff', fillColor: '#fff', fillOpacity: 1, weight: 0
    }).addTo(map);
    return poly;
}

// ── PLANE ANIMATION ──
let jumperMarkers = [];   // active parachute markers on map
let jumperTimers  = [];   // setTimeout ids for scheduling jumpers

function planeIcon(hdg){
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="36" height="36" viewBox="-18 -18 36 36"
        style="transform:rotate(${hdg}deg);overflow:visible">
      <ellipse cx="0" cy="0"    rx="2.5" ry="9"   fill="#ff4f4f"/>
      <ellipse cx="0" cy="-10" rx="1.8" ry="3"   fill="#cc2222"/>
      <ellipse cx="0" cy="-1"  rx="14"  ry="2.2" fill="#ff6666"/>
      <ellipse cx="0" cy="-1"  rx="16"  ry="1.1" fill="#dd4444" opacity="0.6"/>
      <ellipse cx="0" cy="8"   rx="6"   ry="1.3" fill="#ff6666"/>
      <ellipse cx="0" cy="6.5" rx="1"   ry="2.5" fill="#cc2222"/>
      <ellipse cx="0" cy="-11.5" rx="1.2" ry="1.8" fill="#aa2222"/>
      <ellipse cx="0" cy="-13" rx="4"   ry="0.6" fill="#ff4f4f" opacity="0.7"/>
    </svg>`;
    return L.divIcon({ html: svg, className: '', iconSize: [36,36], iconAnchor: [18,18] });
}

function parachuteIcon(){
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="28" viewBox="0 0 24 28">
      <!-- canopy dome -->
      <path d="M2,13 Q2,2 12,2 Q22,2 22,13 Z" fill="#ff4f4f" stroke="#cc2222" stroke-width="0.8"/>
      <!-- canopy panels -->
      <line x1="12" y1="2"  x2="12" y2="13" stroke="#cc2222" stroke-width="0.5"/>
      <line x1="7"  y1="3"  x2="10" y2="13" stroke="#cc2222" stroke-width="0.5"/>
      <line x1="17" y1="3"  x2="14" y2="13" stroke="#cc2222" stroke-width="0.5"/>
      <line x1="3"  y1="7"  x2="8"  y2="13" stroke="#cc2222" stroke-width="0.5"/>
      <line x1="21" y1="7"  x2="16" y2="13" stroke="#cc2222" stroke-width="0.5"/>
      <!-- risers -->
      <line x1="8"  y1="13" x2="11" y2="22" stroke="#cc2222" stroke-width="0.7"/>
      <line x1="16" y1="13" x2="13" y2="22" stroke="#cc2222" stroke-width="0.7"/>
      <!-- jumper body -->
      <ellipse cx="12" cy="24" rx="2" ry="3" fill="#cc2222"/>
    </svg>`;
    return L.divIcon({ html: svg, className: '', iconSize: [24,28], iconAnchor: [12,28] });
}

function stopPlane(){
    if (planeAnimId)  { cancelAnimationFrame(planeAnimId); planeAnimId = null; }
    if (planeMarker)  { planeMarker.remove(); planeMarker = null; }
    jumperTimers.forEach(t => clearTimeout(t));
    jumperTimers = [];
    jumperMarkers.forEach(m => m.remove());
    jumperMarkers = [];
}

function startPlaneAnimation(){
    stopPlane();
    if (!jrStart || !jrEnd || !lastWind14k || !lastFreefall) return;

    const hdg      = bearing(jrStart, jrEnd);
    const jrDist   = distMeters(jrStart, jrEnd);

    // Ground speed with 14k wind headwind/tailwind component
    const airspeed  = 110 * 0.514444;                      // m/s
    const wSpd      = lastWind14k.speed * 0.514444;
    const wDir      = lastWind14k.direction;
    const angleDiff = ((wDir - hdg + 180 + 360) % 360) - 180;
    const headwind  = wSpd * Math.cos(angleDiff * Math.PI / 180);
    const gndSpeed  = Math.max(airspeed - headwind, airspeed * 0.4);
    const duration  = (jrDist / gndSpeed) * 1000;          // ms total flight time

    // Freefall drift vector (from lastFreefall)
    const ffDist = lastFreefall.distance;
    const ffRad  = lastFreefall.direction * Math.PI / 180;
    const cosLat0 = Math.cos(jrStart[0] * Math.PI / 180);

    // Schedule jumper drops: first at 2 s, then every 8 s while plane is still flying
    const dropTimes = [];
    for (let t = 2000; t < duration; t += 8000) dropTimes.push(t);

    dropTimes.forEach(dropMs => {
        const tid = setTimeout(() => {
            // Where is the plane at dropMs?
            const frac    = dropMs / duration;
            const exitLat = jrStart[0] + (jrEnd[0] - jrStart[0]) * frac;
            const exitLon = jrStart[1] + (jrEnd[1] - jrStart[1]) * frac;

            // Landing point = exit point + freefall drift
            const dlatFF  = Math.cos(ffRad) * ffDist / 111000;
            const dlonFF  = Math.sin(ffRad) * ffDist / (111000 * Math.cos(exitLat * Math.PI / 180));
            const landLat = exitLat + dlatFF;
            const landLon = exitLon + dlonFF;

            // Animate parachute drifting from exit → landing over ~30 s (visual)
            const jumper = L.marker([exitLat, exitLon], {
                icon: parachuteIcon(), zIndexOffset: 900
            }).addTo(map);
            jumperMarkers.push(jumper);

            const animDuration = 8000;  // 8 s visual drift
            const jStart = performance.now();

            function driftAnimate(now){
                const jt = Math.min((now - jStart) / animDuration, 1);
                jumper.setLatLng([
                    exitLat + (landLat - exitLat) * jt,
                    exitLon + (landLon - exitLon) * jt
                ]);
                if (jt < 1){
                    requestAnimationFrame(driftAnimate);
                } else {
                    // Landed — draw 100 ft translucent red circle
                    const landingCircle = L.circle([landLat, landLon], {
                        radius:      152.4,   // 500 ft in metres
                        color:       '#ff4f4f',
                        weight:      1,
                        opacity:     0.7,
                        fill:        true,
                        fillColor:   '#ff4f4f',
                        fillOpacity: 0.18
                    }).addTo(map);
                    jumperMarkers.push(landingCircle);
                }
            }
            requestAnimationFrame(driftAnimate);

        }, dropMs);
        jumperTimers.push(tid);
    });

    // Animate the plane
    planeMarker = L.marker(jrStart, { icon: planeIcon(hdg), zIndexOffset: 1000 }).addTo(map);
    const startTime = performance.now();

    function animate(now){
        const t = Math.min((now - startTime) / duration, 1);
        planeMarker.setLatLng([
            jrStart[0] + (jrEnd[0] - jrStart[0]) * t,
            jrStart[1] + (jrEnd[1] - jrStart[1]) * t
        ]);
        if (t < 1){
            planeAnimId = requestAnimationFrame(animate);
        } else {
            planeAnimId = null;
            document.getElementById("replayBtn").style.display = "block";
        }
    }
    planeAnimId = requestAnimationFrame(animate);
}

// ── GEOMETRY HELPERS ──
function distMeters(a, b){
    const R   = 6378137;
    const dLat = (b[0]-a[0]) * Math.PI/180;
    const dLon = (b[1]-a[1]) * Math.PI/180;
    const s   = Math.sin(dLat/2)*Math.sin(dLat/2) +
                Math.cos(a[0]*Math.PI/180)*Math.cos(b[0]*Math.PI/180)*
                Math.sin(dLon/2)*Math.sin(dLon/2);
    return R * 2 * Math.atan2(Math.sqrt(s), Math.sqrt(1-s));
}

function bearing(a, b){
    const dLon = (b[1]-a[1]) * Math.PI/180;
    const y    = Math.sin(dLon) * Math.cos(b[0]*Math.PI/180);
    const x    = Math.cos(a[0]*Math.PI/180)*Math.sin(b[0]*Math.PI/180) -
                 Math.sin(a[0]*Math.PI/180)*Math.cos(b[0]*Math.PI/180)*Math.cos(dLon);
    return (Math.atan2(y, x) * 180/Math.PI + 360) % 360;
}

// ── CLEAR JUMP RUN ──
function clearJumpRun(){
    if (jumpRunLine)  { jumpRunLine.forEach(l => l.remove());  jumpRunLine = null; }
    if (freefallLine) { freefallLine.forEach(l => l.remove()); freefallLine = null; }
    stopPlane();
    jrStart = null; jrEnd = null;
    document.getElementById("jumpRunInfo").style.display = "none";
    document.getElementById("replayBtn").style.display   = "none";
    document.getElementById("clearBtn").style.display    = "none";
}

// ── DRAW MODE TOGGLE ──
function toggleDrawMode(){
    drawMode = !drawMode;
    const btn = document.getElementById("drawBtn");
    if (drawMode){
        btn.classList.add("active");
        btn.textContent = "✕ Cancel Draw";
        map.getContainer().style.cursor = "crosshair";
        jrStart = null; jrEnd = null;
    } else {
        btn.classList.remove("active");
        btn.textContent = "✏️ Draw Jump Run";
        map.getContainer().style.cursor = "";
    }
}

// ── MAP CLICK HANDLER ──
function onMapClick(e){
    if (!drawMode) return;
    const pt = [e.latlng.lat, e.latlng.lng];

    if (!jrStart){
        jrStart = pt;
        // show temp marker
        if (jumpRunLine) { jumpRunLine.forEach(l => l.remove()); jumpRunLine = null; }
        if (freefallLine){ freefallLine.forEach(l => l.remove()); freefallLine = null; }
        document.getElementById("jumpRunInfo").style.display = "none";
    } else {
        jrEnd = pt;

        drawJumpRun();

        // Exit draw mode
        toggleDrawMode();
    }
}

// ── FIT VIEW ──
function fitToCanopy(radiusMeters){
    // Fit map to a circle of given radius centred on DZ
    const er   = 6378137;
    const pad  = 1.15;   // 15% padding so circles aren't clipped
    const dLat = (radiusMeters * pad / er) * (180 / Math.PI);
    const dLon = dLat / Math.cos(lat * Math.PI / 180);
    map.fitBounds(
        [[lat - dLat, lon - dLon],[lat + dLat, lon + dLon]],
        { animate: true }
    );
}

// ── COLOR CLASS ──
function colorClass(s){
    if (s < 10) return 'dot-green';
    if (s < 25) return 'dot-orange';
    return 'dot-red';
}

// ── LOAD ──
async function load(){
    document.getElementById("loader").classList.remove("hidden");

    const hour = document.getElementById("hour").value;

    try {
        const r = await fetch(`/data?lat=${lat}&lon=${lon}&hour=${hour}`);
        const d = await r.json();

        // map markers
        if (marker)              marker.remove();
        if (canopyCircle)        canopyCircle.remove();
        if (canopyCircleShifted) canopyCircleShifted.remove();
        if (highLine)            highLine.remove();

        marker = L.marker([lat, lon]).addTo(map);

        // ── CANOPY REACH CIRCLE ──
        // Center circle: glide radius from DZ (how far canopy can fly in any direction)
        const glideR = d.canopy.glide_radius;

        // Wind shifts the effective reach: offset center by wind drift vector
        const wRad   = d.canopy.wind_dir * Math.PI / 180;
        const wDrift = d.canopy.wind_drift;
        const cosLat = Math.cos(lat * Math.PI / 180);
        const driftLat = Math.cos(wRad) * wDrift / 111000;
        const driftLon = Math.sin(wRad) * wDrift / (111000 * cosLat);
        const shiftedCenter = [lat + driftLat, lon + driftLon];

        // Fit map to canopy circles on first load or DZ change
        if (firstLoad){ fitToCanopy(glideR); firstLoad = false; }

        // Dashed base circle (pure glide reach, no wind)
        canopyCircle = L.circle([lat, lon], {
            radius:    glideR,
            color:     '#39ff89',
            weight:    2,
            opacity:   0.4,
            fill:      false,
            dashArray: '6,5'
        }).addTo(map);

        // Solid shifted circle (wind-adjusted reach — where you can actually land)
        canopyCircleShifted = L.circle(shiftedCenter, {
            radius:  glideR,
            color:   '#39ff89',
            weight:  3,
            opacity: 0.85,
            fill:    true,
            fillColor:   '#39ff89',
            fillOpacity: 0.06
        }).addTo(map);

        // Store freefall + 14k wind; redraw parallel if jump run already drawn
        lastFreefall = { distance: d.freefall.distance, direction: d.freefall.direction };
        lastWind14k  = d.wind_14k;
        if (jrStart && jrEnd) drawFreefallParallel();

        // UTC label + mobile handle summary
        if (d.time_label) {
            document.getElementById("utcLabel").innerText = d.time_label;
        }
        updateHandleSummary(d.canopy.speed, d.canopy.direction,
            document.getElementById("timeLabel").innerText);

        // summary helpers
        function arrow(dir){ return ["↑","↗","→","↘","↓","↙","←","↖","↑"][Math.round(((dir + 180) % 360) / 45)]; }

        document.getElementById("canopyBlock").innerHTML =
            `<div class="sc-label">Canopy &nbsp;•&nbsp; SFC – 3 000 ft</div>
            <div class="sc-data">
                <div><span>SPD</span>${d.canopy.speed.toFixed(1)} kt</div>
                <div><span>DIR</span>${d.canopy.direction.toFixed(0)}°</div>
                <div><span>RADIUS</span>${(d.canopy.glide_radius / 1609.344).toFixed(2)} mi</div>
                <div style="font-size:1.2rem">${arrow(d.canopy.direction)}</div>
            </div>`;

        const ffFromDir = (d.freefall.direction + 180) % 360;  // convert back to wind-from for display
        document.getElementById("freefallBlock").innerHTML =
            `<div class="sc-label">Freefall &nbsp;•&nbsp; 4 000 – 14 000 ft</div>
            <div class="sc-data">
                <div><span>SPD</span>${d.freefall.speed.toFixed(1)} kt</div>
                <div><span>DIR</span>${ffFromDir.toFixed(0)}°</div>
                <div><span>DRIFT</span>${(d.freefall.distance / 1609.344).toFixed(2)} mi</div>
                <div style="font-size:1.2rem">${arrow(ffFromDir)}</div>
            </div>`;

        // wind cards
        let html = '';
        const alts = Object.keys(d.winds).map(Number).sort((a,b)=>a-b);

        html += `<div class="alt-section-title lower">Surface & Low</div>`;
        let lastGroup = 'low';

        for (const a of alts){
            const w = d.winds[a];
            const group = a < 4000 ? 'low' : 'high';

            if (group === 'high' && lastGroup === 'low'){
                html += `<div class="alt-section-title upper">Upper Winds</div>`;
                lastGroup = 'high';
            }

            const flipped = (w.direction + 180) % 360;
            const arrow   = ["↑","↗","→","↘","↓","↙","←","↖","↑"][Math.floor(flipped / 45)];
            const cls     = colorClass(w.speed);
            const altLabel = a === 0 ? 'SFC' : `${a.toLocaleString()} ft`;

            html += `<div class="wind-card ${group === 'high' ? 'upper' : ''}">
                <span class="alt">${altLabel}</span>
                <span class="arrow ${cls}">${arrow}</span>
                <span class="speed ${cls}">${w.speed.toFixed(1)} kt</span>
                <span class="dir">${w.direction.toFixed(0)}°</span>
            </div>`;
        }

        document.getElementById("cards").innerHTML = html;

    } catch(e) {
        console.error("Load error:", e);
    } finally {
        document.getElementById("loader").classList.add("hidden");
    }
}

// ── MOBILE PANEL TOGGLE ──
let panelOpen = false;
function togglePanel(){
    panelOpen = !panelOpen;
    document.getElementById("panel").classList.toggle("open", panelOpen);
    setTimeout(() => map.invalidateSize(), 350);
}
function updateHandleSummary(canopySpd, canopyDir, timeStr){
    const el = document.getElementById("handleSummary");
    if (el) el.textContent = `${timeStr}  •  ${canopySpd.toFixed(0)}kt ${canopyDir.toFixed(0)}°`;
}

// ── MAP INIT ──
function initMap(){
    map = L.map('map').setView([lat, lon], 9);
    L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
        attribution: 'Tiles © Esri'
    }).addTo(map);
    map.on('click', onMapClick);
}

// ── DZ INIT ──
function initDZ(){
    const sel  = document.getElementById("dz");
    const keys = Object.keys(dz);

    const defaultDz =
        keys.find(k => k.toLowerCase().includes("skydive new england")) || keys[0];

    for (const k of keys){
        const o = document.createElement("option");
        o.value = k; o.text = k;
        sel.appendChild(o);
    }

    sel.value = defaultDz;
    lat = dz[defaultDz][0];
    lon = dz[defaultDz][1];

    sel.onchange = () => {
        const v = sel.value;
        lat = dz[v][0];
        lon = dz[v][1];
        // Clear jump run when switching DZ
        if (jumpRunLine)  { jumpRunLine.forEach(l => l.remove());  jumpRunLine = null; }
        if (freefallLine) { freefallLine.forEach(l => l.remove()); freefallLine = null; }
        stopPlane();
        jrStart = null; jrEnd = null;
        document.getElementById("jumpRunInfo").style.display = "none";
        document.getElementById("replayBtn").style.display  = "none";
        firstLoad = true;   // re-fit zoom for new DZ
        load();
    };

    initMap();
    load();

    document.getElementById("hour").oninput = () => {
        renderTime();
        // Clear jump run and jumpers immediately on scroll
        if (jumpRunLine)  { jumpRunLine.forEach(l => l.remove());  jumpRunLine = null; }
        if (freefallLine) { freefallLine.forEach(l => l.remove()); freefallLine = null; }
        stopPlane();
        jrStart = null; jrEnd = null;
        document.getElementById("jumpRunInfo").style.display = "none";
        document.getElementById("replayBtn").style.display  = "none";
        document.getElementById("clearBtn").style.display   = "none";
        clearTimeout(loadTimeout);
        loadTimeout = setTimeout(load, 3400);
    };

    renderTime();
}

initDZ();
</script>

</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(HTML, dz=DROPZONES)


# =====================================================
# RUN
# =====================================================

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
