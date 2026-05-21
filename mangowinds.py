from flask import Flask, render_template, jsonify, request
import json
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
CACHE_FILE = os.path.join(os.path.dirname(__file__), "winds_cache.json")


def load_cache_from_disk():
    """Load persisted cache from disk on startup."""
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r") as f:
                raw = json.load(f)
            now = datetime.now(timezone.utc)
            for k, v in raw.items():
                expires = datetime.fromisoformat(v["expires"])
                if expires > now:
                    # Restore tuple key from string
                    parts = k.split(",")
                    key = (float(parts[0]), float(parts[1]))
                    _forecast_cache[key] = {"data": v["data"], "expires": expires}
            print(f"Loaded {len(_forecast_cache)} cache entries from disk")
    except Exception as e:
        print(f"Cache load error: {e}")


def save_cache_to_disk():
    """Persist current cache to disk."""
    try:
        serializable = {}
        for k, v in _forecast_cache.items():
            str_key = f"{k[0]},{k[1]}"
            serializable[str_key] = {
                "data": v["data"],
                "expires": v["expires"].isoformat()
            }
        with open(CACHE_FILE, "w") as f:
            json.dump(serializable, f)
    except Exception as e:
        print(f"Cache save error: {e}")


load_cache_from_disk()

# =====================================================
# 🪂 DROPZONES
# =====================================================

def load_dropzones(path="Dropzone list.txt"):
    dz = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if ":" not in line:
                    continue
                name, rest = line.split(":", 1)
                parts = [p.strip() for p in rest.split(",")]
                lat = float(parts[0])
                lon = float(parts[1])
                icao = parts[2] if len(parts) > 2 else None
                dz[name.strip()] = (lat, lon, icao)
    except Exception:
        dz = {"default DZ": (43.3712, -70.9259, "KLEB")}
    return dz


DROPZONES = load_dropzones()


# =====================================================
# 🌐 FORECAST — Mark Schulze winds_openmeteo.php proxy
# Falls back to Open-Meteo if unavailable
# =====================================================

def fetch_schulze(lat, lon, hour_offset=0):
    """Fetch winds from Schulze's parsed Open-Meteo endpoint."""
    try:
        url = "https://www.markschulze.net/winds/winds_openmeteo.php"
        params = {
            "lat": round(lat, 4),
            "lon": round(lon, 4),
            "hourOffset": hour_offset,
            "referrer": "MangoWindHub"
        }
        headers = {"User-Agent": "MangoWindHub/1.0 skydiving-wind-tool"}
        r = requests.get(url, timeout=15, params=params, headers=headers)
        r.raise_for_status()
        data = r.json()
        if "direction" in data and "speed" in data:
            print(f"Schulze fetch OK: validtime={data.get('validtime')}")
            return {"source": "schulze", "data": data}
        print(f"Schulze response missing fields: {list(data.keys())[:5]}")
        return None
    except Exception as e:
        print(f"Schulze fetch error: {e}")
        return None


def fetch_metar(icao):
    """Fetch current surface wind from aviationweather.gov METAR API."""
    if not icao:
        return None
    try:
        url = "https://aviationweather.gov/api/data/metar"
        params = {"ids": icao, "format": "json"}
        headers = {"User-Agent": "MangoWindHub/1.0 skydiving-wind-tool"}
        r = requests.get(url, timeout=10, params=params, headers=headers)
        r.raise_for_status()
        data = r.json()
        print(f"METAR raw {icao}: {data[0] if data else 'empty'}")
        if data and len(data) > 0:
            obs = data[0]
            wdir = obs.get("wdir")
            wspd = obs.get("wspd")
            temp = obs.get("temp")
            # Handle variable winds (VRB) — use 0° direction, still show speed
            if str(wdir).upper() == "VRB":
                wdir = 0
            if wdir is not None and wspd is not None:
                try:
                    wdir_f = float(wdir)
                    wspd_f = float(wspd)
                    print(f"METAR {icao}: {wspd_f}kt @ {wdir_f}°")
                    return {
                        "wdir": wdir_f,
                        "wspd": wspd_f,
                        "temp": float(temp) if temp is not None else None,
                    }
                except (ValueError, TypeError) as e:
                    print(f"METAR {icao} parse error: {e} wdir={wdir} wspd={wspd}")
            else:
                print(f"METAR {icao}: missing wdir or wspd — obs={obs}")
    except Exception as e:
        print(f"METAR fetch error for {icao}: {e}")
    return None


