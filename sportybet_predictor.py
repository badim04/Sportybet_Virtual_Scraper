"""
╔══════════════════════════════════════════════════════════════════╗
║  SPORTYBET VIRTUAL PREDICTOR  v2                                 ║
║  ─────────────────────────────────────────────────────────────── ║
║  1. Loads all historical JSON files (football + racing)          ║
║  2. Builds calibrated frequency models per league / venue        ║
║  3. (optional) Opens browser, scrapes LIVE upcoming odds         ║
║  4. Compares historical rates to live implied probabilities       ║
║  5. Saves strong bets to predictions/ folder                     ║
║                                                                  ║
║  Usage:                                                          ║
║    python sportybet_predictor.py              # full mode        ║
║    python sportybet_predictor.py --no-live    # analysis only    ║
║    python sportybet_predictor.py --football   # football only    ║
║    python sportybet_predictor.py --racing     # racing only      ║
║    python sportybet_predictor.py --headless   # hidden browser   ║
║    python sportybet_predictor.py --min-edge 0.04  # stricter     ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os, sys, json, glob, re, time, math, signal, queue, threading, argparse
from collections import defaultdict

# Windows: force UTF-8 output so box-drawing chars don't crash + line-buffered
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
    )
from datetime import datetime

# ═══════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════

DATA_DIR    = "data"
PRED_DIR    = "predictions"
DEBUG_DIR   = "debug"
SITE_URL    = "https://www.sportybet.com/ng/virtual/"
MIN_EDGE    = 0.030   # 3% — minimum edge to flag as value bet
STRONG_EDGE = 0.060   # 6% — strong recommendation
MIN_SAMPLES = 30      # skip markets with fewer historical records

FOOTBALL_LEAGUES = ["England", "France", "Germany", "Italy", "Spain", "Turkey"]
RACING_SPORTS    = ["Greyhound Racing", "Horse Racing", "Speedway Racing", "Motorbike Racing"]

# sidebar text aliases (site may display full league names)
LEAGUE_ALIASES = {
    "England": ["England", "Virtual Premier League", "Premier League"],
    "France":  ["France",  "Virtual Ligue 1",       "Ligue 1"],
    "Germany": ["Germany", "Virtual Bundesliga",     "Bundesliga"],
    "Italy":   ["Italy",   "Virtual Serie A",        "Serie A"],
    "Spain":   ["Spain",   "Virtual La Liga",        "La Liga"],
    "Turkey":  ["Turkey",  "Virtual Süper Lig",      "Süper Lig"],
}

# Venue names per racing sport (from confirmed debug dumps)
RACING_VENUES = {
    "Greyhound Racing": ["Santa Monica", "London"],
    "Horse Racing":     ["Royal Meadow", "Queen Mary", "Paris", "Kentucky",
                         "San Pablo", "Dubai", "Palermo", "Melbourne",
                         "Golden Gate Fields", "Gulfstream"],
    "Speedway Racing":  ["Bristol"],
    "Motorbike Racing": ["Southern"],
}


# ═══════════════════════════════════════════════════════════
#  CTRL+C — non-blocking pause (same pattern as scrapers)
# ═══════════════════════════════════════════════════════════

_pause   = False
_input_q: queue.Queue = queue.Queue()

def _input_worker():
    while True:
        try: _input_q.put(input())
        except EOFError: break

threading.Thread(target=_input_worker, daemon=True).start()

def _ph(sig, frame):
    global _pause
    if _pause:
        print("\n  Force quit.")
        os._exit(0)
    _pause = True
    print("\n\n  PAUSED — press ENTER to resume, Ctrl+C again to quit.")

signal.signal(signal.SIGINT, _ph)

def _smart_sleep(secs):
    end = time.time() + secs
    while time.time() < end:
        global _pause
        if _pause:
            while _pause:
                try:
                    _input_q.get(timeout=0.3)
                    _pause = False
                    print("  Resumed.\n")
                except queue.Empty:
                    pass
        time.sleep(0.1)


# ═══════════════════════════════════════════════════════════
#  SECTION 1 — LOAD HISTORICAL DATA
# ═══════════════════════════════════════════════════════════

def load_football_history():
    files = sorted(glob.glob(os.path.join(DATA_DIR, "sportybet_results_*.json")))
    if not files:
        # also check current dir for backwards compat
        files = sorted(glob.glob("sportybet_results_*.json"))
    records, seen = [], set()
    for f in files:
        try:
            with open(f, encoding="utf-8") as fp:
                data = json.load(fp)
            if not isinstance(data, list):
                continue
            for r in data:
                mid    = r.get("match_id")
                league = r.get("league", "")
                key    = (mid, league) if mid else id(r)
                if key not in seen:
                    seen.add(key)
                    records.append(r)
        except Exception as e:
            print(f"  ⚠  {os.path.basename(f)}: {e}", flush=True)

    print(f"  ✓ Football : {len(records):,} unique matches from {len(files)} file(s)", flush=True)
    return records


def load_racing_history():
    files = sorted(glob.glob(os.path.join(DATA_DIR, "sportybet_racing_*.json")))
    if not files:
        files = sorted(glob.glob("sportybet_racing_*.json"))
    records, seen = [], set()
    for f in files:
        try:
            with open(f, encoding="utf-8") as fp:
                data = json.load(fp)
            if not isinstance(data, list):
                continue
            for r in data:
                rid = r.get("race_id")
                key = rid or id(r)
                if key not in seen:
                    seen.add(key)
                    records.append(r)
        except Exception as e:
            print(f"  ⚠  {os.path.basename(f)}: {e}", flush=True)

    print(f"  ✓ Racing   : {len(records):,} unique races from {len(files)} file(s)", flush=True)
    return records


# ═══════════════════════════════════════════════════════════
#  SECTION 2 — FOOTBALL MODEL
#  per-league frequency tables
# ═══════════════════════════════════════════════════════════

def build_football_model(records):
    by_league = defaultdict(list)
    for r in records:
        hs = r.get("home_score")
        as_ = r.get("away_score")
        if hs is None or as_ is None:
            continue
        try:
            hs, as_ = int(hs), int(as_)
        except (ValueError, TypeError):
            continue
        by_league[r.get("league", "Unknown")].append((hs, as_))

    model = {}
    for league, games in by_league.items():
        n = len(games)
        if n < 10:
            continue
        totals = [h + a for h, a in games]
        score_freq = defaultdict(int)
        for h, a in games:
            score_freq[f"{h}-{a}"] += 1
        top = sorted(score_freq.items(), key=lambda x: -x[1])[:8]

        model[league] = {
            "n":             n,
            "home_win_rate": sum(1 for h, a in games if h > a) / n,
            "draw_rate":     sum(1 for h, a in games if h == a) / n,
            "away_win_rate": sum(1 for h, a in games if h < a) / n,
            "over_0_5_rate": sum(1 for t in totals if t > 0.5) / n,
            "over_1_5_rate": sum(1 for t in totals if t > 1.5) / n,
            "over_2_5_rate": sum(1 for t in totals if t > 2.5) / n,
            "over_3_5_rate": sum(1 for t in totals if t > 3.5) / n,
            "over_4_5_rate": sum(1 for t in totals if t > 4.5) / n,
            "btts_rate":     sum(1 for h, a in games if h > 0 and a > 0) / n,
            "avg_goals":     sum(totals) / n,
            "avg_home_goals": sum(h for h, _ in games) / n,
            "avg_away_goals": sum(a for _, a in games) / n,
            "top_scores":    [(s, c / n) for s, c in top],
        }
    return model


# ═══════════════════════════════════════════════════════════
#  SECTION 3 — RACING MODEL
#  per venue: EVEN/ODD rates, OVER/UNDER rates
# ═══════════════════════════════════════════════════════════

def build_racing_model(records):
    by_venue = defaultdict(list)
    for r in records:
        sport = r.get("sport", "Unknown")
        venue = r.get("race_name", "Unknown")
        by_venue[f"{sport} — {venue}"].append(r)

    model = {}
    for key, races in by_venue.items():
        n = len(races)

        eo = [(r["markets"]["even_odd"]["result"],
               r["markets"]["even_odd"].get("odds"))
              for r in races
              if r.get("markets", {}).get("even_odd", {}).get("result")]
        eo_n   = len(eo)
        even_n = sum(1 for res, _ in eo if res == "E")

        ou = [(r["markets"]["over_under"]["result"],
               r["markets"]["over_under"].get("odds"))
              for r in races
              if r.get("markets", {}).get("over_under", {}).get("result")]
        ou_n   = len(ou)
        over_n = sum(1 for res, _ in ou if res == "O")

        even_hist_odds = [o for res, o in eo if res == "E" and o and o > 1]
        odd_hist_odds  = [o for res, o in eo if res == "O" and o and o > 1]
        over_hist_odds = [o for res, o in ou if res == "O" and o and o > 1]
        undr_hist_odds = [o for res, o in ou if res == "U" and o and o > 1]
        win_odds = [r["markets"]["win"]["odds"] for r in races
                    if r.get("markets", {}).get("win", {}).get("odds")]

        def _safe(num, denom):
            return num / denom if denom > 0 else None

        model[key] = {
            "n":            n,
            "sport":        races[0].get("sport") if races else "",
            "even_rate":    _safe(even_n, eo_n),
            "odd_rate":     _safe(eo_n - even_n, eo_n),
            "eo_n":         eo_n,
            "avg_even_odds": sum(even_hist_odds)/len(even_hist_odds) if even_hist_odds else None,
            "avg_odd_odds":  sum(odd_hist_odds)/len(odd_hist_odds)   if odd_hist_odds  else None,
            "over_rate":    _safe(over_n, ou_n),
            "under_rate":   _safe(ou_n - over_n, ou_n),
            "ou_n":         ou_n,
            "avg_over_odds": sum(over_hist_odds)/len(over_hist_odds) if over_hist_odds else None,
            "avg_undr_odds": sum(undr_hist_odds)/len(undr_hist_odds) if undr_hist_odds else None,
            "avg_win_odds": sum(win_odds) / len(win_odds) if win_odds else None,
        }
    return model


# ═══════════════════════════════════════════════════════════
#  SECTION 4 — DISPLAY: HISTORICAL ANALYSIS
# ═══════════════════════════════════════════════════════════

def _pct(val, dp=1):
    return f"{val*100:.{dp}f}%" if val is not None else "  N/A"

def _f(val, fmt=".2f"):
    return format(val, fmt) if val is not None else "N/A"


def display_football_analysis(model):
    leagues = [l for l in FOOTBALL_LEAGUES if l in model]
    w = 100
    print("\n" + "═" * w)
    print("  FOOTBALL HISTORICAL RATES")
    print("═" * w)
    print(f"  {'League':<10} {'N':>6}  {'HW%':>6} {'D%':>6} {'AW%':>6}  "
          f"{'O1.5':>6} {'O2.5':>6} {'O3.5':>6}  {'BTTS':>6}  {'AvgG':>5}")
    print("  " + "─" * (w - 2))
    for lg in leagues:
        m = model[lg]
        print(f"  {lg:<10} {m['n']:>6}  "
              f"{_pct(m['home_win_rate']):>6} {_pct(m['draw_rate']):>6} {_pct(m['away_win_rate']):>6}  "
              f"{_pct(m['over_1_5_rate']):>6} {_pct(m['over_2_5_rate']):>6} {_pct(m['over_3_5_rate']):>6}  "
              f"{_pct(m['btts_rate']):>6}  {m['avg_goals']:.2f}")

    print()
    for lg in leagues:
        m = model[lg]
        top = "  ".join(f"{s}({p*100:.1f}%)" for s, p in m["top_scores"][:5])
        print(f"  {lg:<10} top scores: {top}")
    print("═" * w)


def display_racing_analysis(model):
    w = 88
    print("\n" + "═" * w)
    print("  RACING HISTORICAL RATES")
    print("═" * w)
    print(f"  {'Venue':<40} {'N':>5}  {'EVEN':>6} {'ODD':>6}  {'OVER':>6} {'UNDR':>6}  {'AvgWin':>7}")
    print("  " + "─" * (w - 2))
    for key, m in sorted(model.items()):
        print(f"  {key:<40} {m['n']:>5}  "
              f"{_pct(m['even_rate']):>6} {_pct(m['odd_rate']):>6}  "
              f"{_pct(m['over_rate']):>6} {_pct(m['under_rate']):>6}  "
              f"{_f(m['avg_win_odds']):>7}")
    print("═" * w)


# ═══════════════════════════════════════════════════════════
#  SECTION 5 — BROWSER SETUP + UTILITIES
# ═══════════════════════════════════════════════════════════

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")


def _ensure_dirs():
    os.makedirs(PRED_DIR, exist_ok=True)
    os.makedirs(DEBUG_DIR, exist_ok=True)


def setup_browser(headless=False):
    from playwright.sync_api import sync_playwright
    pw      = sync_playwright().start()
    browser = pw.chromium.launch(
        channel="chrome",
        headless=headless,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    )
    ctx = browser.new_context(
        user_agent=UA,
        viewport={"width": 1366, "height": 900},
        locale="en-GB",
    )
    ctx.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'languages', {get: () => ['en-GB','en']});
    """)
    page = ctx.new_page()
    return pw, browser, ctx, page


