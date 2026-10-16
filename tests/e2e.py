"""Browser smoke tests (Playwright). Starts the mock server itself.
    pip install -r requirements-dev.txt && playwright install chromium
    python tests/e2e.py
If the machine can't reach the Leaflet CDN, point LEAFLET_DIR at a folder containing leaflet.js and leaflet.css."""
import os, subprocess, sys, time, urllib.request
from playwright.sync_api import sync_playwright

PORT = 5099
URL = f"http://localhost:{PORT}/"
HERE = os.path.dirname(os.path.abspath(__file__))
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

def main():
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
            pg.evaluate("clearJumpRun()")

            # 4. Suggested Jump Run produces a run on several headings
            pg.click("#suggestBtn"); ok = True
            for d in (0, 90, 200, 300):
                pg.fill("#sgDir", str(d)); pg.click("#sgSubmitBtn"); pg.wait_for_timeout(400)
                ok &= pg.evaluate("!!jrStart && !!jrEnd")
                pg.evaluate("clearJumpRun();openSuggestModal()")
            check("suggested jump run works for 4 headings", ok)

            # 5. recommended separation formula
            check("separation = 1500 ft / ground speed, rounded", pg.evaluate("[60,90,120].map(recommendedSep)") == [15, 10, 7])
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
            ctx.close(); b.close()
    finally:
        srv.terminate()
    print(f"\n{sum(results)}/{len(results)} passed")
    sys.exit(0 if all(results) else 1)

if __name__ == "__main__":
    main()
