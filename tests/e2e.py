"""Browser smoke tests (Playwright). Starts the mock server itself.
    pip install -r requirements-dev.txt && playwright install chromium
    python tests/e2e.py
If the machine can't reach the Leaflet CDN, point LEAFLET_DIR at a folder containing leaflet.js and leaflet.css."""
import json, os, subprocess, sys, time, urllib.parse, urllib.request
from playwright.sync_api import sync_playwright

PORT = 5099
URL = f"http://localhost:{PORT}/"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.stdout.reconfigure(encoding="utf-8")   # push titles carry emoji; the Windows console default (cp1252) chokes on them
LEAF = os.environ.get("LEAFLET_DIR")
results = []

def check(name, ok, extra=""):
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + name + (f"  {extra}" if extra else ""))

def new_page(browser, w=1000, h=800, touch=False):
    ctx = browser.new_context(viewport={"width": w, "height": h}, has_touch=touch)
    pg = ctx.new_page(); errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)[:300]))
    def route(r):
        u = r.request.url
        if f"localhost:{PORT}" in u: return r.continue_()
        if LEAF and "leaflet" in u and u.endswith(".js"): return r.fulfill(body=open(os.path.join(LEAF, "leaflet.js")).read(), content_type="application/javascript")
        if LEAF and "leaflet" in u and u.endswith(".css"): return r.fulfill(body=open(os.path.join(LEAF, "leaflet.css")).read(), content_type="text/css")
        if LEAF:   # offline: stub everything else external
            if u.endswith(".js"): return r.fulfill(body="", content_type="application/javascript")
            if u.endswith(".css") or "fonts.googleapis" in u: return r.fulfill(body="", content_type="text/css")
            return r.abort()
        return r.continue_()
    pg.route("**/*", route)
    pg.goto(URL); pg.wait_for_timeout(2500); pg.evaluate("closeWelcome()"); pg.wait_for_timeout(400)
    return ctx, pg, errs