def get_virtual_frame(page, timeout=60):
    """Wait for the virtual games iframe and return it."""
    start    = time.time()
    last_log = 0

    while time.time() - start < timeout:
        if time.time() - last_log > 3:
            all_urls = [f.url for f in page.frames if f.url]
            print(f"  ℹ Frames ({len(all_urls)}): " +
                  " | ".join(u[:70] for u in all_urls), flush=True)
            last_log = time.time()

        for f in page.frames:
            url = f.url or ""
            if (url and url != "about:blank"
                    and "sportybet.com/ng/virtual" not in url
                    and url != SITE_URL
                    and len(url) > 10):
                try:
                    body = f.evaluate(
                        "() => document.body ? document.body.innerText.length : 0"
                    )
                    if body and body > 50:
                        print(f"  ✓ Virtual frame: {url[:100]}", flush=True)
                        return f
                except:
                    pass

        try:
            srcs = page.evaluate(r"""() => {
                return Array.from(document.querySelectorAll('iframe'))
                    .map(f => ({src: f.src || f.getAttribute('src') || '',
                                id: f.id, cls: f.className}))
                    .filter(f => f.src && f.src !== 'about:blank');
            }""")
            if srcs:
                print(f"  ℹ iframe[src] in DOM: {srcs}", flush=True)
        except:
            pass

        _smart_sleep(2)

    for f in page.frames:
        url = f.url or ""
        if url and url != "about:blank" and url != SITE_URL:
            print(f"  ⚠ Fallback frame: {url[:100]}", flush=True)
            return f

    print("  ⚠ No virtual iframe found", flush=True)
    return page


