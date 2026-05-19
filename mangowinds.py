from flask import Flask, render_template, jsonify, request
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


def fetch_forecast(lat, lon, hour_offset=0):
    # Cache key is per-DZ only (not per hour) since Schulze returns all hours
    dz_key = (round(lat, 3), round(lon, 3))
    now = datetime.now(timezone.utc)

    cached = _forecast_cache.get(dz_key)
    if cached and cached["expires"] > now:
        print(f"Cache HIT for {dz_key}")
        return cached["data"]

    result = fetch_schulze(lat, lon, hour_offset)
    if result:
        _forecast_cache[dz_key] = {"data": result, "expires": now + CACHE_TTL}
        return result

    print("Schulze unavailable — falling back to Open-Meteo")
    try:
        api_key = os.environ.get("OPENMETEO_API_KEY")
        url = "https://customer-api.open-meteo.com/v1/forecast" if api_key else "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat, "longitude": lon,
            "hourly": [
                "windspeed_10m", "winddirection_10m",
                "windspeed_925hPa", "winddirection_925hPa",
                "windspeed_850hPa", "winddirection_850hPa",
                "windspeed_700hPa", "winddirection_700hPa",
                "windspeed_600hPa", "winddirection_600hPa",
            ],
            "forecast_days": 3, "timezone": "auto", "wind_speed_unit": "kn",
        }
        if api_key:
            params["apikey"] = api_key
        r = requests.get(url, timeout=15, params=params,
                         headers={"User-Agent": "MangoWindHub/1.0"})
        r.raise_for_status()
        data = r.json()
        result = {"source": "openmeteo", "data": data}
        _forecast_cache[dz_key] = {"data": result, "expires": now + CACHE_TTL}
        print(f"Open-Meteo fallback OK for {dz_key}")
        return result
    except Exception as e:
        print(f"Open-Meteo fallback error: {e}")
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


def format_winds(data, hour):
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

        # Open-Meteo fallback — no temp data available
        h = data["data"]["hourly"]
        pressure_levels = [
            (2500,  h["windspeed_925hPa"][hour],  h["winddirection_925hPa"][hour]),
            (4800,  h["windspeed_850hPa"][hour],  h["winddirection_850hPa"][hour]),
            (9843,  h["windspeed_700hPa"][hour],  h["winddirection_700hPa"][hour]),
            (14764, h["windspeed_600hPa"][hour],  h["winddirection_600hPa"][hour]),
        ]
        surf_speed = h["windspeed_10m"][hour]
        surf_dir   = h["winddirection_10m"][hour]
        base = [(0, surf_speed, surf_dir)] + pressure_levels
        result = {}
        result[0] = {
            "speed":     round(surf_speed, 1),
            "direction": round(surf_dir % 360, 0),
            "arrow":     wind_arrow(surf_dir),
            "color":     color(surf_speed),
            "temp_f":    None,
        }
        for alt in range(1000, 15000, 1000):
            speed, direction = interpolate(base, alt)
            result[alt] = {
                "speed":     round(speed, 1),
                "direction": round(direction % 360, 0),
                "arrow":     wind_arrow(direction),
                "color":     color(speed),
                "temp_f":    None,
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
    speeds = []
    sin_sum = 0.0
    cos_sum = 0.0
    for alt in sorted(winds.keys()):
        if low <= alt < high:
            w = winds[alt]
            spd = w["speed"]
            speeds.append(spd)
            r = math.radians(w["direction"])
            # Weight by speed so a 40kt wind dominates over a 2kt wind
            sin_sum += math.sin(r) * spd
            cos_sum += math.cos(r) * spd
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

    raw = fetch_forecast(lat, lon, hour)
    winds = format_winds(raw, hour)

    if not winds:
        print(f"ERROR: winds empty for lat={lat} lon={lon} hour={hour}")
        resp = jsonify({"error": "Could not fetch forecast. Try again in 60 seconds."})
        resp.headers["Cache-Control"] = "no-store"
        return resp, 503

    canopy_speed, canopy_dir = avg_wind_display(winds, 0, 3000)
    free_speed, free_dir = avg_wind_display(winds, 4000, 14000)

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
    return render_template("index.html", dz=DROPZONES)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
