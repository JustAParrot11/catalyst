"""Drive the brain map in a real browser and check the camera rule.

    The script may move the camera and change what is emphasised.
    It never decides what is drawn.

The offline suite can hold the static half of that - the SVG has no
script, the layer fetches nothing, the no-JS controls are all still
links. It cannot hold the half that matters most: that the picture is
IDENTICAL with scripting on and off. That needs two real browsers, so it
lives here rather than in pytest, which is offline by contract.

Run it after any change to the map or its interaction layer:

    python scripts/map_interaction_check.py

Exits non-zero on the first failure, so it can go in a pre-merge check.
Skips cleanly (exit 0, with a notice) where Playwright is not installed,
because the VPS does not need a browser to run the bot.
"""

import pathlib
import sqlite3
import sys
import tempfile
import threading
from datetime import datetime, timezone

CHROMIUM = "/opt/pw-browsers/chromium"
SCHEMA_FILES = ("catalyst/storage/schema.sql",
                "catalyst/storage/schema_graph.sql",
                "catalyst/dashboard/schema_logs.sql")

passed = failed = 0


def check(name, got, want=True):
    global passed, failed
    ok = (got == want)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}"
          + ("" if ok else f"   (got {got!r}, wanted {want!r})"))
    if ok:
        passed += 1
    else:
        failed += 1


def build_db() -> str:
    path = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(path)
    root = pathlib.Path(__file__).resolve().parents[1]
    for f in SCHEMA_FILES:
        conn.executescript((root / f).read_text())
    now = datetime.now(timezone.utc).isoformat()
    srcs = ["edgar_form4", "federal_register", "clinicaltrials",
            "alpaca_news", "openfda"]
    for i in range(40):
        conn.execute("INSERT INTO raw_events VALUES (?,?,?,?)",
                     (srcs[i % 5], f"e{i}", now, "{}"))
        conn.execute(
            "INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
            (f"c{i}", f"TK{i:02d}", ["financing", "earnings", "guidance"][i % 3],
             "2026-08-25", "confirmed", f'["e{i}"]', now, "2870", "[]"))
        if i % 2 == 0:
            conn.execute(
                "INSERT INTO research_views VALUES (?,?,?,?,?,?,?,?)",
                (f"c{i}", "long" if i % 4 else "no_trade", 0.6 + i / 200,
                 "t", "inv", 10, 0, "x"))
            conn.execute(
                "INSERT INTO risk_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (f"rd{i}", f"c{i}", "skip", None, None, None, None, None,
                 '["conviction_below_floor"]', "{}", now))
    conn.commit()
    conn.close()
    return path