def dump_frame(frame, label):
    try:
        data = frame.evaluate(r"""() => ({
            url:   location.href,
            title: document.title,
            body:  (document.body.innerText || '').substring(0, 6000),
            links: Array.from(document.querySelectorAll('a')).map(a => ({
                text: a.textContent.trim().substring(0, 60),
                cls:  a.className.substring(0, 60),
                vis:  a.offsetParent !== null,
            })).filter(a => a.text),
        })""")
        fname = os.path.join(DEBUG_DIR, f"debug_predictor_{label}.txt")
        with open(fname, "w", encoding="utf-8") as f:
            f.write(f"URL: {data['url']}\nTitle: {data['title']}\n\n")
            f.write("=== BODY ===\n" + data["body"] + "\n\n")
            f.write("=== LINKS ===\n")
            for lnk in data["links"]:
                f.write(f"  [{'V' if lnk['vis'] else 'h'}] {lnk['cls']} | {lnk['text']}\n")
        print(f"  📄 Debug dump: {fname}", flush=True)
    except Exception as e:
        print(f"  ⚠ Dump failed: {e}", flush=True)


# ═══════════════════════════════════════════════════════════
#  SECTION 6 — LIVE FOOTBALL ODDS SCRAPER
#
#  Sidebar structure:
#    "Football League" is a toggler (collapsed by default)
#    Sub-items: England, Italy, Spain, Germany, France, Turkey
#    After clicking a league: click "Over/Under" tab, parse O/U 2.5 odds
# ═══════════════════════════════════════════════════════════

def _parse_odds(text):
    """Extract decimal odds (1.01–99.99) from a string, in order."""
    return [float(m) for m in re.findall(r'\b(\d{1,2}\.\d{2})\b', text)
            if 1.01 <= float(m) <= 99.99]