def fetch_forecast(lat, lon, hour_offset=0):
    # All hours use Open-Meteo, keyed by (lat, lon, hour)
    dz_key = (round(lat, 3), round(lon, 3), hour_offset)
    now = datetime.now(timezone.utc)

    cached = _forecast_cache.get(dz_key)
    if cached and cached["expires"] > now:
        print(f"Cache HIT for {dz_key}")
        return cached["data"]

    print(f"Fetching Open-Meteo for hour={hour_offset}")
    try:
        api_key = os.environ.get("OPENMETEO_API_KEY")
        url = "https://customer-api.open-meteo.com/v1/forecast" if api_key else "https://api.open-meteo.com/v1/forecast"
        hourly_fields = [
            "windspeed_10m","winddirection_10m","windspeed_80m","winddirection_80m",
            "windspeed_1000hPa","winddirection_1000hPa",
            "windspeed_975hPa","winddirection_975hPa",
            "windspeed_950hPa","winddirection_950hPa",
            "windspeed_925hPa","winddirection_925hPa",
            "windspeed_850hPa","winddirection_850hPa",
            "windspeed_700hPa","winddirection_700hPa",
            "windspeed_600hPa","winddirection_600hPa",
            "windspeed_500hPa","winddirection_500hPa",
            "temperature_1000hPa","temperature_975hPa",
            "temperature_950hPa","temperature_925hPa",
            "temperature_850hPa","temperature_700hPa",
            "temperature_600hPa","temperature_500hPa",
            "geopotential_height_1000hPa","geopotential_height_975hPa",
            "geopotential_height_950hPa","geopotential_height_925hPa",
            "geopotential_height_850hPa","geopotential_height_700hPa",
            "geopotential_height_600hPa","geopotential_height_500hPa",
        ]
        hourly_str = ",".join(hourly_fields)
        model = os.environ.get("WIND_MODEL", "gfs_seamless")
        full_url = (f"{url}?latitude={lat}&longitude={lon}"
                    f"&hourly={hourly_str}"
                    f"&forecast_days=3&timezone=auto"
                    f"&wind_speed_unit=kn"
                    f"&models={model}")
        if api_key:
            full_url += f"&apikey={api_key}"
        r = requests.get(full_url, timeout=15,
                         headers={"User-Agent": "MangoWindHub/1.0 skydiving-wind-tool"})
        if not r.ok:
            print(f"Open-Meteo {r.status_code}: {r.text[:300]}")
            r.raise_for_status()
        data = r.json()
        result = {"source": "openmeteo", "data": data}
        # Cache future-hour Open-Meteo data for 60 min
        _forecast_cache[dz_key] = {"data": result, "expires": now + CACHE_TTL}
        save_cache_to_disk()
        print(f"Open-Meteo OK for {dz_key} hour={hour_offset}")
        return result
    except Exception as e:
        print(f"Open-Meteo error: {e}")
        if cached:
            return cached["data"]
        return None


# =====================================================
# 📊 WIND MODEL
# =====================================================

def wind_arrow(d):
    return ["↑","↗","→","↘","↓","↙","←","↖","↑"][int((d % 360) / 45)]


def color(s):
    if s < 10: return "green"
    if s < 25: return "orange"
    return "red"