def counts(page):
    """What is actually drawn. The number that must not move."""
    return page.evaluate("""() => ({
      dots: document.querySelectorAll('svg [data-node]').length,
      lines: document.querySelectorAll('svg [data-src]').length,
      text: (document.querySelector('svg') || {}).textContent || ''
    })""")


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright not installed - skipping the browser checks")
        return 0

    from catalyst.dashboard import server as srv

    db = build_db()
    httpd = srv.make_server("127.0.0.1", 0, db)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=CHROMIUM)

        print("\nTHE CAMERA RULE: same picture, scripting on or off")
        off = browser.new_context(java_script_enabled=False)
        on = browser.new_context()
        p_off, p_on = off.new_page(), on.new_page()
        p_off.goto(f"{base}/brain")
        p_on.goto(f"{base}/brain")
        c_off, c_on = counts(p_off), counts(p_on)
        check(f"same nodes drawn ({c_on['dots']})", c_on["dots"], c_off["dots"])
        check(f"same lines drawn ({c_on['lines']})", c_on["lines"], c_off["lines"])
        check("same text in the drawing", c_on["text"], c_off["text"])
        check("no-JS page still links into a focus",
              p_off.query_selector_all('a[href*="/brain?focus="]') != [])
        check("no-JS page hides the drag hints",
              p_off.is_visible("#brain-tools"), False)
        check("scripted page shows them", p_on.is_visible("#brain-tools"))

        print("\nDRAG TO MOVE")
        page = p_on
        page.set_viewport_size({"width": 1049, "height": 900})
        page.goto(f"{base}/brain")
        before = page.eval_on_selector("#brain-map-camera",
                                       "e => e.getAttribute('transform') || ''")
        box = page.query_selector("#brain-map").bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] / 2 - 160,
                        box["y"] + box["height"] / 2 - 40, steps=8)
        page.mouse.up()
        after = page.eval_on_selector("#brain-map-camera",
                                      "e => e.getAttribute('transform') || ''")
        check("dragging moved the camera", after != before)
        check("dragging did not change what is drawn", counts(page)["dots"],
              c_on["dots"])
        check("dragging did not change what is linked", counts(page)["lines"],
              c_on["lines"])

        print("\nSCROLL TO ZOOM")
        page.goto(f"{base}/brain")
        page.mouse.move(box["x"] + 300, box["y"] + 150)
        page.mouse.wheel(0, -400)
        page.wait_for_timeout(60)
        zoomed = page.eval_on_selector(
            "#brain-map-camera",
            "e => +(e.getAttribute('transform')||'').split('scale(')[1]"
            ".replace(')','')")
        check(f"wheel zoomed in (scale {zoomed:.2f})", zoomed > 1.0)
        check("the readout followed",
              page.text_content("#brain-zoomnow") != "100%")
        page.mouse.wheel(0, 900)
        page.wait_for_timeout(60)
        check("wheel zooms back out",
              page.eval_on_selector(
                  "#brain-map-camera",
                  "e => +(e.getAttribute('transform')||'').split('scale(')[1]"
                  ".replace(')','')") < zoomed)

        print("\nRESET")
        page.click("#brain-reset")
        check("reset returns to 100%", page.text_content("#brain-zoomnow"), "100%")

        print("\nCLICK A NODE TO FOLLOW ITS THREAD")
        page.goto(f"{base}/brain")
        # BASELINE TAKEN HERE, on this page, immediately before the
        # click. Comparing against a count captured earlier on another
        # page let a deliberately broken build - one whose script
        # REMOVED the nodes it had not selected - pass every check.
        # A stale baseline is not a baseline.
        base_dots = counts(page)["dots"]
        node = page.query_selector("svg [data-node]")
        label = node.get_attribute("data-label")
        node.click()
        page.wait_for_timeout(60)
        check("a card appeared", page.is_visible("#brain-card"))
        check("it names the node it was told about",
              label in (page.text_content("#brain-card") or ""))
        check("it offers the runbook",
              page.query_selector('#brain-card a[href^="/node?id="]') is not None)
        dimmed = page.evaluate(
            """() => [].slice.call(document.querySelectorAll('svg .node'))
                 .filter(n => getComputedStyle(n).opacity < '0.5').length""")
        check(f"unrelated nodes dimmed ({dimmed})", dimmed > 0)
        hidden = page.evaluate(
            """() => [].slice.call(document.querySelectorAll('svg .node'))
                 .filter(n => getComputedStyle(n).display === 'none').length""")
        check("nothing was HIDDEN, only dimmed", hidden, 0)
        check("selecting removed no node", counts(page)["dots"], base_dots)
        check("selecting removed no line", counts(page)["lines"], c_on["lines"])
        page.keyboard.press("Escape")
        page.wait_for_timeout(60)
        check("Escape clears the selection",
              page.is_visible("#brain-card"), False)

        print("\nTYPE TO FIND")
        page.fill("#brain-find", label[:3])
        page.wait_for_timeout(60)
        found = page.eval_on_selector_all("svg .node.found", "n => n.length")
        check(f"matches are marked ({found})", found > 0)
        check("non-matches are dimmed, not removed",
              counts(page)["dots"], base_dots)

        print("\nKEYBOARD")
        page.goto(f"{base}/brain")
        page.focus("#brain-map")
        page.keyboard.press("ArrowRight")
        page.wait_for_timeout(40)
        check("arrow keys pan",
              page.eval_on_selector("#brain-map-camera",
                                    "e => e.getAttribute('transform')")
              != "translate(0.00,0.00) scale(1.0000)")

        print("\nNO CONSOLE ERRORS")
        errors = []
        p2 = on.new_page()
        p2.on("pageerror", lambda e: errors.append(str(e)))
        p2.on("console", lambda m: errors.append(m.text)
              if m.type == "error" else None)
        p2.goto(f"{base}/brain")
        p2.mouse.wheel(0, -200)
        p2.wait_for_timeout(200)
        check(f"clean console {errors}", errors, [])

        browser.close()
    httpd.shutdown()

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