def _expand_sidebar_toggler(frame, toggler_text):
    """
    Expand a sidebar toggler by text.
    Uses offsetHeight of the next sibling UL to detect expansion state.
    Returns True if successfully expanded (or already was).
    """
    try:
        result = frame.evaluate(r"""(text) => {
            // Find the toggler element
            const all = Array.from(document.querySelectorAll(
                '[class*="toggler"], [class*="nav-item"], li, a'
            ));
            const el = all.find(e =>
                e.textContent.trim().includes(text) &&
                e.getBoundingClientRect().height > 0
            );
            if (!el) return 'not_found';

            // Check if sub-list already has height (already expanded)
            const sub = el.nextElementSibling || el.querySelector('ul');
            if (sub && sub.offsetHeight > 0) return 'already_open';

            // Click to expand
            el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
            el.dispatchEvent(new MouseEvent('mouseup',   {bubbles: true}));
            el.click();
            return 'clicked';
        }""", toggler_text)

        if result == "already_open":
            print(f"    ℹ '{toggler_text}' already expanded", flush=True)
            return True
        if result == "clicked":
            _smart_sleep(1.2)
            # Verify expansion
            is_open = frame.evaluate(r"""(text) => {
                const all = Array.from(document.querySelectorAll(
                    '[class*="toggler"], [class*="nav-item"], li, a'
                ));
                const el = all.find(e =>
                    e.textContent.trim().includes(text) &&
                    e.getBoundingClientRect().height > 0
                );
                if (!el) return false;
                const sub = el.nextElementSibling || el.querySelector('ul');
                return sub ? sub.offsetHeight > 0 : false;
            }""", toggler_text)
            if is_open:
                print(f"    ✓ Expanded '{toggler_text}'", flush=True)
                return True
            print(f"    ⚠ '{toggler_text}' may not have expanded (check debug dump)", flush=True)
            return False
        print(f"    ⚠ Toggler '{toggler_text}' not found in DOM", flush=True)
        return False
    except Exception as e:
        print(f"    ⚠ expand_toggler error: {e}", flush=True)
        return False


def _click_sidebar_item(frame, texts, label="item"):
    """
    Click the first visible sidebar link whose text exactly matches any entry in `texts`.
    Uses Playwright native click (triggers Angular router navigation).
    Returns the matched text on success, None on failure.
    """
    for text in texts:
        # Playwright native click — auto-scrolls and triggers Angular router
        try:
            loc = frame.locator(f"text='{text}'").first
            if loc.is_visible(timeout=1500):
                loc.click(timeout=3000)
                print(f"    ✓ Clicked {label}: '{text}'", flush=True)
                return text
        except:
            pass

        # Fallback: exact-text `a` element
        try:
            loc = frame.locator(f"a").filter(has_text=re.compile(rf"^{re.escape(text)}$")).first
            if loc.is_visible(timeout=1000):
                loc.click(timeout=3000)
                print(f"    ✓ Clicked {label} (a exact): '{text}'", flush=True)
                return text
        except:
            pass

        # Last resort: JS with exact match + scrollIntoView
        try:
            matched = frame.evaluate(r"""(t) => {
                const all = Array.from(document.querySelectorAll('a, li'));
                const el = all.find(e =>
                    e.textContent.trim() === t &&
                    e.getBoundingClientRect().height > 0
                );
                if (el) {
                    el.scrollIntoView({block: 'center'});
                    el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                    el.dispatchEvent(new MouseEvent('mouseup',   {bubbles: true}));
                    el.click();
                    return true;
                }
                return false;
            }""", text)
            if matched:
                print(f"    ✓ Clicked {label} (JS exact): '{text}'", flush=True)
                return text
        except:
            pass

    return None


def _click_tab(frame, tab_texts):
    """
    Click a tab button by text using Playwright native click.
    This properly triggers Angular's (click) bindings unlike JS el.click().
    Returns matched text or None.
    """
    for text in tab_texts:
        # Playwright native: exact text match on likely tab elements
        for sel in [
            f"a:has-text('{text}')",
            f"button:has-text('{text}')",
            f"[role='tab']:has-text('{text}')",
            f"[class*='tab']:has-text('{text}')",
        ]:
            try:
                loc = frame.locator(sel).first
                if loc.is_visible(timeout=1000):
                    loc.click(timeout=3000)
                    print(f"    ✓ Clicked tab: '{text}'", flush=True)
                    return text
            except:
                pass

        # Fallback: JS with scrollIntoView to ensure it's in viewport
        try:
            matched = frame.evaluate(r"""(t) => {
                const candidates = Array.from(document.querySelectorAll(
                    'a, button, [role="tab"], [class*="tab-item"], [class*="market-filter"]'
                ));
                const el = candidates.find(e =>
                    e.textContent.trim() === t &&
                    e.getBoundingClientRect().height > 0
                );
                if (el) {
                    el.scrollIntoView({block: 'center'});
                    el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                    el.dispatchEvent(new MouseEvent('mouseup',   {bubbles: true}));
                    el.click();
                    return true;
                }
                return false;
            }""", text)
            if matched:
                print(f"    ✓ Clicked tab (JS): '{text}'", flush=True)
                return text
        except:
            pass
    return None