def interpolate(base, alt):
    base = [(a, s, d) for a, s, d in base if s is not None and d is not None]
    if not base:
        return 0, 0
    if alt <= base[0][0]:
        return base[0][1], base[0][2]
    if alt >= base[-1][0]:
        return base[-1][1], base[-1][2]
    for i in range(len(base) - 1):
        a0, s0, d0 = base[i]
        a1, s1, d1 = base[i + 1]
        if a0 <= alt <= a1:
            t = (alt - a0) / (a1 - a0)
            speed = s0 + (s1 - s0) * t
            r0 = math.radians(d0)
            r1 = math.radians(d1)
            sin_avg = math.sin(r0) + (math.sin(r1) - math.sin(r0)) * t
            cos_avg = math.cos(r0) + (math.cos(r1) - math.cos(r0)) * t
            direction = math.degrees(math.atan2(sin_avg, cos_avg)) % 360
            return speed, direction
    return base[-1][1], base[-1][2]


def format_winds(data, hour, lat=0, lon=0):
    if not data:
        print("format_winds: data is None")
        return {}
    try:
        source = data.get("source", "openmeteo")

        if source == "schulze":
            d = data["data"]
            directions = d.get("direction", {})
            speeds     = d.get("speed", {})
            temps      = d.get("temp", {})  # °C at every 1000ft
            result = {}
            for alt in [0] + list(range(1000, 15000, 1000)):
                key = str(alt)
                spd  = float(speeds.get(key, 0))
                dirn = float(directions.get(key, 0))
                tc   = temps.get(key)
                tf   = round(tc * 9/5 + 32) if tc is not None else None
                result[alt] = {
                    "speed":     round(spd, 1),
                    "direction": round(dirn % 360, 0),
                    "arrow":     wind_arrow(dirn),
                    "color":     color(spd),
                    "temp_f":    tf,
                }
            # Override SFC with Schulze's ground observation
            gspd = float(d.get("groundSpd", speeds.get("0", 0)))
            gdir = float(d.get("groundDir", directions.get("0", 0)))
            gt   = d.get("groundTemp")
            gtf  = round(gt * 9/5 + 32) if gt is not None else None
            result[0] = {
                "speed":     round(gspd, 1),
                "direction": round(gdir % 360, 0),
                "arrow":     wind_arrow(gdir),
                "color":     color(gspd),
                "temp_f":    gtf,
            }
            print(f"format_winds (schulze): {len(result)} levels")
            return result

        # Open-Meteo — use geopotential heights for accurate interpolation
        h = data["data"]["hourly"]

        # Standard atmosphere fallback heights (ft MSL) for models that don't support geopotential
        STD_HEIGHTS = {
            1000: 364, 975: 820, 950: 1555, 925: 2500,
            850: 4780, 700: 9843, 600: 14108, 500: 18289
        }
        def gh(lvl):
            val = h.get(f"geopotential_height_{lvl}hPa", [None]*200)[hour]
            if val is None:
                return float(STD_HEIGHTS.get(lvl, 0))
            return val * 3.28084

        def tc_to_f(tc):
            return round(tc * 9/5 + 32) if tc is not None else None

        all_levels = [
            (gh(1000), h["windspeed_1000hPa"][hour], h["winddirection_1000hPa"][hour], h.get("temperature_1000hPa", [None]*200)[hour]),
            (gh(975),  h["windspeed_975hPa"][hour],  h["winddirection_975hPa"][hour],  h.get("temperature_975hPa",  [None]*200)[hour]),
            (gh(950),  h["windspeed_950hPa"][hour],  h["winddirection_950hPa"][hour],  h.get("temperature_950hPa",  [None]*200)[hour]),
            (gh(925),  h["windspeed_925hPa"][hour],  h["winddirection_925hPa"][hour],  h.get("temperature_925hPa",  [None]*200)[hour]),
            (gh(850),  h["windspeed_850hPa"][hour],  h["winddirection_850hPa"][hour],  h.get("temperature_850hPa",  [None]*200)[hour]),
            (gh(700),  h["windspeed_700hPa"][hour],  h["winddirection_700hPa"][hour],  h.get("temperature_700hPa",  [None]*200)[hour]),
            (gh(600),  h["windspeed_600hPa"][hour],  h["winddirection_600hPa"][hour],  h.get("temperature_600hPa",  [None]*200)[hour]),
            (gh(500),  h["windspeed_500hPa"][hour],  h["winddirection_500hPa"][hour],  h.get("temperature_500hPa",  [None]*200)[hour]),
        ]
        # Get site elevation from Open-Meteo response (top-level field)
        try:
            elev_m = float(data["data"].get("elevation") or 0)
            elev_ft = elev_m * 3.28084
        except Exception:
            elev_ft = 0
        # Only keep pressure levels at least 300ft above site elevation
        min_alt_ft = elev_ft + 300
        pressure_levels = [(a, s, d, t) for a, s, d, t in all_levels
                           if a > min_alt_ft and s is not None and d is not None]
        # Safety: if filter removed everything, fall back to all valid levels
        if not pressure_levels:
            pressure_levels = [(a, s, d, t) for a, s, d, t in all_levels
                               if s is not None and d is not None and a > 0]

        # Interpolation base uses pressure levels only (no 10m surface anchor)
        base = [(p[0], p[1], p[2]) for p in pressure_levels]

        # SFC: average 10m and 80m AGL winds (vector average)
        spd10  = h["windspeed_10m"][hour]
        dir10  = h["winddirection_10m"][hour]
        spd80  = h.get("windspeed_80m",  [spd10]*200)[hour] or spd10
        dir80  = h.get("winddirection_80m", [dir10]*200)[hour] or dir10
        r10 = math.radians(dir10); r80 = math.radians(dir80)
        sin_s = (math.sin(r10)*spd10 + math.sin(r80)*spd80) / (spd10+spd80+0.001)
        cos_s = (math.cos(r10)*spd10 + math.cos(r80)*spd80) / (spd10+spd80+0.001)
        surf_spd = (spd10 + spd80) / 2
        surf_dir = math.degrees(math.atan2(sin_s, cos_s)) % 360

        result = {}
        result[0] = {
            "speed":     round(surf_spd, 1),
            "direction": round(surf_dir % 360, 0),
            "arrow":     wind_arrow(surf_dir),
            "color":     color(surf_spd),
            "temp_f":    tc_to_f(pressure_levels[0][3]) if pressure_levels else None,
        }
        for alt in range(1000, 15000, 1000):
            speed, direction = interpolate(base, alt)
            # Interpolate temperature between bracketing pressure levels
            temp_c = None
            for i in range(len(pressure_levels) - 1):
                a0, _, _, t0 = pressure_levels[i]
                a1, _, _, t1 = pressure_levels[i + 1]
                if a0 <= alt <= a1 and t0 is not None and t1 is not None:
                    temp_c = t0 + (t1 - t0) * (alt - a0) / (a1 - a0)
                    break
            if temp_c is None:
                if alt <= pressure_levels[0][0]:
                    temp_c = pressure_levels[0][3]
                else:
                    temp_c = pressure_levels[-1][3]
            result[alt] = {
                "speed":     round(speed, 1),
                "direction": round(direction % 360, 0),
                "arrow":     wind_arrow(direction),
                "color":     color(speed),
                "temp_f":    tc_to_f(temp_c),
            }
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
    """Simple arithmetic average of speed and direction for displayed altitudes."""
    speeds = []
    sin_sum = 0.0
    cos_sum = 0.0
    for alt in sorted(winds.keys()):
        if low <= alt < high:
            w = winds[alt]
            spd = w["speed"]
            speeds.append(spd)
            r = math.radians(w["direction"])
            sin_sum += math.sin(r)
            cos_sum += math.cos(r)
    if not speeds:
        return 0, 0
    avg_speed = sum(speeds) / len(speeds)
    avg_dir = math.degrees(math.atan2(sin_sum, cos_sum)) % 360
    return avg_speed, avg_dir


