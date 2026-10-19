"""Playwright checks + screenshots for web/. Re-runnable.

  .venv/bin/python pipeline/frontend/check_web.py            # fixtures and real data
  .venv/bin/python pipeline/frontend/check_web.py fixtures   # only ?fixtures=1

Serves web/ on a local port, asserts the SPEC defect fixes, writes web/screens/*.png.
Exit code = number of failed checks.
"""
import functools
import http.server
import sys
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

WEB = Path(__file__).resolve().parents[2] / "web"
SHOTS = WEB / "screens"
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAILS.append(name)


def serve() -> int:
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a: object) -> None:
            pass

    handler = functools.partial(Quiet, directory=str(WEB))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


def labels_text(page) -> list[str]:
    return page.eval_on_selector_all(".hood-label", "els => els.map(e => e.textContent.trim())")


def run_variant(p, port: int, tag: str, query: str) -> None:
    url = f"http://127.0.0.1:{port}/index.html{query}"
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: m.type == "error" and "404" not in m.text and "Failed to load resource" not in m.text and errors.append(m.text))
    page.goto(url)
    page.wait_for_function("window.__cmw && window.__cmw.stopBest")
    page.wait_for_timeout(1500)  # tiles
    S = page.evaluate("({n: __cmw.n, src: __cmw.sourceKeys, mode: __cmw.mode, walk: __cmw.walk, file: __cmw.walkFromFile, reached: Array.from(__cmw.grid).filter(isFinite).length, cells: __cmw.grid.length})")
    print(tag, S)
    check(f"{tag}: grid has reached cells", S["reached"] > 0, f"{S['reached']}/{S['cells']}")
    page.screenshot(path=str(SHOTS / f"{tag}-desktop-default.png"))

    # timing of one origin change at this data scale
    ms = page.evaluate("() => { const t = performance.now(); document.getElementById('center-office').click(); return performance.now() - t; }")
    check(f"{tag}: origin change under 1500 ms", ms < 1500, f"{ms:.0f} ms")

    # hash written on map click, survives reload
    page.mouse.click(900, 450)
    page.wait_for_timeout(300)
    h1 = page.evaluate("location.hash")
    check(f"{tag}: hash written after map click", h1.startswith("#52."), h1)
    page.reload()
    page.wait_for_function("window.__cmw && window.__cmw.stopBest")
    o = page.evaluate("__cmw.origin.map(v => v.toFixed(5)).join(',')")
    check(f"{tag}: hash survives reload", "#" + o == h1, f"{o} vs {h1}")

    # Q-04/Q-05: bad hash or bad saved origin must not brick startup
    page.evaluate("localStorage.setItem('cmw-origin', JSON.stringify({lat: 1}))")
    page.goto(url.split("#")[0] + "#..,..")
    page.reload()
    page.wait_for_function("window.__cmw && window.__cmw.stopBest", timeout=5000)
    check(f"{tag}: bad hash + bad saved origin fall back", page.evaluate("__cmw.origin.join(',')") == "52.2329,20.9986")
    page.goto(url.split("#")[0] + "#0,0")
    page.reload()
    page.wait_for_function("window.__cmw && window.__cmw.stopBest", timeout=5000)
    check(f"{tag}: out-of-range hash #0,0 falls back", page.evaluate("__cmw.origin.join(',')") == "52.2329,20.9986")

    has_listings = bool(S["src"])
    # labels non-empty after zoom 13->11->13 and toggle off/on (Q-01, Q-02)
    for z in (13, 11, 13):
        page.evaluate(f"void __cmwMap.setZoom({z}, {{animate: false}})")
        page.wait_for_timeout(250)
    texts = labels_text(page)
    blanks = [t for t in texts if not t]
    check(f"{tag}: labels have text after zoom cycle", texts and not blanks, f"{len(texts)} labels, {len(blanks)} blank")
    page.click("label:has(#t-labels)")
    page.click("label:has(#t-labels)")
    texts = labels_text(page)
    check(f"{tag}: labels have text after toggle", texts and not [t for t in texts if not t], f"{len(texts)} labels")
    if has_listings:
        med = page.evaluate("__cmw.hoodStats.filter(s => s.med != null).length")
        print(tag, "hoods with median:", med)

    # Q-22: a bin with no own listings but >= 3 in its 3x3 block must get a value
    if has_listings:
        res = page.evaluate("""() => { const S = __cmw, br = S.binRows, bc = S.binCols, LAT0 = 52.09, LON0 = 20.85, ML = 110540, MO = 111320 * Math.cos(52.23 * Math.PI / 180);
          const cnt = new Int32Array(br * bc);
          for (const o of S.filtered) { if (o.prec >= 2) continue; const y = Math.floor((o.lat - LAT0) * ML / 350), x = Math.floor((o.lon - LON0) * MO / 350);
            if (y >= 0 && x >= 0 && y < br && x < bc) cnt[y * bc + x]++; }
          let tested = 0, bad = 0;
          for (let y = 1; y < br - 1; y++) for (let x = 1; x < bc - 1; x++) { if (cnt[y * bc + x]) continue;
            let s = 0; for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) s += cnt[(y + dy) * bc + x + dx];
            if (s >= 3) { tested++; if (Number.isNaN(S.binVal[y * bc + x])) bad++; } }
          return [tested, bad]; }""")
        check(f"{tag}: empty bins with qualifying 3x3 block are painted (Q-22)", res[0] > 0 and res[1] == 0, f"tested {res[0]}, unpainted {res[1]}")

    # 44 px targets
    small = page.evaluate("""() => [...document.querySelectorAll('#sidebar button, #sidebar select, #sidebar input:not([type=checkbox]), #sidebar label.chk, .leaflet-control-zoom a')]
      .filter(e => e.offsetParent !== null).map(e => [e.id || e.textContent.trim().slice(0, 20), e.getBoundingClientRect()])
      .filter(([_, r]) => r.height < 44 || r.width < 44).map(([n, r]) => n + ':' + Math.round(r.width) + 'x' + Math.round(r.height))""")
    check(f"{tag}: touch targets >= 44 px", not small, ", ".join(small))

    # focus ring visible after Tab
    page.mouse.click(5, 895)
    page.keyboard.press("Tab")
    outline = page.evaluate("() => { const e = document.activeElement; const cs = getComputedStyle(e); return [e.tagName + '#' + e.id, cs.outlineStyle, cs.outlineWidth]; }")
    check(f"{tag}: focus ring on Tab", outline[1] != "none" and outline[2] != "0px", str(outline))

    # zoom control not under sidebar (Q-03)
    ov = page.evaluate("""() => { const a = document.querySelector('.leaflet-control-zoom').getBoundingClientRect(), b = document.getElementById('sidebar').getBoundingClientRect();
      return !(a.right < b.left || a.left > b.right || a.bottom < b.top || a.top > b.bottom); }""")
    check(f"{tag}: zoom control clear of sidebar (desktop)", not ov)

    if has_listings:
        for mode in ("commute", "rent", "value"):
            page.click(f"[data-mode={mode}]")
            page.wait_for_timeout(200)
            page.screenshot(path=str(SHOTS / f"{tag}-desktop-{mode}.png"))
        page.click("[data-metric=ppm]")
        page.click("[data-mode=rent]")
        page.wait_for_timeout(200)
        page.screenshot(path=str(SHOTS / f"{tag}-desktop-rent-ppm.png"))
        check(f"{tag}: ppm count line mentions area", "powierzchni" in page.inner_text("#count"))
        page.click("[data-metric=price]")
        if len(S["src"]) > 1:
            page.click(f"[data-source={S['src'][1]}]")
            page.wait_for_timeout(200)
            check(f"{tag}: source switch updates count", S["src"][1] == page.evaluate("__cmw.source"))
        page.select_option("#rooms", "1")
        page.wait_for_timeout(200)
        page.screenshot(path=str(SHOTS / f"{tag}-desktop-kawalerka.png"))
        page.select_option("#rooms", "any")
        # escaping check: names with quotes/angle brackets render as text, not markup
        bad = page.evaluate("[...document.querySelectorAll('.hood-label')].some(e => e.querySelector('test'))")
        check(f"{tag}: label names escaped", not bad)
    else:
        check(f"{tag}: rent/value disabled without listings", page.is_disabled("[data-mode=rent]"))

    check(f"{tag}: no console errors", not errors, " | ".join(errors[:3]))
    ctx.close()

    # phone
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True)
    page = ctx.new_page()
    page.goto(url.split("#")[0])
    page.wait_for_function("window.__cmw && window.__cmw.stopBest")
    page.wait_for_timeout(1200)
    sw = page.evaluate("[document.documentElement.scrollWidth, innerWidth]")
    check(f"{tag}: phone no horizontal scroll", sw[0] <= sw[1], str(sw))
    page.screenshot(path=str(SHOTS / f"{tag}-phone-compact.png"))
    page.click("#title-btn")
    page.wait_for_timeout(200)
    page.screenshot(path=str(SHOTS / f"{tag}-phone-expanded.png"))
    ov = page.evaluate("""() => { const a = document.querySelector('.leaflet-control-zoom').getBoundingClientRect(), b = document.getElementById('sidebar').getBoundingClientRect();
      return !(a.right < b.left || a.left > b.right || a.bottom < b.top || a.top > b.bottom); }""")
    check(f"{tag}: zoom control clear of sidebar (phone)", not ov)
    ctx.close()
    browser.close()


def main() -> int:
    SHOTS.mkdir(exist_ok=True)
    port = serve()
    variants = [("fixtures", "?fixtures=1")] + ([] if sys.argv[1:] == ["fixtures"] else [("real", "")])
    with sync_playwright() as p:
        for tag, q in variants:
            run_variant(p, port, tag, q)
    print(f"{len(FAILS)} failed: {FAILS}")
    return len(FAILS)


if __name__ == "__main__":
    sys.exit(main())