def _scrape_football_ou25(frame):
    """
    Scrape Over/Under 2.5 odds from the Over/Under tab.

    Actual page format (multi-line, each value on its own line):
        1.          <- match number
        (space)
        LEH         <- home abbrev
        -
        SBR         <- away abbrev
        Over 0.5
        1.04        <- odds on NEXT line after label
        Under 0.5
        11.70
        Over 1.5
        1.37
        Under 1.5
        2.98
        Over 2.5    <- ← we want this
        2.12
        Under 2.5
        1.68

    Returns list of {match_name, week, over_2_5, under_2_5}.
    """
    results = []
    week    = None

    try:
        body  = frame.evaluate("() => document.body.innerText")
        lines = [l.strip() for l in body.split("\n") if l.strip()]

        # Find week number (e.g. "Week 18" or "Week 18 - France")
        for line in lines:
            wm = re.search(r'\bWeek\s+(\d+)\b', line, re.IGNORECASE)
            if wm:
                week = int(wm.group(1))
                break

        # Collect all Over 2.5 / Under 2.5 lines with their odds (next line)
        over_list  = []
        under_list = []
        for i, line in enumerate(lines):
            # Exact label on its own line (e.g. "Over 2.5")
            if re.fullmatch(r'[Oo]ver\s*2\.5', line):
                if i + 1 < len(lines):
                    odds = _parse_odds(lines[i + 1])
                    if odds:
                        over_list.append(odds[0])
            elif re.fullmatch(r'[Uu]nder\s*2\.5', line):
                if i + 1 < len(lines):
                    odds = _parse_odds(lines[i + 1])
                    if odds:
                        under_list.append(odds[0])
            # Also handle "Over 2.5  1.70" on a single line
            elif '2.5' in line:
                ll   = line.lower()
                odds = _parse_odds(line)
                if 'over' in ll and 'under' in ll and len(odds) >= 2:
                    over_list.append(odds[0])
                    under_list.append(odds[1])
                elif 'over' in ll and odds:
                    over_list.append(odds[0])
                elif 'under' in ll and odds:
                    under_list.append(odds[0])

        # Pair each Over with the corresponding Under
        for j, (ov, un) in enumerate(zip(over_list, under_list)):
            results.append({
                "match_name": f"Week {week} Match {j + 1}" if week else f"Match {j + 1}",
                "week":       week,
                "over_2_5":   ov,
                "under_2_5":  un,
            })

    except Exception as e:
        print(f"    ⚠ OU25 parse error: {e}", flush=True)

    # Deduplicate by odds pair
    seen, out = set(), []
    for r in results:
        key = (r.get("over_2_5"), r.get("under_2_5"))
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def scrape_live_football(frame, leagues=None):
    """
    For each league:
      1. Re-expand "Football League" toggler (it collapses after each navigation)
      2. Click country sidebar link
      3. Wait for page to load
      4. Click "Over/Under" tab (Playwright native click to trigger Angular)
      5. Parse O/U 2.5 odds per match (multi-line format)
    Returns {league: [match_dict, ...]}
    """
    leagues = leagues or FOOTBALL_LEAGUES
    live = {}

    for league in leagues:
        print(f"  ℹ  Football — {league}...", flush=True)

        # Re-expand every iteration — toggler collapses after each league navigation
        _expand_sidebar_toggler(frame, "Football League")
        _smart_sleep(0.8)

        aliases = LEAGUE_ALIASES.get(league, [league])
        clicked = _click_sidebar_item(frame, aliases, label=f"league '{league}'")

        if not clicked:
            print(f"    ⚠ Sidebar item not found", flush=True)
            dump_frame(frame, f"football_{league.lower()}_sidebar_fail")
            continue

        # Wait for league page to fully load (Angular router + data fetch)
        _smart_sleep(3.5)

        # Click the Over/Under tab — must use real Playwright click (triggers Angular)
        tab = _click_tab(frame, ["Over/Under"])
        if not tab:
            print(f"    ⚠ Over/Under tab not found — dumping DOM", flush=True)
            dump_frame(frame, f"football_{league.lower()}_tab_fail")
            continue

        # Wait for Over/Under content to render
        _smart_sleep(2.0)

        matches = _scrape_football_ou25(frame)
        if not matches:
            print(f"    ⚠ No O/U 2.5 odds found — dumping DOM", flush=True)
            dump_frame(frame, f"football_{league.lower()}_ou_fail")
        else:
            print(f"    ✓ {len(matches)} match(es) with O/U 2.5 odds", flush=True)
            live[league] = matches

    return live


# ═══════════════════════════════════════════════════════════
#  SECTION 7 — LIVE RACING ODDS SCRAPER
#
#  Sidebar structure (confirmed from debug dumps):
#    "Greyhound Racing" is a toggler → sub-items: Santa Monica, London
#    "Horse Racing"     is a toggler → sub-items: Royal Meadow, Queen Mary, ...
#    "Speedway Racing"  is a toggler → sub-items: Bristol
#    "Motorbike Racing" is a toggler → sub-items: Southern
#  After clicking a venue: click Even/Odd tab, then Over/Under tab
# ═══════════════════════════════════════════════════════════

RACING_EO_TAB_CANDIDATES = ["Even/Odd", "Even Odd", "EvenOdd"]
RACING_OU_TAB_CANDIDATES = ["Over/Under", "Over Under", "OverUnder",
                             "Over/Under/Trifecta", "Over Under Trifecta"]


def _scrape_eo_ou_from_page(frame):
    """
    Read EVEN/ODD and OVER/UNDER odds from the current race tab.
    Scans innerText for labeled pairs.
    """
    try:
        body = frame.evaluate("() => document.body.innerText")
    except:
        return {}

    lines  = [l.strip() for l in body.split("\n") if l.strip()]
    result = {"even_odds": None, "odd_odds": None,
              "over_odds": None, "under_odds": None}

    for line in lines:
        ll   = line.lower()
        odds = _parse_odds(line)
        if not odds:
            continue

        if "even" in ll and "odd" in ll and len(odds) >= 2:
            result["even_odds"] = odds[0]
            result["odd_odds"]  = odds[1]
        elif "even" in ll and len(odds) >= 1:
            result["even_odds"] = odds[0]
        elif "odd" in ll and "even" not in ll and len(odds) >= 1:
            result["odd_odds"] = odds[0]

        if "over" in ll and "under" in ll and len(odds) >= 2:
            result["over_odds"]  = odds[0]
            result["under_odds"] = odds[1]
        elif "over" in ll and "under" not in ll and len(odds) >= 1:
            result["over_odds"] = odds[0]
        elif "under" in ll and "over" not in ll and len(odds) >= 1:
            result["under_odds"] = odds[0]

    return result


