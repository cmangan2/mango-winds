"""Run the app with a canned forecast (no network needed for weather) for browser tests.
Usage: python tests/mock_server.py [port]     (default port 5099)
Sets throwaway data files so nothing real is touched (MWH_TEST_DIR keeps them in one folder across restarts)."""
import os, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
tmp = os.environ.get("MWH_TEST_DIR") or tempfile.mkdtemp(prefix="mwh-test-")  # set it to reuse data files across restarts
os.environ["PREWARM"] = "0"  # no startup weather fetch in tests
os.environ["TRACK_RECORDER"] = "0"  # no live ADS-B polling in tests
for k, f in (("CACHE_FILE", "winds_cache.json"), ("TAILS_FILE", "tails.json"), ("LASTLOAD_FILE", "lastload.json"),
             ("ADSB_TRAILS_FILE", "adsb_trails.json"), ("RECORDED_TRACKS_FILE", "recorded_tracks.json"), ("VISITS_FILE", "visits.json"),
             ("API_LOG_FILE", "api_log.json"), ("DB_FILE", "mwh.db")):
    os.environ[k] = os.path.join(tmp, f)
os.environ.pop("DATABASE_URL", None)   # always the throwaway SQLite file
from py_vapid import Vapid01, b64urlencode
from cryptography.hazmat.primitives import serialization
_v = Vapid01(); _v.generate_keys()     # throwaway push keys so the alert UI shows
os.environ["VAPID_PRIVATE_KEY"] = b64urlencode(_v.private_key.private_numbers().private_value.to_bytes(32, "big"))
os.environ["VAPID_PUBLIC_KEY"] = b64urlencode(_v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
import mangowinds as m

def mk(w10, w80, lv):
    h = {"time": ["t"] * 72}
    for k, v in (("windspeed_10m", w10[0]), ("winddirection_10m", w10[1]), ("windspeed_80m", w80[0]),
                 ("winddirection_80m", w80[1]), ("temperature_2m", 15)):
        h[k] = [v] * 72
    for l in (1000, 975, 950, 925, 850, 700, 600, 500):
        sp, d = lv.get(l, (12, 250))
        h[f"windspeed_{l}hPa"] = [sp] * 72; h[f"winddirection_{l}hPa"] = [d] * 72; h[f"temperature_{l}hPa"] = [10] * 72
        h[f"geopotential_height_{l}hPa"] = [{1000: 110, 975: 330, 950: 553, 925: 782, 850: 1496, 700: 3000, 600: 4200, 500: 5500}[l]] * 72
    return {"hourly": h, "elevation": 76}

A = mk((2, 330), (4, 326), {1000: (3, 328), 975: (4, 327), 950: (5, 329), 925: (9, 230)})
B = mk((1, 256), (1, 259), {1000: (2, 249), 975: (2, 249), 950: (3, 247), 925: (9, 232)})
C = mk((3, 214), (3, 216), {1000: (3, 214), 975: (3, 215), 950: (3, 217), 925: (9, 228)})
m.fetch_forecast = lambda *a: {"source": "openmeteo_ensemble", "models": {"gfs_seamless": A, "hrrr_conus": B, "icon_seamless": C}}
m.fetch_metar = lambda icao: None
m.fetch_cloud_data = lambda *a, **k: None
m.record_api_call = lambda *a, **k: None

# Sign-in emails and pushes are captured instead of sent; tests read them back here
_sent = {"codes": {}, "pushes": []}
def _fake_email(to, code, link):
    _sent["codes"][to] = {"code": code, "link": link}
    return True
m.send_login_email = _fake_email
def _fake_push(rows, payload):
    _sent["pushes"].append({"endpoints": [r[0] for r in rows], "payload": payload})
    return len(rows)
m.push_send = _fake_push

@m.app.route("/_test/sent")
def _test_sent():
    return m.jsonify(_sent)

@m.app.route("/_test/airborne", methods=["POST"])
def _test_airborne():
    b = m.request.get_json()
    m._tails_log.setdefault(b["dz"], []).append(b["tail"])
    m._push_last_alert.clear()
    m.notify_airborne(b["tail"], 2300)
    return m.jsonify({"ok": True})

m.app.run(port=int(sys.argv[1]) if len(sys.argv) > 1 else 5099)
