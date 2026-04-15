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
        dz = {"Default DZ": (43.3712, -70.9259)}

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
            t = (alt-a0)/(a1-a0)
            return (
                s0 + (s1-s0)*t,
                d0 + (d1-d0)*t
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
            "speed": round(speed,1),
            "direction": round(direction % 360,0),
            "arrow": wind_arrow(direction),
            "color": color(speed)
        }

    return result


# =====================================================
# 🧠 VECTOR AVG
# =====================================================

def avg_vector(winds, low, high):

    u = 0
    v = 0
    count = 0

    for alt, w in winds.items():

        if low <= alt < high:

            r = math.radians(w["direction"])
            u += w["speed"] * math.cos(r)
            v += w["speed"] * math.sin(r)
            count += 1

    if count == 0:
        return 0, 0

    u /= count
    v /= count

    speed = math.sqrt(u*u + v*v)
    direction = (math.degrees(math.atan2(v, u)) + 360) % 360

    return speed, direction


# =====================================================
# 🚀 PHYSICS FIXED MODELS (TRUE VECTOR GLIDE)
# =====================================================

def canopy_distance(wind_speed, wind_dir):

    seconds = 180

    # 2:1 glide ratio ~ forward airspeed ≈ 12 m/s, sink ≈ 6 m/s
    air_forward = 12.0
    sink_rate = 6.0

    wind = wind_speed * 0.514

    # glide direction assumed downwind adjusted
    glide_dir = math.radians(wind_dir)

    # canopy air vector
    u_air = air_forward * math.cos(glide_dir)
    v_air = air_forward * math.sin(glide_dir)

    # wind vector
    u_wind = wind * math.cos(glide_dir)
    v_wind = wind * math.sin(glide_dir)

    u = u_air + u_wind
    v = v_air + v_wind

    ground_speed = math.sqrt(u*u + v*v)

    return ground_speed * seconds


def freefall_distance(wind_speed, wind_dir):

    seconds = 60

    fall_speed = 120 * 0.44704
    wind = wind_speed * 0.514

    r = math.radians(wind_dir)

    u = fall_speed + wind * math.cos(r)
    v = wind * math.sin(r)

    ground_speed = math.sqrt(u*u + v*v)

    return ground_speed * seconds


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

    canopy_speed, canopy_dir = avg_vector(winds, 0, 4000)
    free_speed, free_dir = avg_vector(winds, 4000, 14001)

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
    width:420px;
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

function formatTime(){
    const d = new Date();
    let h = d.getHours();
    let m = d.getMinutes();
    let ampm = h >= 12 ? "PM" : "AM";
    h = h % 12;
    h = h ? h : 12;
    m = m.toString().padStart(2, "0");
    return `${h}:${m} ${ampm}`;
}

const pageLoadTime = formatTime();

document.addEventListener("DOMContentLoaded", () => {
    document.getElementById("timeLabel").innerText =
        `Forecast Time: ${pageLoadTime}`;
});

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

async function load(){

    let hour = document.getElementById("hour").value;
    document.getElementById("hourLabel").innerText = `+${hour}h`;

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
        Direction: ${d.canopy.direction.toFixed(0)}°`;

    document.getElementById("freefallBlock").innerHTML =
        `<b style="color:green">Freefall Wind 4Kft-14Kft</b><br>
        Speed: ${d.freefall.speed.toFixed(1)} kt<br>
        Direction: ${d.freefall.direction.toFixed(0)}°`;

    let html = "";

    for(let a in d.winds){

        let w = d.winds[a];
        let pushDir = (w.direction + 180) % 360;
        let arrowSymbol = ["↑","↗","→","↘","↓","↙","←","↖","↑"][Math.floor(pushDir/45)];

        html += `<div class="card">
            ${a} ft<br>
            ${arrowSymbol} ${w.speed} kt<br>
            FROM: ${w.direction.toFixed(0)}°
        </div>`;
    }

    document.getElementById("cards").innerHTML = html;
}

function initMap(){
    map = L.map('map').setView([lat,lon],9);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png').addTo(map);
}

function initDZ(){

    let sel = document.getElementById("dz");
    let defaultKey = null;

    for(let k in dz){
        let o = document.createElement("option");
        o.value = k;
        o.text = k;
        sel.appendChild(o);

        if(k.toLowerCase().includes("skydive new england")){
            defaultKey = k;
        }
    }

    let first = defaultKey || Object.keys(dz)[0];

    sel.value = first;
    lat = dz[first][0];
    lon = dz[first][1];

    sel.onchange = ()=>{
        let v = sel.value;
        lat = dz[v][0];
        lon = dz[v][1];
        load();
    };

    initMap();

    load();

    document.getElementById("hour").oninput = load;
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