from flask import Flask, render_template_string, jsonify, request
import requests
import math
from datetime import datetime

app = Flask(__name__)

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
                name, coords = line.split(":")
                lat, lon = coords.split(",")
                dz[name.strip()] = (float(lat), float(lon))
    except:
        dz = {"default DZ": (43.3712, -70.9259)}

    return dz


DROPZONES = load_dropzones()

# =====================================================
# 🌐 FORECAST
# =====================================================

def fetch_forecast(lat, lon):
    url = "https://api.open-meteo.com/v1/forecast"

    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": [
            "windspeed_1000hPa","winddirection_1000hPa",
            "windspeed_925hPa","winddirection_925hPa",
            "windspeed_850hPa","winddirection_850hPa",
            "windspeed_700hPa","winddirection_700hPa",
            "windspeed_600hPa","winddirection_600hPa",
            "windspeed_500hPa","winddirection_500hPa"
        ],
        "forecast_days": 3,
        "timezone": "auto"
    }

    try:
        r = requests.get(url, timeout=10, params=params)
        r.raise_for_status()
        return r.json()
    except:
        return None


# =====================================================
# 📊 WIND MODEL
# =====================================================

def wind_arrow(d):
    return ["↑","↗","→","↘","↓","↙","←","↖","↑"][int((d % 360)/45)]

def color(s):
    if s < 10: return "green"
    if s < 25: return "orange"
    return "red"


def interpolate(base, alt):
    for i in range(len(base)-1):
        a0,s0,d0 = base[i]
        a1,s1,d1 = base[i+1]

        if a0 <= alt <= a1:
            t = (a0 - alt) / (a0 - a1) if a1 != a0 else 0
            return (
                s0 + (s1 - s0) * (1 - t),
                d0 + (d1 - d0) * (1 - t)
            )

    return base[-1][1], base[-1][2]


def format_winds(data, hour):
    if not data:
        return {}

    h = data["hourly"]

    base = [
        (0,    h["windspeed_1000hPa"][hour], h["winddirection_1000hPa"][hour]),
        (1000, h["windspeed_925hPa"][hour],  h["winddirection_925hPa"][hour]),
        (5000, h["windspeed_850hPa"][hour],  h["winddirection_850hPa"][hour]),
        (8000, h["windspeed_700hPa"][hour],  h["winddirection_700hPa"][hour]),
        (12000,h["windspeed_600hPa"][hour],  h["winddirection_600hPa"][hour]),
        (14000,h["windspeed_500hPa"][hour],  h["winddirection_500hPa"][hour]),
    ]

    result = {}

    for alt in range(0, 15000, 1000):
        speed, direction = interpolate(base, alt)

        result[alt] = {
            "speed": round(speed, 1),
            "direction": round(direction % 360, 0),
            "arrow": wind_arrow(direction),
            "color": color(speed)
        }

    return result


# =====================================================
# 🧠 SIMPLE AVERAGING
# =====================================================

def avg_wind_display(winds, low, high):
    speeds = []
    dirs = []

    for alt in sorted(winds.keys()):
        if low <= alt < high:
            w = winds[alt]
            speeds.append(w["speed"])
            dirs.append(w["direction"])

    if not speeds:
        return 0, 0

    avg_speed = sum(speeds) / len(speeds)

    avg_dir = math.degrees(
        math.atan2(
            sum(math.sin(math.radians(d)) for d in dirs),
            sum(math.cos(math.radians(d)) for d in dirs)
        )
    ) % 360

    return avg_speed, avg_dir


# =====================================================
# 🚀 PHYSICS
# =====================================================

def canopy_distance(wind_speed, wind_dir):
    seconds = 180
    wind_ms = wind_speed * 0.514

    r = math.radians(wind_dir)

    wx = wind_ms * math.cos(r)
    wy = wind_ms * math.sin(r)

    wind_strength = math.sqrt(wx*wx + wy*wy)
    effective_speed = wind_strength * 1.25

    return effective_speed * seconds


def freefall_distance(wind_speed, wind_dir):
    seconds = 60
    wind_ms = wind_speed * 0.514

    r = math.radians(wind_dir)

    wx = wind_ms * math.cos(r)
    wy = wind_ms * math.sin(r)

    drift_speed = math.sqrt(wx*wx + wy*wy)

    return drift_speed * seconds


# =====================================================
# API
# =====================================================

@app.route("/data")
def data():
    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)
    hour = request.args.get("hour", 0, type=int)

    raw = fetch_forecast(lat, lon)
    winds = format_winds(raw, hour)

    canopy_speed, canopy_dir = avg_wind_display(winds, 0, 5000)
    free_speed, free_dir = avg_wind_display(winds, 4000, 15000)

    return jsonify({
        "winds": winds,
        "canopy": {
            "speed": canopy_speed,
            "direction": canopy_dir,
            "distance": canopy_distance(canopy_speed, canopy_dir)
        },
        "freefall": {
            "speed": free_speed,
            "direction": free_dir,
            "distance": freefall_distance(free_speed, free_dir)
        }
    })


# =====================================================
# FRONTEND
# =====================================================