def scrape_live_racing(frame, sports=None):
    """
    For each sport:
      1. Expand sport toggler (Greyhound Racing, Horse Racing, etc.)
      2. Click first available venue (Santa Monica, Royal Meadow, etc.)
      3. Click Even/Odd tab → scrape
      4. Click Over/Under tab → scrape
    Returns {sport_name: {even_odds, odd_odds, over_odds, under_odds, venue}}
    """
    sports = sports or list(RACING_VENUES.keys())
    live   = {}

    for sport in sports:
        venues = RACING_VENUES.get(sport, [])
        if not venues:
            continue

        print(f"  ℹ  Racing — {sport}...", flush=True)

        # Expand the sport's sidebar toggler
        _expand_sidebar_toggler(frame, sport)
        _smart_sleep(1.0)

        # Click the first venue that's visible
        venue_clicked = None
        for venue in venues:
            clicked = _click_sidebar_item(frame, [venue], label=f"venue '{venue}'")
            if clicked:
                venue_clicked = venue
                break

        if not venue_clicked:
            print(f"    ⚠ No venue found for {sport}", flush=True)
            dump_frame(frame, f"racing_{sport.replace(' ','_').lower()}_sidebar_fail")
            continue

        _smart_sleep(2.0)

        card = {"even_odds": None, "odd_odds": None,
                "over_odds": None, "under_odds": None,
                "venue": venue_clicked}

        # Click Even/Odd tab (Playwright native click triggers Angular)
        eo_tab = _click_tab(frame, RACING_EO_TAB_CANDIDATES)
        if eo_tab:
            _smart_sleep(2.0)
            result = _scrape_eo_ou_from_page(frame)
            card.update({k: v for k, v in result.items() if v is not None})

        # Click Over/Under tab (separate tab)
        ou_tab = _click_tab(frame, RACING_OU_TAB_CANDIDATES)
        if ou_tab:
            _smart_sleep(2.0)
            result = _scrape_eo_ou_from_page(frame)
            card.update({k: v for k, v in result.items() if v is not None})

        has_data = card["even_odds"] or card["over_odds"]
        if not has_data:
            print(f"    ⚠ No odds found — dumping DOM", flush=True)
            dump_frame(frame, f"racing_{sport.replace(' ','_').lower()}_odds_fail")
        else:
            parts = []
            if card["even_odds"]: parts.append(f"EVEN {card['even_odds']}")
            if card["odd_odds"]:  parts.append(f"ODD {card['odd_odds']}")
            if card["over_odds"]: parts.append(f"OVER {card['over_odds']}")
            if card["under_odds"]:parts.append(f"UNDR {card['under_odds']}")
            print(f"    ✓ {venue_clicked}: {' | '.join(parts)}", flush=True)

        live[sport] = card

    return live


# ═══════════════════════════════════════════════════════════
#  SECTION 8 — PREDICTION ENGINE
#  Football: Over/Under 2.5 only (league-wide rate is valid)
#  Racing: Even/Odd + Over/Under
# ═══════════════════════════════════════════════════════════

def _make_rec(market, live_odds, hist_rate, n_samples):
    if live_odds is None or hist_rate is None:
        return None
    implied = 1 / live_odds
    edge    = hist_rate - implied
    return {
        "market":     market,
        "odds":       live_odds,
        "implied":    implied,
        "historical": hist_rate,
        "edge":       edge,
        "n_samples":  n_samples,
    }


def predict_football(model, live_matches):
    """
    Only predict Over/Under 2.5 — league-wide rate is statistically valid.
    1X2 predictions are omitted: home win rate is a league average,
    not valid for a specific match at specific odds.
    """
    preds = []
    for league, matches in live_matches.items():
        m = model.get(league)
        if not m:
            continue
        hist_over  = m["over_2_5_rate"]
        hist_under = 1 - hist_over
        n          = m["n"]
        for match in matches:
            match_name = match.get("match_name", "Unknown Match")
            week       = match.get("week")
            label      = f"Week {week} — {match_name}" if week else match_name

            recs = []
            for mkt_name, hist, live_odds in [
                ("Over 2.5",  hist_over,  match.get("over_2_5")),
                ("Under 2.5", hist_under, match.get("under_2_5")),
            ]:
                r = _make_rec(mkt_name, live_odds, hist, n)
                if r and r["edge"] >= MIN_EDGE:
                    recs.append(r)

            if recs:
                preds.append({
                    "sport":  "Football",
                    "league": league,
                    "week":   week,
                    "label":  label,
                    "recs":   sorted(recs, key=lambda x: -x["edge"]),
                })
    return preds


def predict_racing(model, live_racing):
    preds = []
    for sport, card in live_racing.items():
        key = next((k for k in model if k.startswith(sport)), None)
        if not key:
            continue
        m     = model[key]
        venue = card.get("venue", sport)
        recs  = []
        for mkt_name, hist, live_odds, n in [
            ("Even",  m.get("even_rate"),  card.get("even_odds"),  m["eo_n"]),
            ("Odd",   m.get("odd_rate"),   card.get("odd_odds"),   m["eo_n"]),
            ("Over",  m.get("over_rate"),  card.get("over_odds"),  m["ou_n"]),
            ("Under", m.get("under_rate"), card.get("under_odds"), m["ou_n"]),
        ]:
            r = _make_rec(mkt_name, live_odds, hist, n)
            if r and r["edge"] >= MIN_EDGE:
                recs.append(r)

        if recs:
            preds.append({
                "sport":  "Racing",
                "league": key,
                "week":   None,
                "label":  f"Next {venue} race",
                "recs":   sorted(recs, key=lambda x: -x["edge"]),
            })
    return preds


# ═══════════════════════════════════════════════════════════
#  SECTION 9 — OUTPUT: EXCEL + JSON
#  File 1: all strong bets (edge >= STRONG_EDGE) per league/week
#  File 2: single top pick per gameweek (highest edge)
# ═══════════════════════════════════════════════════════════

def _flag(edge):
    if edge >= STRONG_EDGE: return "STRONG"
    if edge >= MIN_EDGE:    return "VALUE"
    return "SKIP"