# =====================================================
# 🚀 PHYSICS
# =====================================================

def canopy_data(wind_speed_kts, wind_dir):
    descent_time = 120
    canopy_kts = 22
    canopy_ms = canopy_kts * 0.514444
    glide_radius = canopy_ms * descent_time
    wind_ms = wind_speed_kts * 0.514444
    wind_drift = wind_ms * descent_time
    return glide_radius, wind_drift, wind_dir


def freefall_distance(wind_speed_kts, wind_dir):
    seconds = 60
    wind_ms = wind_speed_kts * 0.514444
    return wind_ms * seconds


# =====================================================
# API
# =====================================================

@app.route("/debug")
def debug():
    lat = request.args.get("lat", 43.371169, type=float)
    lon = request.args.get("lon", -70.925974, type=float)

    # ── Pressure level fields ──
    pressure_fields = [
        "windspeed_10m","winddirection_10m",
        "windspeed_1000hPa","winddirection_1000hPa",
        "windspeed_975hPa","winddirection_975hPa",
        "windspeed_950hPa","winddirection_950hPa",
        "windspeed_925hPa","winddirection_925hPa",
        "windspeed_850hPa","winddirection_850hPa",
        "windspeed_700hPa","winddirection_700hPa",
        "windspeed_600hPa","winddirection_600hPa",
        "windspeed_500hPa","winddirection_500hPa",
        "geopotential_height_1000hPa","geopotential_height_975hPa",
        "geopotential_height_950hPa","geopotential_height_925hPa",
        "geopotential_height_850hPa","geopotential_height_700hPa",
        "geopotential_height_600hPa","geopotential_height_500hPa",
    ]
    # ── Altitude-based fields (metres AGL) ──
    # Open-Meteo altitude levels: 10,80,120,180m and then pressure-derived heights
    # The "wind_speed_Xm" variables are at fixed heights above ground
    alt_fields = [
        "windspeed_10m","winddirection_10m",
        "windspeed_80m","winddirection_80m",
        "windspeed_120m","winddirection_120m",
        "windspeed_180m","winddirection_180m",
    ]

    base_url = "https://api.open-meteo.com/v1/forecast"
    STD = {1000:364,975:820,950:1555,925:2500,850:4780,700:9843,600:14108,500:18289}

    def fetch_pressure(model):
        fields = ",".join(pressure_fields)
        url = (f"{base_url}?latitude={lat}&longitude={lon}"
               f"&hourly={fields}"
               f"&forecast_days=1&timezone=auto&wind_speed_unit=kn"
               + (f"&models={model}" if model else ""))
        try:
            r = requests.get(url, timeout=15, headers={"User-Agent": "MangoWindHub/1.0"})
            r.raise_for_status()
            h = r.json()["hourly"]
            def gh(lvl):
                v = h.get(f"geopotential_height_{lvl}hPa",[None]*5)[0]
                return v*3.28084 if v is not None else float(STD.get(lvl,0))
            return [
                ("10m",     33,       h["windspeed_10m"][0],     h["winddirection_10m"][0]),
                ("1000hPa", gh(1000), h["windspeed_1000hPa"][0], h["winddirection_1000hPa"][0]),
                ("975hPa",  gh(975),  h["windspeed_975hPa"][0],  h["winddirection_975hPa"][0]),
                ("950hPa",  gh(950),  h["windspeed_950hPa"][0],  h["winddirection_950hPa"][0]),
                ("925hPa",  gh(925),  h["windspeed_925hPa"][0],  h["winddirection_925hPa"][0]),
                ("850hPa",  gh(850),  h["windspeed_850hPa"][0],  h["winddirection_850hPa"][0]),
                ("700hPa",  gh(700),  h["windspeed_700hPa"][0],  h["winddirection_700hPa"][0]),
                ("600hPa",  gh(600),  h["windspeed_600hPa"][0],  h["winddirection_600hPa"][0]),
                ("500hPa",  gh(500),  h["windspeed_500hPa"][0],  h["winddirection_500hPa"][0]),
            ]
        except Exception as e:
            return [("Error", 0, None, str(e))]

    def fetch_altitude():
        # Altitude-based: 10m, 80m, 120m, 180m above ground
        # Also fetch pressure levels for comparison — these give heights in metres AGL
        fields = ",".join(alt_fields)
        url = (f"{base_url}?latitude={lat}&longitude={lon}"
               f"&hourly={fields}"
               f"&forecast_days=1&timezone=auto&wind_speed_unit=kn")
        try:
            r = requests.get(url, timeout=15, headers={"User-Agent": "MangoWindHub/1.0"})
            r.raise_for_status()
            h = r.json()["hourly"]
            return [
                ("10m AGL",   33,   h["windspeed_10m"][0],  h["winddirection_10m"][0]),
                ("80m AGL",   262,  h["windspeed_80m"][0],  h["winddirection_80m"][0]),
                ("120m AGL",  394,  h["windspeed_120m"][0], h["winddirection_120m"][0]),
                ("180m AGL",  591,  h["windspeed_180m"][0], h["winddirection_180m"][0]),
            ]
        except Exception as e:
            return [("Error", 0, None, str(e))]

    pressure_models = [("GFS", "gfs_seamless"), ("GFS+HRRR", "gfs_hrrr"), ("RAP/GFS013", "ncep_gfs013"), ("ECMWF", "ecmwf_ifs025"), ("ICON", "icon_seamless")]
    pressure_results = {name: fetch_pressure(m) for name, m in pressure_models}
    alt_results = fetch_altitude()

    def make_rows(data):
        out = ""
        for name, alt_ft, spd, dirn in data:
            grey = "color:#555" if isinstance(alt_ft, (int,float)) and alt_ft > 14500 else ""
            spd_str = f"{spd:.1f} kt" if isinstance(spd, float) else (f"{spd} kt" if spd is not None else "None")
            dir_str = f"{dirn:.0f}°"  if isinstance(dirn, float) else (f"{dirn}°" if dirn is not None else "None")
            out += f'<tr style="{grey}"><td>{name}</td><td>{alt_ft:,.0f} ft</td><td>{spd_str}</td><td>{dir_str}</td></tr>'
        return out

    cols = ""
    for name, _ in pressure_models:
        cols += f'''<td style="vertical-align:top;padding-right:28px">
            <h3>{name}</h3>
            <table>
                <tr><th>Level</th><th>Alt MSL</th><th>Speed</th><th>Dir</th></tr>
                {make_rows(pressure_results[name])}
            </table></td>'''

    alt_col = f'''<td style="vertical-align:top;padding-right:28px">
        <h3 style="color:#ffaa00">ALT-BASED</h3>
        <table>
            <tr><th>Level</th><th>Alt AGL</th><th>Speed</th><th>Dir</th></tr>
            {make_rows(alt_results)}
        </table></td>'''

    return f"""<!DOCTYPE html><html><head><title>Model Comparison</title>
    <style>
        body{{font-family:monospace;background:#0d1520;color:#c8daea;padding:20px}}
        h2{{color:#00d4ff}} h3{{color:#39ff89;margin-bottom:6px}}
        table{{border-collapse:collapse}}
        th{{color:#00d4ff;text-align:left;padding:5px 10px;border-bottom:1px solid #1e3045}}
        td{{padding:5px 10px;border-bottom:1px solid #1e3045;white-space:nowrap}}
        .note{{color:#5a7a96;font-size:0.85rem;margin-top:8px}}
    </style></head><body>
    <h2>Wind Model Comparison — Hour 0</h2>
    <p class="note">lat={lat}, lon={lon} | SNE elev ≈ 315ft | grey = above 14,500ft MSL</p>
    <table><tr>{cols}{alt_col}</tr></table>
    </body></html>"""