@app.route("/")
def index():
    return render_template_string("""
<!DOCTYPE html>
<html>
<head>
<title>Mango Wind Hub</title>

<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.3/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.3/dist/leaflet.js"></script>

<style>
body { margin:0; font-family:Arial; background:#0b0f14; color:white; }
#wrap { display:flex; height:100vh; }
#map { flex:1; }

#panel {
    width:280px;
    background:#121a24;
    padding:12px;
    overflow:auto;
}

.card {
    background:#1a2432;
    padding:8px;
    margin:6px 0;
    border-radius:10px;
}

.block {
    background:#0f1620;
    padding:10px;
    margin:10px 0;
    border-radius:10px;
}

label { color:#bbb; display:block; margin-top:10px; }

#hourLabel, #timeLabel { color:#aaa; margin:8px 0; }
</style>

</head>

<body>

<div id="wrap">

<div id="map"></div>

<div id="panel">

<h2>🪂 Mango Wind Hub</h2>

<label>Select DZ</label>
<select id="dz"></select>

<div id="timeLabel"></div>
<input type="range" min="0" max="72" value="0" id="hour">
<div id="hourLabel">+0h</div>

<div class="block" id="canopyBlock"></div>
<div class="block" id="freefallBlock"></div>

<div id="cards"></div>

</div>
</div>

<script>

let dz = {{ dz | tojson }};
let lat, lon;

let map;
let marker;
let lowLine;
let highLine;

let loadTimeout = null;

// ==============================
// 🧭 LIVE TIME LABEL
// ==============================
function renderTime(){
    let hour = document.getElementById("hour").value;
    document.getElementById("hourLabel").innerText = `+${hour}h`;
}

// ==============================
// 🌍 VECTOR
// ==============================
function vec(distance, dir, color){
    let r = dir * Math.PI/180;
    let len = Math.min(distance, 6000);

    let dlat = Math.cos(r) * len / 111000;
    let dlon = Math.sin(r) * len / 111000;

    return L.polyline([[lat,lon],[lat+dlat,lon+dlon]],{
        color:color,
        weight:4
    }).addTo(map);
}

// ==============================
// 🗺️ 2 MILE ZOOM (NEW)
// ==============================
function fitTwoMileRadius(){
    const miles = 2;
    const meters = miles * 1609.344;

    const earthRadius = 6378137;

    const dLat = (meters / earthRadius) * (180 / Math.PI);
    const dLon = dLat / Math.cos(lat * Math.PI / 180);

    const bounds = L.latLngBounds(
        [lat - dLat, lon - dLon],
        [lat + dLat, lon + dLon]
    );

    map.fitBounds(bounds, { animate: true });
}

// ==============================
// 🔄 LOAD
// ==============================
async function load(){

    let hour = document.getElementById("hour").value;

    let r = await fetch(`/data?lat=${lat}&lon=${lon}&hour=${hour}`);
    let d = await r.json();

    if(marker) marker.remove();
    marker = L.marker([lat,lon]).addTo(map);

    if(lowLine) lowLine.remove();
    if(highLine) highLine.remove();

    lowLine = vec(d.canopy.distance, d.canopy.direction, "red");
    highLine = vec(d.freefall.distance, d.freefall.direction, "green");

    document.getElementById("canopyBlock").innerHTML =
        `<b style="color:red">Canopy Wind 0ft-4Kft</b><br>
        Speed: ${d.canopy.speed.toFixed(1)} kt<br>
        Direction: ${d.canopy.direction.toFixed(0)}°<br>
        Distance: ${(d.canopy.distance / 1609.344).toFixed(2)} mi`;

    document.getElementById("freefallBlock").innerHTML =
        `<b style="color:green">Freefall Wind 4Kft-14Kft</b><br>
        Speed: ${d.freefall.speed.toFixed(1)} kt<br>
        Direction: ${d.freefall.direction.toFixed(0)}°<br>
        Distance: ${(d.freefall.distance / 1609.344).toFixed(2)} mi`;

    let html = "";

    for(let a in d.winds){

        let w = d.winds[a];

        let flippedDir = (w.direction + 180) % 360;
        let arrowSymbol = ["↑","↗","→","↘","↓","↙","←","↖","↑"][Math.floor(flippedDir / 45)];

        html += `<div class="card">
            ${a} ft<br>
            ${arrowSymbol} ${w.speed} kt<br>
            FROM: ${w.direction.toFixed(0)}°
        </div>`;
    }

    document.getElementById("cards").innerHTML = html;
}

// ==============================
// 🗺️ INIT MAP
// ==============================
function initMap(){
    map = L.map('map').setView([lat,lon],9);

    L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
        attribution: 'Tiles © Esri'
    }).addTo(map);
}

// ==============================
// 🪂 INIT DZ
// ==============================
function initDZ(){

    let sel = document.getElementById("dz");
    let keys = Object.keys(dz);

    let defaultDz =
        keys.find(k => k.toLowerCase().includes("skydive new england"))
        || keys[0];

    for(let k of keys){
        let o = document.createElement("option");
        o.value = k;
        o.text = k;
        sel.appendChild(o);
    }

    sel.value = defaultDz;
    lat = dz[defaultDz][0];
    lon = dz[defaultDz][1];

    sel.onchange = ()=>{
        let v = sel.value;
        lat = dz[v][0];
        lon = dz[v][1];

        fitTwoMileRadius();
        load();
    };

    initMap();
    fitTwoMileRadius();
    load();

    document.getElementById("hour").oninput = () => {

        renderTime();

        clearTimeout(loadTimeout);
        loadTimeout = setTimeout(load, 400);
    };

    renderTime();
}

initDZ();

</script>

</body>
</html>
""", dz=DROPZONES)


# =====================================================
# RUN
# =====================================================

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
