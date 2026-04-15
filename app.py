import requests
from bs4 import BeautifulSoup
import pandas as pd
import time
import json
import os

BASE_URL = "https://www.markschulze.net/winds/"
CACHE_FILE = "winds_cache.json"
CACHE_TTL = 900  # seconds (15 min)

# -----------------------------
# Helpers
# -----------------------------

def direction_to_arrow(deg):
    dirs = [
        (22.5, "↑"), (67.5, "↗"), (112.5, "→"),
        (157.5, "↘"), (202.5, "↓"), (247.5, "↙"),
        (292.5, "←"), (337.5, "↖"), (360, "↑")
    ]
    for threshold, arrow in dirs:
        if deg <= threshold:
            return arrow
    return "?"

def speed_color(speed):
    if speed < 5:
        return "green"
    elif speed < 15:
        return "orange"
    else:
        return "red"

def altitude_group(alt):
    alt = int(alt)
    if alt <= 3000:
        return "0–3k"
    elif alt <= 6000:
        return "3–6k"
    elif alt <= 9000:
        return "6–9k"
    elif alt <= 12000:
        return "9–12k"
    else:
        return "12–14k"

# -----------------------------
# Cache
# -----------------------------

def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r") as f:
            data = json.load(f)
            if time.time() - data["timestamp"] < CACHE_TTL:
                print("Using cached data...")
                return data["data"]
    return None

def save_cache(data):
    with open(CACHE_FILE, "w") as f:
        json.dump({
            "timestamp": time.time(),
            "data": data
        }, f)

# -----------------------------
# Scraper
# -----------------------------

def fetch_data():
    print("Fetching fresh data...")
    resp = requests.get(BASE_URL)
    soup = BeautifulSoup(resp.text, "html.parser")

    dropzones = []

    tables = soup.find_all("table")

    for table in tables:
        title = table.find_previous("h2")
        if not title:
            continue

        dz_name = title.text.strip()
        rows = table.find_all("tr")

        for row in rows[1:]:
            cols = row.find_all("td")
            if len(cols) < 3:
                continue

            altitude = cols[0].text.strip()
            direction = cols[1].text.strip()
            speed = cols[2].text.strip()

            try:
                altitude_val = int(altitude.replace("ft", "").strip())
                direction_val = int(direction)
                speed_val = int(speed)
            except:
                continue

            dropzones.append({
                "Dropzone": dz_name,
                "Altitude": altitude_val,
                "Group": altitude_group(altitude_val),
                "Direction": direction_val,
                "Arrow": direction_to_arrow(direction_val),
                "Speed": speed_val,
                "Color": speed_color(speed_val)
            })

    return dropzones

# -----------------------------
# Output
# -----------------------------

def export_csv(data):
    df = pd.DataFrame(data)
    df.sort_values(["Dropzone", "Altitude"], inplace=True)
    df.to_csv("winds.csv", index=False)
    print("Saved winds.csv")

def export_html(data):
    html = """
    <html>
    <head>
    <style>
    body { font-family: Arial; }
    table { border-collapse: collapse; margin-bottom: 20px; }
    td, th { border: 1px solid #ccc; padding: 6px; text-align: center; }
    .green { background-color: #b6fcb6; }
    .orange { background-color: #ffe0a3; }
    .red { background-color: #ffb3b3; }
    </style>
    </head>
    <body>
    <h1>Skydiving Winds Briefing</h1>
    """

    grouped = {}
    for row in data:
        grouped.setdefault(row["Dropzone"], []).append(row)

    for dz, rows in grouped.items():
        html += f"<h2>{dz}</h2><table>"
        html += "<tr><th>Alt</th><th>Dir</th><th>Speed</th></tr>"

        for r in sorted(rows, key=lambda x: x["Altitude"]):
            html += f"""
            <tr class="{r['Color']}">
                <td>{r['Altitude']}</td>
                <td>{r['Arrow']} ({r['Direction']}°)</td>
                <td>{r['Speed']} kt</td>
            </tr>
            """

        html += "</table>"

    html += "</body></html>"

    with open("winds.html", "w") as f:
        f.write(html)

    print("Saved winds.html (printable briefing)")

# -----------------------------
# Main
# -----------------------------

def main():
    data = load_cache()
    if not data:
        data = fetch_data()
        save_cache(data)

    export_csv(data)
    export_html(data)

    print("\nDone.")

if __name__ == "__main__":
    main()