def start_server(port, env=None):
    srv = subprocess.Popen([sys.executable, os.path.join(HERE, "mock_server.py"), str(port)], env={**os.environ, **(env or {})},
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        try: urllib.request.urlopen(f"http://localhost:{port}/tails?dz=x", timeout=1); break
        except Exception: time.sleep(0.25)
    return srv

def persistence_tests():
    """Plane lists live in the database: they survive a restart, and an old tails.json is imported once."""
    import tempfile
    port, d = PORT + 1, tempfile.mkdtemp(prefix="mwh-persist-")
    with open(os.path.join(d, "tails.json"), "w") as f:
        json.dump({"Old DZ": ["N111OLD"]}, f)
    get = lambda dz: json.loads(urllib.request.urlopen(f"http://localhost:{port}/tails?dz=" + urllib.parse.quote(dz)).read())
    srv = start_server(port, {"MWH_TEST_DIR": d})
    try:
        check("tails.json imported into the database on first start", get("Old DZ") == ["N111OLD"])
        urllib.request.urlopen(urllib.request.Request(f"http://localhost:{port}/tails", data=json.dumps({"dz": "Test DZ", "tail": "n222mw"}).encode(),
                                                      headers={"Content-Type": "application/json"}))
    finally:
        srv.terminate(); srv.wait()
    os.remove(os.path.join(d, "tails.json"))     # a deploy wipes the disk; only the database is left
    srv = start_server(port, {"MWH_TEST_DIR": d})
    try:
        check("added tail still listed after a restart", get("Test DZ") == ["N222MW"] and get("Old DZ") == ["N111OLD"])
    finally:
        srv.terminate(); srv.wait()

def main():
    persistence_tests()
    srv = subprocess.Popen([sys.executable, os.path.join(HERE, "mock_server.py"), str(PORT)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(40):
            try: urllib.request.urlopen(URL, timeout=1); break
            except Exception: time.sleep(0.25)
        with sync_playwright() as p:
            b = p.chromium.launch()

            # 1. loads, draws main + reserve polygons, no JS errors
            ctx, pg, errs = new_page(b)
            check("page loads without JS errors", not errs, str(errs))
            check("green polygon + reserve polygon drawn", pg.evaluate("!!canopyCircleShifted && !!reserveCircle"))

            # Renamed DZ: an old name saved on the device opens the renamed entry (not the default DZ)
            pg.evaluate("localStorage.setItem('mwh-dz', 'Boston Skydive Center (Smithfield, RI USA)')")
            pg.reload(); pg.wait_for_timeout(2500); pg.evaluate("closeWelcome()")
            check("saved old DZ name opens the renamed DZ", pg.evaluate("document.getElementById('dz').value") == "Boston Skydive Center (Smithfield, RI)")
            pg.evaluate("localStorage.removeItem('mwh-dz')"); pg.reload(); pg.wait_for_timeout(2500); pg.evaluate("closeWelcome()")

            # 2. Canopy Settings persist across reload, Reset clears them
            pg.evaluate("openAdvancedModal()"); pg.fill("#advWeight", "200"); pg.fill("#advSize", "170"); pg.fill("#advResSize", "126")
            pg.evaluate("applyAdvancedSettings()"); pg.wait_for_timeout(800)
            pg.reload(); pg.wait_for_timeout(2500); pg.evaluate("closeWelcome()")
            check("canopy settings restored after reload", pg.evaluate("[advWeight.value, advSize.value, advResSize.value]") == ["200", "170", "126"])
            pg.evaluate("resetCanopySettings()"); pg.wait_for_timeout(800)
            check("reset restores defaults", pg.evaluate("[advWeight.value, advSize.value, advResSize.value]") == ["240", "150", "143"])

            # 3. Defined jump run shows info box; box is draggable
            pg.click("#defineBtn"); pg.fill("#dfDir", "270"); pg.fill("#dfLength", "2"); pg.evaluate("submitDefineJumpRun()"); pg.wait_for_timeout(800)
            el = pg.locator('.leaflet-marker-pane div:has-text("JUMP RUN")').last
            bb = el.bounding_box(); x, y = bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2
            pg.mouse.move(x, y); pg.mouse.down(); pg.mouse.move(x + 60, y - 50, steps=6); pg.mouse.up(); pg.wait_for_timeout(200)
            bb2 = el.bounding_box()
            check("jump run box drags", round(bb2["x"] - bb["x"]) == 60 and round(bb2["y"] - bb["y"]) == -50)
            # Freefall drift is a 40–65 s box; jumper boxes land on its 52.5 s centerline
            fb = pg.evaluate("""(() => { const box = freefallLine.find(l => l instanceof L.Polygon), ll = box.getLatLngs()[0];
                const v = p => [(p.lng - jrStart[1]) * 111000 * Math.cos(jrStart[0]*Math.PI/180), (p.lat - jrStart[0]) * 111000];
                const s = ffDriftM(1), near = Math.hypot(...v(ll[0])), far = Math.hypot(...v(ll[3])), per = Math.hypot(...s);
                return [near / per, far / per, Math.hypot(...ffDriftM(FF_MID_S)) / per]; })()""")
            wv = pg.evaluate("[wsBalloonVal.innerText, (lastFreefall.speed*0.514444*45/1609.344).toFixed(2), (lastFreefall.speed*0.514444*60/1609.344).toFixed(2)]")
            check("wind widget shows freefly 45 s + belly 60 s drift", f"FF {wv[1]} mi" in wv[0] and f"Belly {wv[2]} mi" in wv[0], str(wv))
            check("drift box spans 40-65 s, landings at 52.5 s", [round(x, 1) for x in fb] == [40.0, 65.0, 52.5], str(fb))
            pg.evaluate("clearJumpRun()")

            # 4. Suggested Jump Run produces a run on several headings
            pg.click("#suggestBtn"); ok = True; snapped = True
            for d in (0, 90, 200, 300):
                pg.fill("#sgDir", str(d)); pg.click("#sgSubmitBtn"); pg.wait_for_timeout(400)
                ok &= pg.evaluate("!!jrStart && !!jrEnd")
                # Prior/Past and Offset of the start land on a 0.1 mi grid
                snap = pg.evaluate("""(() => { const c = Math.cos(lat*Math.PI/180), h = bearing(jrStart, jrEnd)*Math.PI/180;
                    const dx = (jrStart[1]-lon)*111000*c/1609.344, dy = (jrStart[0]-lat)*111000/1609.344;
                    return [dx*Math.sin(h)+dy*Math.cos(h), dx*Math.cos(h)-dy*Math.sin(h)]; })()""")
                snapped &= all(abs(v * 10 - round(v * 10)) < 0.02 for v in snap)
                pg.evaluate("clearJumpRun();openSuggestModal()")
            check("suggested jump run works for 4 headings", ok)
            check("suggested prior/past and offset snap to 0.1 mi", snapped)

            # Landing pattern: placing a spot opens the popup; checkpoints are configurable, validated and saved
            pg.evaluate("closeSuggestModal(); clearJumpRun(); onPatternBtn()")
            box = pg.locator("#map").bounding_box(); pg.mouse.click(box["x"] + box["width"] * 0.4, box["y"] + box["height"] * 0.5); pg.wait_for_timeout(400)
            check("placing a landing spot opens the pattern popup", pg.evaluate("patternModal.style.display") == "flex" and pg.evaluate("!!landingSpot"))
            labels = "Array.from(document.querySelectorAll('.leaflet-marker-pane div')).filter(d=>!d.children.length).map(d=>d.textContent).filter(t=>/^\\d+ft$/.test(t))"
            check("default checkpoints 1000/600/300", pg.evaluate(labels) == ["1000ft", "600ft", "300ft"], str(pg.evaluate(labels)))
            pg.fill("#patternAltDownwind", "900"); pg.fill("#patternAltBase", "500"); pg.fill("#patternAltFinal", "250"); pg.wait_for_timeout(300)
            check("custom checkpoints redraw the pattern", pg.evaluate(labels) == ["900ft", "500ft", "250ft"], str(pg.evaluate(labels)))
            pg.fill("#patternAltBase", "950"); pg.wait_for_timeout(200)
            check("out-of-order checkpoints rejected", pg.evaluate("patternError.style.display") == "block" and pg.evaluate("patternAlts.base") == 500)
            pg.fill("#patternAltBase", "500"); pg.evaluate("closePatternModal()")
            check("checkpoints saved on the device", pg.evaluate("JSON.parse(localStorage.getItem('mwh-pattern-settings'))") == {"downwind": 900, "base": 500, "final": 250})
            pg.click("#patternBtn"); pg.wait_for_timeout(200)
            check("drawer button reopens the popup", pg.evaluate("patternModal.style.display") == "flex" and pg.input_value("#patternAltFinal") == "250")
            hb = pg.locator("#patternHeader").bounding_box(); b0 = pg.locator("#patternBox").bounding_box()
            pg.mouse.move(hb["x"] + 60, hb["y"] + hb["height"] / 2); pg.mouse.down(); pg.mouse.move(hb["x"] + 20, hb["y"] + hb["height"] / 2 - 250, steps=6); pg.mouse.up(); pg.wait_for_timeout(150)
            b1 = pg.locator("#patternBox").bounding_box()
            check("pattern popup drags by its header", round(b1["x"] - b0["x"]) == -40 and round(b1["y"] - b0["y"]) == -250, f"{b0} -> {b1}")
            pg.evaluate("closePatternModal(); openPatternModal()")
            check("pattern popup reopens where it was moved", round(pg.locator("#patternBox").bounding_box()["y"]) == round(b1["y"]))
            pg.evaluate("clearPattern(); closePatternModal()")

            # Tracked plane row in the drawer shows heading (3 digits, north = 360, none on the ground)
            row = pg.evaluate("""(() => { trackedPlanes['N1TEST'] = { color:'#00d4ff', trail:[], data:{ alt:9500, spd:105, hdg:45, vert:1200 } };
                updateMultiPlaneInfoPanel(); const a = planeInfo.innerText;
                trackedPlanes['N1TEST'].data.hdg = 0; updateMultiPlaneInfoPanel(); const b = planeInfo.innerText;
                trackedPlanes['N1TEST'].data.on_ground = true; updateMultiPlaneInfoPanel(); const c = planeInfo.innerText;
                delete trackedPlanes['N1TEST']; updateMultiPlaneInfoPanel(); return [a, b, c]; })()""")
            exp = pg.evaluate("[String(magDeg(45)).padStart(3,'0') + '°', String(magDeg(0)).padStart(3,'0') + '°']")
            check("plane row shows magnetic heading", exp[0] in row[0] and exp[1] in row[1] and "°" not in row[2], f"{row} expected {exp}")
            # Magnetic directions: declination from /data (SNE ≈ 14° W), typed headings are magnetic, map math stays true
            decl = pg.evaluate("magDecl")
            check("declination loaded for the DZ (SNE ≈ -14)", -16 < decl < -12, str(decl))
            check("magnetic = true - declination", pg.evaluate("[magDeg(270), Math.round(toTrue(284))]") == [round(270 - decl) % 360, round(284 + decl) % 360])
            pg.evaluate("clearJumpRun(); openSuggestModal()"); pg.fill("#sgDir", "300"); pg.click("#sgSubmitBtn"); pg.wait_for_timeout(400)
            tb = pg.evaluate("[Math.round(bearing(jrStart, jrEnd)), document.querySelector('.leaflet-marker-pane').innerText]")
            check("typed magnetic jump run heading flies true = mag + decl, box shows the typed number", tb[0] == round(300 + decl) and "Heading: 300° mag" in tb[1], str(tb))
            pg.evaluate("clearJumpRun()")

            # 5. recommended separation formula
            check("separation = 1500 ft / ground speed, rounded", pg.evaluate("[60,90,120].map(recommendedSep)") == [15, 10, 7])
            ver = pg.evaluate("document.querySelector('meta[name=app-version]').content")
            check("version shown in welcome + Quick Start", pg.evaluate("[welcomeVersion.textContent, document.querySelector('#quickStartModal .app-ver-label').textContent]") == ["Version " + ver, "v" + ver])
            check("no JS errors during session", not errs, str(errs))
            ctx.close()

            # 6. Phone: dragging the wind summary moves it (no stretching, no popup); a plain tap still opens the detail
            ctx, pg, errs = new_page(b, 390, 844, touch=True)
            pg.evaluate("windSummary.style.display='block'"); pg.wait_for_timeout(300)
            rect = "(()=>{const r=windSummary.getBoundingClientRect();return [Math.round(r.left),Math.round(r.top),Math.round(r.width),Math.round(r.height)]})()"
            swipe = """([dx, dy]) => { const t = windSummary.querySelector('[title="Tap for canopy detail"]'), r = t.getBoundingClientRect();
                const x = r.left + r.width/2, y = r.top + r.height/2;
                const fire = (type, cx, cy) => { const k = new Touch({identifier: 1, target: t, clientX: cx, clientY: cy});
                    t.dispatchEvent(new TouchEvent(type, {bubbles: true, cancelable: true, touches: type == 'touchend' ? [] : [k], changedTouches: [k]})); };
                fire('touchstart', x, y); for (let i = 1; i <= 10; i++) fire('touchmove', x + dx*i/10, y + dy*i/10); fire('touchend', x + dx, y + dy); }"""
            r0 = pg.evaluate(rect); pg.evaluate(swipe, [-30, -200]); r1 = pg.evaluate(rect)
            check("phone: wind summary drags without resizing", r1[2:] == r0[2:] and r1[1] - r0[1] == -200 and r1[0] - r0[0] == -30, f"{r0} -> {r1}")
            check("phone: drag doesn't open the detail popup", pg.evaluate("canopyBlock.style.display") == "none")
            pg.evaluate(swipe, [0, 0])
            check("phone: tap opens the detail popup", pg.evaluate("canopyBlock.style.display") != "none")
            ctx.close()

            # 7. Accounts: email code sign-in, first sign-in uploads this device's canopy settings, a second device gets them
            def sent(): return json.loads(urllib.request.urlopen(URL + "_test/sent").read())
            def sign_in(pg, email):
                pg.evaluate("openAccountModal()"); pg.fill("#acctEmail", email); pg.click("#acctSendBtn"); pg.wait_for_timeout(500)
                pg.fill("#acctCode", sent()["codes"][email]["code"]); pg.wait_for_timeout(800)
            ctxA, pa, errsA = new_page(b)
            pa.evaluate("openAdvancedModal()"); pa.fill("#advWeight", "210"); pa.evaluate("applyAdvancedSettings()"); pa.wait_for_timeout(500)
            pa.evaluate("openAccountModal()"); pa.fill("#acctEmail", "jumper@example.com"); pa.click("#acctSendBtn"); pa.wait_for_timeout(500)
            pa.fill("#acctCode", "000000" if sent()["codes"]["jumper@example.com"]["code"] != "000000" else "111111"); pa.wait_for_timeout(500)
            check("wrong code is rejected", "Wrong code" in pa.inner_text("#acctMsg") and pa.evaluate("acctUser") is None)
            pa.fill("#acctCode", sent()["codes"]["jumper@example.com"]["code"]); pa.wait_for_timeout(800)
            check("sign in with emailed code", pa.evaluate("acctUser && acctUser.email") == "jumper@example.com")
            check("first sign-in uploads canopy settings", pa.evaluate("fetch('/auth/me').then(r=>r.json()).then(d=>d.user.settings.canopy.advWeight)") == "210")
            ctxB, pb, errsB = new_page(b)
            sign_in(pb, "jumper@example.com")
            check("second device gets synced canopy settings", pb.evaluate("advWeight.value") == "210")
            pb.evaluate("openAdvancedModal()"); pb.fill("#advWeight", "190"); pb.evaluate("applyAdvancedSettings()"); pb.wait_for_timeout(500)
            pa.reload(); pa.wait_for_timeout(2500)
            check("change on one device shows on the other after reload", pa.evaluate("advWeight.value") == "190")

            # 8. Takeoff alerts: the Track Plane modal shows the switch; a subscribed device gets the airborne push for its DZ
            pa.evaluate("closeWelcome(); openTrackModal()"); pa.wait_for_timeout(800)
            check("takeoff alert switch shown", pa.evaluate("alertRow.style.display") == "block" and pa.inner_text("#alertToggle") == "OFF")
            dzname = pa.evaluate("document.getElementById('dz').value")
            d = pa.evaluate("""(dzn) => fetch('/push/subscribe', {method:'POST', headers:{'Content-Type':'application/json'},
                body: JSON.stringify({subscription:{endpoint:'https://push.example/abc', keys:{p256dh:'x', auth:'y'}}, dz: dzn, on: true})}).then(r=>r.json())""", dzname)
            check("push subscribe stores the DZ", d.get("dzs") == [dzname])
            urllib.request.urlopen(urllib.request.Request(URL + "_test/airborne", data=json.dumps({"dz": dzname, "tail": "N999MW"}).encode(),
                                                          headers={"Content-Type": "application/json"}))
            time.sleep(0.8)
            pushes = sent()["pushes"]
            check("airborne alert sent to the DZ's subscribers", any("https://push.example/abc" in x["endpoints"] and "N999MW" in x["payload"]["title"] for x in pushes), str(pushes)[:200])
            pa.evaluate("signOut()"); pa.wait_for_timeout(400)
            check("sign out", pa.evaluate("fetch('/auth/me').then(r=>r.json()).then(d=>d.user)") is None)
            check("no JS errors in account/alert flows", not errsA and not errsB, str(errsA + errsB))
            ctxA.close(); ctxB.close()

            # 9. Service worker covers the whole app, and the takeoff-alert switch turns on AND off
            ctx = b.new_context(viewport={"width": 412, "height": 900})
            pg = ctx.new_page(); errs = []; pg.on("pageerror", lambda e: errs.append(str(e)[:300]))
            pg.goto(URL); pg.wait_for_timeout(1500)
            pg.evaluate("navigator.serviceWorker.register('/static/sw.js')")   # an old /static/-scoped worker, as on existing phones
            pg.reload(); pg.wait_for_timeout(2500); pg.evaluate("closeWelcome()")
            scopes = pg.evaluate("navigator.serviceWorker.ready.then(() => navigator.serviceWorker.getRegistrations()).then(rs => rs.map(r => new URL(r.scope).pathname))")
            check("service worker scope is the whole app; old /static/ worker removed", scopes == ["/"], str(scopes))
            # headless Chrome has no push service and always reports notifications blocked: stand-ins for both
            pg.evaluate("""(async () => { Object.defineProperty(Notification, 'permission', { get: () => 'granted' }); Notification.requestPermission = async () => 'granted';
                const reg = await navigator.serviceWorker.ready; let sub = null;
                const mk = () => ({ endpoint: 'https://push.example/toggle', toJSON(){ return { endpoint: this.endpoint, keys: { p256dh: 'x', auth: 'y' } }; } });
                reg.pushManager.getSubscription = async () => sub; reg.pushManager.subscribe = async () => (sub = sub || mk()); })()""")
            pg.evaluate("openTrackModal()"); pg.wait_for_timeout(800)
            seq = [pg.inner_text("#alertToggle")]
            for _ in range(3):
                pg.click("#alertToggle"); pg.wait_for_timeout(600); seq.append(pg.inner_text("#alertToggle"))
            check("takeoff alert switch toggles OFF→ON→OFF→ON", seq == ["OFF", "ON", "OFF", "ON"], str(seq))
            pg.click("#alertToggle"); pg.wait_for_timeout(600)
            srv_dzs = pg.evaluate("fetch('/push/status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({endpoint:'https://push.example/toggle'})}).then(r=>r.json()).then(d=>d.dzs)")
            check("turning alerts off removes the DZ on the server", srv_dzs == [], str(srv_dzs))
            check("no JS errors in service worker/alert toggle", not errs, str(errs))
            ctx.close(); b.close()
    finally:
        srv.terminate()
    print(f"\n{sum(results)}/{len(results)} passed")
    sys.exit(0 if all(results) else 1)

if __name__ == "__main__":
    main()