def _build_flat_rows(all_preds, threshold):
    rows = []
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for pred in all_preds:
        for r in pred["recs"]:
            verdict = ("STRONG BET" if r["edge"] >= STRONG_EDGE
                       else "VALUE BET" if r["edge"] >= threshold
                       else "SKIP")
            rows.append({
                "timestamp":      ts,
                "sport":          pred["sport"],
                "league":         pred["league"],
                "week":           pred.get("week", ""),
                "match":          pred["label"],
                "market":         r["market"],
                "live_odds":      round(r["odds"],      2),
                "implied_pct":    round(r["implied"]    * 100, 2),
                "historical_pct": round(r["historical"] * 100, 2),
                "edge_pct":       round(r["edge"]       * 100, 2),
                "n_samples":      r["n_samples"],
                "verdict":        verdict,
            })
    return rows


def _top_pick_per_week(strong_rows):
    """
    For each (league, week) combination, return the single row with
    the highest edge. For racing (no week), group by league alone.
    """
    groups = defaultdict(list)
    for row in strong_rows:
        wk  = row.get("week") or "next"
        key = (row["league"], wk)
        groups[key].append(row)
    top = []
    for rows in groups.values():
        best = max(rows, key=lambda x: x["edge_pct"])
        top.append(best)
    return sorted(top, key=lambda x: -x["edge_pct"])


def save_predictions(all_preds, threshold):
    """
    Save predictions to predictions/ folder:
      - sportybet_strong_bets_TIMESTAMP.xlsx   (all strong bets)
      - sportybet_top_pick_TIMESTAMP.xlsx       (one pick per week)
      - sportybet_predictions_TIMESTAMP.json    (all value bets)
    """
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
        has_xl = True
    except ImportError:
        print("  ⚠ openpyxl not installed — skipping Excel. Run: pip install openpyxl", flush=True)
        has_xl = False

    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    rows = _build_flat_rows(all_preds, threshold)

    # ── JSON: all value bets ─────────────────────────────────
    json_path = os.path.join(PRED_DIR, f"sportybet_predictions_{ts}.json")
    value_rows  = [r for r in rows if r["verdict"] in ("STRONG BET", "VALUE BET")]
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(value_rows, f, indent=2, ensure_ascii=False)
    print(f"  💾 JSON: {json_path}  ({len(value_rows)} value bets)", flush=True)

    if not has_xl:
        return

    # Shared style setup
    HDR_FILL    = PatternFill("solid", fgColor="1F3864")
    HDR_FONT    = Font(bold=True, color="FFFFFF", size=11)
    hdr_align   = Alignment(horizontal="center", vertical="center", wrap_text=True)
    STRONG_FILL = PatternFill("solid", fgColor="C6EFCE")
    VALUE_FILL  = PatternFill("solid", fgColor="FFEB9C")
    thin        = Side(style="thin", color="BBBBBB")
    border      = Border(left=thin, right=thin, top=thin, bottom=thin)

    headers  = ["Timestamp", "Sport", "League / Venue", "Week", "Match / Race",
                "Market", "Live Odds", "Implied %", "Historical %",
                "Edge %", "N Samples", "Verdict"]
    col_keys = ["timestamp", "sport", "league", "week", "match", "market",
                "live_odds", "implied_pct", "historical_pct",
                "edge_pct", "n_samples", "verdict"]
    col_widths = [18, 10, 35, 7, 35, 14, 10, 10, 12, 10, 10, 14]
    pct_keys   = {"implied_pct", "historical_pct", "edge_pct"}

    def _write_sheet(ws, data_rows):
        for ci, h in enumerate(headers, 1):
            cell = ws.cell(row=1, column=ci, value=h)
            cell.fill = HDR_FILL; cell.font = HDR_FONT; cell.alignment = hdr_align
        ws.row_dimensions[1].height = 28

        for ri, row in enumerate(data_rows, 2):
            fill = STRONG_FILL if row["verdict"] == "STRONG BET" else VALUE_FILL
            for ci, key in enumerate(col_keys, 1):
                cell = ws.cell(row=ri, column=ci, value=row[key])
                cell.border    = border
                cell.alignment = Alignment(horizontal="center", vertical="center")
                cell.fill      = fill
                if key in ("verdict", "edge_pct"):
                    cell.font = Font(bold=True)
                if key in pct_keys:
                    cell.number_format = '0.00"%"'

        for ci, w in enumerate(col_widths, 1):
            ws.column_dimensions[get_column_letter(ci)].width = w
        ws.freeze_panes = "A2"
        if data_rows:
            ws.auto_filter.ref = ws.dimensions

    # ── Excel 1: all strong bets ────────────────────────────
    strong_rows = [r for r in rows if r["verdict"] == "STRONG BET"]
    strong_rows.sort(key=lambda x: (x["league"], x.get("week") or "", -x["edge_pct"]))

    xl1_path = os.path.join(PRED_DIR, f"sportybet_strong_bets_{ts}.xlsx")
    wb1 = openpyxl.Workbook()
    ws1 = wb1.active
    ws1.title = "Strong Bets"
    if strong_rows:
        _write_sheet(ws1, strong_rows)
    else:
        ws1.cell(row=1, column=1, value="No strong bets found at current threshold.")
    wb1.save(xl1_path)
    print(f"  💾 Strong bets: {xl1_path}  ({len(strong_rows)} bets)", flush=True)

    # ── Excel 2: top pick per gameweek ──────────────────────
    top_rows = _top_pick_per_week(strong_rows if strong_rows else value_rows)
    xl2_path = os.path.join(PRED_DIR, f"sportybet_top_pick_{ts}.xlsx")
    wb2 = openpyxl.Workbook()
    ws2 = wb2.active
    ws2.title = "Top Pick Per Week"
    if top_rows:
        _write_sheet(ws2, top_rows)
    else:
        ws2.cell(row=1, column=1, value="No picks available.")
    wb2.save(xl2_path)
    print(f"  💾 Top picks:   {xl2_path}  ({len(top_rows)} picks)", flush=True)