@app.route("/clearcache")
def clearcache():
    token = request.args.get("token", "")
    expected = os.environ.get("CACHE_TOKEN", "mango")
    if token != expected:
        return jsonify({"error": "unauthorized"}), 403
    _forecast_cache.clear()
    return jsonify({"status": "cache cleared"})


@app.route("/data")
def data():
    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)
    hour = request.args.get("hour", 0, type=int)

    if lat is None or lon is None:
        return jsonify({"error": "lat and lon are required"}), 400

    # Get ICAO for this DZ if available
    icao = None
    for dz_vals in DROPZONES.values():
        if len(dz_vals) > 2 and abs(dz_vals[0]-lat) < 0.01 and abs(dz_vals[1]-lon) < 0.01:
            icao = dz_vals[2]
            break

    raw = fetch_forecast(lat, lon, hour)
    winds = format_winds(raw, hour, lat, lon)

    # Override SFC with live METAR for current conditions
    # If METAR returns calm (0kt) fall back to Open-Meteo lowest level
    if hour == 0 and icao and winds:
        metar = fetch_metar(icao)
        if metar and metar["wspd"] > 0:
            winds[0] = {
                "speed":     round(metar["wspd"], 1),
                "direction": round(metar["wdir"] % 360, 0),
                "arrow":     wind_arrow(metar["wdir"]),
                "color":     color(metar["wspd"]),
                "temp_f":    round(metar["temp"] * 9/5 + 32) if metar["temp"] is not None else None,
            }
            print(f"SFC: using METAR {icao} {metar['wspd']}kt/{metar['wdir']}°")
        else:
            print(f"SFC: METAR calm or unavailable, using Open-Meteo lowest level")

    if not winds:
        print(f"ERROR: winds empty for lat={lat} lon={lon} hour={hour}")
        resp = jsonify({"error": "Could not fetch forecast. Try again in 60 seconds."})
        resp.headers["Cache-Control"] = "no-store"
        return resp, 503

    canopy_speed, canopy_dir = avg_wind_display(winds, 0, 3001)
    free_speed, free_dir = avg_wind_display(winds, 4000, 14001)

    time_label = ""
    try:
        source = raw.get("source", "openmeteo") if raw else "openmeteo"
        if source == "schulze":
            vt = raw["data"].get("validtime", "")
            if vt:
                time_label = f"Valid at {vt}:00Z (local)"
        else:
            times = raw["data"]["hourly"]["time"]
            if hour < len(times):
                time_label = times[hour] + " (local)"
    except Exception:
        pass

    response = jsonify({
        "winds": winds,
        "canopy": {
            "speed": canopy_speed,
            "direction": canopy_dir,
            "glide_radius": canopy_data(canopy_speed, canopy_dir)[0],
            "wind_drift": canopy_data(canopy_speed, canopy_dir)[1],
            "wind_dir": canopy_dir,
        },
        "freefall": {
            "speed": free_speed,
            "direction": (free_dir + 180) % 360,
            "distance": freefall_distance(free_speed, free_dir),
        },
        "wind_14k": {
            "speed": winds.get(14000, {}).get("speed", 0),
            "direction": winds.get(14000, {}).get("direction", 0),
        },
        "time_label": time_label,
    })
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


# =====================================================
# FRONTEND
# =====================================================



@app.route("/")
def index():
    # Pass only lat/lon to frontend (strip ICAO)
    dz_frontend = {k: (v[0], v[1]) for k, v in DROPZONES.items()}
    return render_template("index.html", dz=dz_frontend)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