def display_predictions(all_preds, threshold):
    w  = 92
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    print("\n" + "═" * w)
    print(f"  LIVE PREDICTIONS  [{ts}]   (showing STRONG bets only, edge >= {STRONG_EDGE*100:.0f}%)")
    print("═" * w)

    if not all_preds:
        print("\n  No predictions — live scraper may need DOM calibration.")
        print("  Check debug/ folder for debug_predictor_*.txt files.")
        print("═" * w)
        return

    strong_bets = []

    for pred in all_preds:
        for r in pred["recs"]:
            if r["edge"] >= STRONG_EDGE:
                strong_bets.append({**r,
                                    "sport":  pred["sport"],
                                    "league": pred["league"],
                                    "week":   pred.get("week"),
                                    "label":  pred["label"]})

    if not strong_bets:
        print(f"\n  No STRONG bets (edge >= {STRONG_EDGE*100:.0f}%) found right now.")
        print("  Value bets (smaller edge) are still saved to the JSON file.")
    else:
        print(f"\n  {len(strong_bets)} STRONG BET(S) — sorted by edge:\n")
        print(f"  {'Sport':<8} {'League':<35} {'Week':>5}  {'Market':<12} {'Odds':>6}  "
              f"{'Hist%':>7}  {'Edge':>7}  N")
        print("  " + "─" * (w - 2))
        for vb in sorted(strong_bets, key=lambda x: -x["edge"]):
            wk = str(vb["week"]) if vb["week"] else "—"
            print(f"  {vb['sport']:<8} {vb['league']:<35} {wk:>5}  "
                  f"{vb['market']:<12} {vb['odds']:>6.2f}  "
                  f"{vb['historical']*100:>6.1f}%  {vb['edge']*100:>+6.1f}%  {vb['n_samples']}")

    print("\n" + "═" * w)
    print()
    print("  ⚠  These are virtual RNG games. Edge is calibrated vs historical data only.")
    print("     Higher N = more statistically reliable. Never bet money you can't afford to lose.")
    print("═" * w)

    print()
    save_predictions(all_preds, threshold)


# ═══════════════════════════════════════════════════════════
#  SECTION 10 — MAIN
# ═══════════════════════════════════════════════════════════

def main():
    global DATA_DIR, PRED_DIR, DEBUG_DIR, MIN_EDGE, STRONG_EDGE

    ap = argparse.ArgumentParser(description="Sportybet Virtual Predictor v2")
    ap.add_argument("--no-live",   action="store_true", help="Historical analysis only — no browser")
    ap.add_argument("--football",  action="store_true", help="Football only")
    ap.add_argument("--racing",    action="store_true", help="Racing only")
    ap.add_argument("--headless",  action="store_true", help="Headless browser")
    ap.add_argument("--min-edge",  type=float, default=MIN_EDGE,
                    help=f"Min edge to flag as value bet (default {MIN_EDGE})")
    ap.add_argument("--data-dir",  default=DATA_DIR, help="Directory containing JSON files")
    args = ap.parse_args()

    DATA_DIR    = args.data_dir
    MIN_EDGE    = args.min_edge
    STRONG_EDGE = args.min_edge * 2

    _ensure_dirs()

    do_football = True
    do_racing   = True
    if args.football and not args.racing:
        do_racing = False
    if args.racing and not args.football:
        do_football = False

    w = 70
    print("═" * w)
    print("  SPORTYBET VIRTUAL PREDICTOR  v2")
    print(f"  {datetime.now().strftime('%Y-%m-%d  %H:%M:%S')}")
    print(f"  Data:        {os.path.abspath(DATA_DIR)}")
    print(f"  Predictions: {os.path.abspath(PRED_DIR)}")
    print(f"  Debug dumps: {os.path.abspath(DEBUG_DIR)}")
    print("═" * w)

    # ── Load & model ──────────────────────────────────────────
    print("\n  Loading historical data...")
    football_records = load_football_history() if do_football else []
    racing_records   = load_racing_history()   if do_racing   else []

    football_model = build_football_model(football_records) if football_records else {}
    racing_model   = build_racing_model(racing_records)     if racing_records   else {}

    # ── Historical analysis display ───────────────────────────
    if football_model:
        display_football_analysis(football_model)
    if racing_model:
        display_racing_analysis(racing_model)

    if args.no_live:
        print("\n  [--no-live]  Skipping browser.  "
              "Re-run without --no-live for live predictions.\n")
        return

    # ── Browser: live odds + predictions ─────────────────────
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        print("\n  ⚠ Playwright not installed.")
        print("    pip install playwright && playwright install chromium")
        return

    print("\n  Opening browser for live odds...\n")
    pw = browser = ctx = page = None
    try:
        pw, browser, ctx, page = setup_browser(headless=args.headless)
        print("  ℹ Loading site...", flush=True)
        page.goto(SITE_URL, timeout=60000, wait_until="networkidle")
        print("  ℹ Page loaded — waiting for virtual iframe to initialise...", flush=True)
        _smart_sleep(8)

        frame = get_virtual_frame(page, timeout=60)
        _smart_sleep(2)

        live_football = {}
        live_racing   = {}

        if do_football and football_model:
            print("\n  ── Football ──────────────────────────────────────────────")
            live_football = scrape_live_football(frame)

        if do_racing and racing_model:
            print("\n  ── Racing ────────────────────────────────────────────────")
            live_racing = scrape_live_racing(frame)

        # ── Predict ───────────────────────────────────────────
        fp = predict_football(football_model, live_football)
        rp = predict_racing(racing_model, live_racing)
        display_predictions(fp + rp, threshold=MIN_EDGE)

    except Exception as e:
        print(f"\n  ✗ Error: {e}", flush=True)
        import traceback
        traceback.print_exc()
    finally:
        try:
            if browser: browser.close()
            if pw:      pw.stop()
        except:
            pass


if __name__ == "__main__":
    main()
