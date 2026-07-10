"""
╔══════════════════════════════════════════════════════════════════╗
║  SPORTYBET VIRTUAL FOOTBALL — Historical Results Scraper         ║
║  ─────────────────────────────────────────────────────────────── ║
║  Site:   https://www.sportybet.com/ng/virtual/                   ║
║  Target: Football League → Results History                       ║
║  Leagues: England, Italy, France, Germany, Spain                 ║
║                                                                  ║
║  Features:                                                       ║
║    ✓ Stealth browser (Basic / Advanced 16-layer)                 ║
║    ✓ Proxy (anyip.io) or system VPN                              ║
║    ✓ Headless toggle                                             ║
║    ✓ Iframe detection + frame switching for virtual content      ║
║    ✓ Per-match detailed odds scraping (expand each match)        ║
║    ✓ "Load more events" pagination                               ║
║    ✓ Date filter navigation for historical data                  ║
║    ✓ Output: JSON + Excel (.xlsx)                                ║
║    ✓ 6 mandatory init options                                    ║
║    ✓ Ctrl+C pause/resume                                         ║
║    ✓ Final summary with any failures                             ║
║                                                                  ║
║  Run:   python sportybet_virtual_scraper_v1.py                   ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os, json, csv, random, signal, time, traceback, re
import threading, queue
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright

# ═══════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════

BOT_NAME       = "SPORTYBET VIRTUAL SCRAPER — Part 1: Results History"
SITE_URL       = "https://www.sportybet.com/ng/virtual/"
PROXY_ADDR     = "http://portal.anyip.io:2000"
COOLDOWN_SECS  = 15

LEAGUES        = ["England", "France", "Germany", "Italy", "Spain", "Turkey"]
MAX_LOAD_MORE  = 20        # max "Load more events" clicks per league
EXPAND_DETAILS = True      # scrape detailed odds per match (slower but full data)

OUTPUT_JSON    = "sportybet_results_{league}_{ts}.json"
OUTPUT_XLSX    = "sportybet_results_all_{ts}.xlsx"


# ═══════════════════════════════════════════════════════════
#  BACKGROUND INPUT THREAD (proven from rapid_teacher)
# ═══════════════════════════════════════════════════════════
_input_q = queue.Queue()
def _input_worker():
    while True:
        try: _input_q.put(input())
        except EOFError: break
_input_thread = threading.Thread(target=_input_worker, daemon=True)
_input_thread.start()

def input_with_timeout(prompt, timeout_sec=120):
    while not _input_q.empty(): _input_q.get()
    print(prompt, end="", flush=True)
    try: return _input_q.get(timeout=timeout_sec).strip()
    except queue.Empty: print(); return ""


# ═══════════════════════════════════════════════════════════
#  CTRL+C  PAUSE / RESUME
#  Signal handler only sets a flag — never blocks inside it.
#  _check_pause() and _smart_sleep() poll the flag so the
#  Playwright C-extension never traps the interrupt.
#  Second Ctrl+C while paused = immediate force-quit.
# ═══════════════════════════════════════════════════════════
_pause = False

def _ph(sig, frame):
    global _pause
    if _pause:
        print("\n  ✋ Force quit.")
        os._exit(0)
    _pause = True
    print("\n\n  ⏸  PAUSED — press ENTER to resume, Ctrl+C to quit.")

signal.signal(signal.SIGINT, _ph)


def _check_pause():
    """Block until unpaused. Call inside loops to honour Ctrl+C."""
    global _pause
    if not _pause:
        return
    while _pause:
        try:
            line = _input_q.get(timeout=0.3)
            if line.strip().lower() == "q":
                print("  ✋ Quitting.")
                os._exit(0)
            _pause = False
            print("  ▶  Resumed.\n")
        except queue.Empty:
            pass


def _smart_sleep(secs):
    """Sleep in 200 ms chunks so Ctrl+C / pause can always fire."""
    end = time.time() + secs
    while time.time() < end:
        _check_pause()
        time.sleep(min(0.2, max(0, end - time.time())))


# ═══════════════════════════════════════════════════════════
#  STEALTH BROWSER (copied from ultimate_survey_framework.py)
# ═══════════════════════════════════════════════════════════

_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
]

_ADVANCED_STEALTH_JS = r"""
Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
Object.defineProperty(navigator,'plugins',{get:()=>{
    const p=[{name:'Chrome PDF Plugin',filename:'internal-pdf-viewer',description:'Portable Document Format',length:1},
    {name:'Chrome PDF Viewer',filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai',description:'',length:1},
    {name:'Native Client',filename:'internal-nacl-plugin',description:'',length:2}];
    p.__proto__=PluginArray.prototype;return p}});
Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});
Object.defineProperty(navigator,'platform',{get:()=>'Win32'});
(function(){const gp=HTMLCanvasElement.prototype.getContext;
HTMLCanvasElement.prototype.getContext=function(t,a){const c=gp.call(this,t,a);
if(t==='2d'){const gid=c.getImageData;c.getImageData=function(){
const d=gid.apply(this,arguments);for(let i=0;i<d.data.length;i+=100){
d.data[i]=d.data[i]^(Math.random()>0.5?1:0)}return d}}return c}})();
(function(){try{const gc=WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter=function(p){
if(p===37445)return'Google Inc.';if(p===37446)return'ANGLE (Intel, Intel(R) UHD Graphics 630, OpenGL 4.5)';
return gc.call(this,p)};const gc2=WebGL2RenderingContext.prototype.getParameter;
WebGL2RenderingContext.prototype.getParameter=function(p){
if(p===37445)return'Google Inc.';if(p===37446)return'ANGLE (Intel, Intel(R) UHD Graphics 630, OpenGL 4.5)';
return gc2.call(this,p)}}catch(e){}})();
window.chrome={runtime:{onConnect:{addListener:()=>{}},connect:()=>({onMessage:{addListener:()=>{}},postMessage:()=>{}}),
sendMessage:()=>{}},loadTimes:()=>({})};
Object.defineProperty(navigator,'hardwareConcurrency',{get:()=>8});
Object.defineProperty(navigator,'deviceMemory',{get:()=>8});
Object.defineProperty(navigator,'maxTouchPoints',{get:()=>0});
if(typeof Notification!=='undefined'&&Notification.permission==='default'){
Object.defineProperty(Notification,'permission',{get:()=>'denied'})}
"""
_BASIC_STEALTH_JS = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"


def create_stealth_browser(headless=False, proxy_server=None, stealth_mode="advanced"):
    ua = random.choice(_UA_POOL)
    chrome_ver = re.search(r'Chrome/(\d+)', ua)
    chrome_ver = chrome_ver.group(1) if chrome_ver else "125"
    pw = sync_playwright().start()
    args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox", "--disable-dev-shm-usage", "--window-size=1280,900",
    ]
    if stealth_mode == "advanced":
        args += [
            "--disable-features=IsolateOrigins,site-per-process",
            "--disable-infobars", "--disable-setuid-sandbox",
            "--lang=en-US,en", "--start-maximized",
        ]
    launch_kwargs = {
        "headless": headless,
        "args": args,
        "ignore_default_args": ["--enable-automation"],
    }
    if proxy_server:
        launch_kwargs["proxy"] = {"server": proxy_server}

    # Prefer real installed Chrome (best stealth); fall back to Edge (present
    # on every Windows box) and finally Playwright's bundled Chromium, so the
    # app still works on machines without Chrome.
    browser = None
    last_err = None
    for channel in ("chrome", "msedge", None):
        try:
            kw = dict(launch_kwargs)
            if channel:
                kw["channel"] = channel
            browser = pw.chromium.launch(**kw)
            if channel != "chrome":
                print(f"  ⚠ Chrome not found — using {channel or 'bundled Chromium'}")
            break
        except Exception as e:
            last_err = e
    if browser is None:
        pw.stop()
        raise last_err
    ctx_kwargs = {"viewport": {"width": 1280, "height": 900}, "locale": "en-US",
                  "timezone_id": "America/New_York", "extra_http_headers": {"Accept-Language": "en-US,en;q=0.9"}}
    if stealth_mode == "advanced":
        ctx_kwargs.update({
            "user_agent": ua, "color_scheme": "light", "java_script_enabled": True,
            "has_touch": False, "is_mobile": False, "device_scale_factor": 1,
            "extra_http_headers": {
                "Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "gzip, deflate, br",
                "Sec-Ch-Ua": f'"Chromium";v="{chrome_ver}", "Not(A:Brand";v="24", "Google Chrome";v="{chrome_ver}"',
                "Sec-Ch-Ua-Mobile": "?0", "Sec-Ch-Ua-Platform": '"Windows"',
            },
        })
    ctx = browser.new_context(**ctx_kwargs)
    js = _ADVANCED_STEALTH_JS if stealth_mode == "advanced" else _BASIC_STEALTH_JS
    ctx.add_init_script(script=js)
    page = ctx.new_page()
    page.set_default_timeout(30000)
    try:
        page.goto("https://api.ipify.org?format=json", timeout=15000)
        ip_text = page.text_content("body") or "?"
        print(f"  🌐 IP: {ip_text}")
    except:
        print("  ⚠ IP check failed")
    label = "🛡️ Advanced (16-layer)" if stealth_mode == "advanced" else "🔒 Basic"
    print(f"  {label} stealth active")
    return pw, browser, ctx, page


# ═══════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════

def human_pause(min_ms=300, max_ms=800):
    _smart_sleep(random.randint(min_ms, max_ms) / 1000)

def safe_text(page_or_frame):
    """Get visible text safely."""
    try:
        return page_or_frame.evaluate("() => document.body.innerText").lower()
    except:
        return ""

def safe_click(page_or_frame, selector, timeout=5000):
    """Click an element safely, return True/False."""
    try:
        el = page_or_frame.locator(selector).first
        el.wait_for(state="visible", timeout=timeout)
        el.click()
        return True
    except:
        return False

MAIN_PAGE_URL = "https://www.sportybet.com/ng/virtual/"

def wait_and_find_virtual_frame(page, timeout=45000):
    """
    Wait for the virtual games iframe and return it.
    Accepts ANY non-main-page frame — no domain whitelist needed.
    Also probes iframe src attributes directly from the DOM.
    """
    start = time.time()
    last_log = 0

    while (time.time() - start) * 1000 < timeout:
        # --- Log ALL frames every 3s so we can see the real iframe URL ---
        if time.time() - last_log > 3:
            all_urls = [f.url for f in page.frames if f.url]
            print(f"  ℹ All frames ({len(all_urls)}): " + " | ".join(u[:70] for u in all_urls))
            last_log = time.time()

        # Pick the first frame that isn't the main sportybet page
        for f in page.frames:
            url = f.url or ""
            if (url and url != "about:blank"
                    and "sportybet.com/ng/virtual" not in url
                    and "sportybet.com" in url or (
                        url and url != "about:blank"
                        and "sportybet.com/ng/virtual" not in url
                        and url != MAIN_PAGE_URL
                        and len(url) > 10
                    )):
                # It's a non-main frame with real content
                try:
                    # Quick sanity: does this frame have any body content?
                    body = f.evaluate("() => document.body ? document.body.innerText.length : 0")
                    if body and body > 50:
                        print(f"  ✓ Found virtual frame: {url[:100]}")
                        return f
                except:
                    pass

        # Also probe iframe[src] via JS to get the actual URL before Playwright
        # picks it up as a frame
        try:
            srcs = page.evaluate(r"""() => {
                return Array.from(document.querySelectorAll('iframe'))
                    .map(f => ({src: f.src || f.getAttribute('src') || '',
                                id: f.id, cls: f.className}))
                    .filter(f => f.src && f.src !== 'about:blank');
            }""")
            if srcs:
                print(f"  ℹ iframe[src] in DOM: {srcs}")
        except:
            pass

        _smart_sleep(2)

    # Last-ditch: return any frame that isn't the main page frame
    for f in page.frames:
        url = f.url or ""
        if url and url != "about:blank" and url != MAIN_PAGE_URL:
            print(f"  ⚠ Fallback: using frame {url[:100]}")
            return f

    print("  ⚠ No virtual iframe found — operating on main page (navigation will likely fail)")
    return page


def dump_frame_debug(frame, label="debug"):
    """Dump frame text + all clickable element texts to a file for selector diagnosis."""
    try:
        data = frame.evaluate(r"""() => {
            const clickable = Array.from(document.querySelectorAll(
                'a, button, [role="button"], li, [class*="menu"], [class*="nav"], ' +
                '[class*="item"], [class*="tab"], [class*="link"], [class*="sport"], ' +
                '[class*="league"], [class*="category"]'
            )).map(el => ({
                tag: el.tagName,
                cls: el.className.substring(0, 80),
                text: (el.innerText || el.textContent || "").trim().substring(0, 100),
                visible: el.offsetParent !== null,
            })).filter(e => e.text.length > 0);

            return {
                body_text: (document.body.innerText || "").substring(0, 3000),
                clickable: clickable,
                title: document.title,
                url: location.href,
            };
        }""")
        filename = f"debug_frame_{label}.txt"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(f"URL: {data.get('url','')}\n")
            f.write(f"Title: {data.get('title','')}\n\n")
            f.write("=== BODY TEXT ===\n")
            f.write(data.get("body_text", "") + "\n\n")
            f.write("=== CLICKABLE ELEMENTS ===\n")
            for el in data.get("clickable", []):
                vis = "VISIBLE" if el["visible"] else "hidden"
                f.write(f"  [{vis}] <{el['tag']}> cls={el['cls']} | {el['text']}\n")
        print(f"  📄 Debug dump saved: {filename}")
    except Exception as e:
        print(f"  ⚠ Debug dump failed: {e}")


def _try_click(frame, selectors, label, timeout=6000):
    """Try a list of selectors in order; return True on first successful click."""
    for sel in selectors:
        try:
            loc = frame.locator(sel).first
            loc.wait_for(state="visible", timeout=timeout)
            loc.scroll_into_view_if_needed()
            loc.click()
            print(f"    ✓ Clicked '{label}' via: {sel}")
            return True
        except:
            continue
    return False


def expand_football_league(frame):
    """
    Click the Football League toggler in the sidebar to expand its submenu.
    Uses the exact class from the real DOM: a.toggler.gr-icon-league[class*='collapsed']
    Returns True when the submenu is expanded (Results History link visible).
    """
    # The exact anchor: <A class="toggler text-overflow gr-icon-league collapsed ng-star-inserted">
    fl_selectors = [
        "a.toggler.gr-icon-league",
        "a[class*='toggler'][class*='gr-icon-league']",
        "a[class*='gr-icon-league']",
        "a.toggler:has-text('Football League')",
        "a:has-text('Football League')",
        "li:has-text('Football League') > a",
    ]

    clicked = False
    for sel in fl_selectors:
        try:
            el = frame.locator(sel).first
            el.wait_for(state="visible", timeout=8000)
            el.scroll_into_view_if_needed()
            el.click()
            print(f"    ✓ Clicked Football League toggler via: {sel}")
            clicked = True
            break
        except:
            continue

    if not clicked:
        print("    ⚠ Could not click Football League toggler — dumping frame")
        dump_frame_debug(frame, "football_league_fail")
        return False

    # Wait for the submenu to expand: Results History link must become visible
    print("    ⏳ Waiting for Football League submenu to expand...")
    for attempt in range(12):  # up to 6 seconds
        try:
            rh = frame.locator("a[class*='text-overflow']:has-text('Results History')").first
            if rh.is_visible(timeout=500):
                print("    ✓ Football League submenu expanded")
                return True
        except:
            pass
        # If still collapsed, try clicking again (sometimes needs double tap)
        if attempt == 4:
            try:
                el = frame.locator("a[class*='gr-icon-league']").first
                el.click()
                print("    ↺ Re-clicked toggler")
            except:
                pass
        time.sleep(0.5)

    print("    ⚠ Submenu did not expand — dumping frame")
    dump_frame_debug(frame, "football_league_submenu_fail")
    return False


def click_results_history_in_submenu(frame):
    """
    Click the Results History link that belongs to Football League's submenu.
    Uses JavaScript to find the first visible Results History <a> that is
    inside an expanded (non-collapsed) Football League <li>.
    Returns True on success.
    """
    clicked = frame.evaluate(r"""() => {
        // Find the Football League <li> in the sidebar
        const flLinks = Array.from(document.querySelectorAll('a'));
        const flLink = flLinks.find(a => a.textContent.trim() === 'Football League');
        if (!flLink) return false;
        const flLi = flLink.closest('li');
        if (!flLi) return false;
        // Find the Results History <a> inside that <li>
        const rhLinks = flLi.querySelectorAll('a');
        for (const a of rhLinks) {
            if (a.textContent.trim() === 'Results History' && a.offsetParent !== null) {
                a.click();
                return true;
            }
        }
        return false;
    }""")
    return bool(clicked)


def click_league_top_tab_js(frame, league_name):
    """
    Click the league tab at the TOP of the Results History page.
    The real DOM uses <DIV class="item ng-star-inserted"> for these tabs — NOT anchors.
    Selected tab has class "item selected ng-star-inserted".
    """
    clicked = frame.evaluate(f"""(league) => {{
        // Primary: div.item elements (confirmed from real DOM)
        const divItems = Array.from(document.querySelectorAll('div.item, div[class*="item"]'));
        for (const el of divItems) {{
            if (el.textContent.trim() === league && el.offsetParent !== null) {{
                el.click();
                return 'div.item';
            }}
        }}
        // Fallback: any element outside sidebar ul.nav
        const all = Array.from(document.querySelectorAll('a, button, span, div'));
        for (const el of all) {{
            if (el.textContent.trim() === league
                    && !el.closest('ul.nav')
                    && el.offsetParent !== null) {{
                el.click();
                return 'fallback';
            }}
        }}
        return null;
    }}""", league_name)

    if clicked:
        print(f"    ✓ Clicked top tab '{league_name}' via {clicked}")
        return True

    print(f"    ⚠ Top tab '{league_name}' not found — dumping")
    dump_frame_debug(frame, f"top_tab_{league_name}_fail")
    return False


# ─────────────────────────────────────────────────────────────
#  DATE FILTER NAVIGATION (for historical data collection)
# ─────────────────────────────────────────────────────────────

def get_calendar_month_year(frame):
    """Return (month_name, year) currently shown in the date filter calendar."""
    try:
        result = frame.evaluate(r"""() => {
            const title = document.querySelector(
                '.ui-datepicker-title, [class*="datepicker-title"], ' +
                '[class*="calendar-title"], .p-datepicker-title'
            );
            return title ? title.innerText.trim() : '';
        }""")
        return result or ""
    except:
        return ""


def click_calendar_prev_month(frame):
    """Click the previous-month arrow on the date filter calendar."""
    clicked = frame.evaluate(r"""() => {
        const prev = document.querySelector(
            '.ui-datepicker-prev, [class*="datepicker-prev"], ' +
            '.p-datepicker-prev, [class*="prev-month"], ' +
            'a[class*="prev"], button[class*="prev"]'
        );
        if (prev) { prev.click(); return true; }
        // Try by aria-label
        const byAria = document.querySelector('[aria-label*="prev"], [aria-label*="Previous"]');
        if (byAria) { byAria.click(); return true; }
        return false;
    }""")
    return bool(clicked)


def click_calendar_date(frame, day_number):
    """
    Click a specific day number in the date filter calendar.
    Returns True if clicked a non-greyed-out date.
    """
    clicked = frame.evaluate(f"""(day) => {{
        const links = Array.from(document.querySelectorAll(
            'a.ui-state-default, a[class*="ui-state-default"], ' +
            'td[class*="day"] a, [class*="calendar-day"] a, ' +
            '.p-datepicker-calendar td a'
        ));
        for (const a of links) {{
            if (a.textContent.trim() === String(day)
                    && !a.classList.contains('ui-state-disabled')
                    && a.offsetParent !== null) {{
                a.click();
                return true;
            }}
        }}
        return false;
    }}""", day_number)
    return bool(clicked)


def click_date_filter_toggle(frame):
    """Open or close the DATE FILTER calendar panel."""
    try:
        # The DATE FILTER button text appears twice (open + close), just click first visible
        frame.evaluate(r"""() => {
            const all = Array.from(document.querySelectorAll('*'));
            for (const el of all) {
                if (el.children.length === 0
                        && el.textContent.trim() === 'DATE FILTER'
                        && el.offsetParent !== null) {
                    el.click();
                    return true;
                }
            }
            return false;
        }""")
    except:
        pass


def navigate_to_results_history(frame):
    """
    Navigate to Football League → Results History (England default).
    Does NOT click any league tab — caller handles that.
    Returns True when Results History is loaded.
    """
    print(f"\n  📂 Navigating to Football League → Results History")

    # Step 1: Expand Football League submenu
    if not expand_football_league(frame):
        return False
    human_pause(400, 700)

    # Step 2: Click Results History inside Football League's submenu (JS-scoped)
    rh_clicked = click_results_history_in_submenu(frame)
    if not rh_clicked:
        # Playwright fallback — iterate all visible Results History links
        print("    ↺ JS click failed — trying Playwright fallback")
        try:
            els = frame.locator("a:has-text('Results History')").all()
            for el in els:
                try:
                    if el.is_visible(timeout=800):
                        el.scroll_into_view_if_needed()
                        el.click()
                        rh_clicked = True
                        print("    ✓ Clicked Results History via Playwright fallback")
                        break
                except:
                    continue
        except:
            pass

    if not rh_clicked:
        print("    ⚠ Could not click Results History — dumping frame")
        dump_frame_debug(frame, "results_history_fail")
        return False

    # Wait for Results History to load (URL should change)
    human_pause(2000, 3000)

    # Dump the Results History page so we can inspect the top-tab structure
    dump_frame_debug(frame, "results_history_loaded")
    print("    ✓ Results History loaded — dump saved")
    return True


def scrape_matches_structured(frame):
    """
    Parse all match results from the Results History page.

    Confirmed real body text format (from debug dump):
        Football League: England - Week 14 - 28/05/2026 16:25
        #3234031
        FOR

        1 : 0

        MUN

    Each match token block = match_id line, home_team line, blank, score line, blank, away_team line.
    Week headers appear before each group of 10 matches.
    """
    matches = []
    try:
        body = frame.evaluate("() => document.body.innerText || ''")
        if not body:
            return matches

        lines = [l.strip() for l in body.split('\n')]
        current_header = {"league": "", "week": 0, "match_datetime": ""}

        i = 0
        while i < len(lines):
            line = lines[i]

            # Week header: "Football League: England - Week 14 - 28/05/2026 16:25"
            hm = re.match(
                r'Football League:\s*(\w+)\s*[-–]\s*Week\s*(\d+)\s*[-–]\s*'
                r'(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2})',
                line
            )
            if hm:
                current_header = {
                    "league": hm.group(1),
                    "week": int(hm.group(2)),
                    "match_datetime": hm.group(3),
                }
                i += 1
                continue

            # Match ID line: "#3234031"
            mid_m = re.match(r'^#?(\d{5,})$', line)
            if mid_m:
                match_id = mid_m.group(1)
                # Collect non-blank tokens after the ID until we have home, score, away
                tokens = []
                j = i + 1
                while j < len(lines) and len(tokens) < 3:
                    t = lines[j].strip()
                    # Score line: "1 : 0"
                    if re.match(r'^\d+\s*:\s*\d+$', t):
                        tokens.append(t)
                    elif t and t != ' ':
                        tokens.append(t)
                    j += 1

                if len(tokens) >= 3:
                    home_team = tokens[0]
                    score_str = tokens[1]
                    away_team = tokens[2]
                    score_m = re.match(r'(\d+)\s*:\s*(\d+)', score_str)
                    if score_m and re.match(r'^[A-Z]{2,4}$', home_team) \
                               and re.match(r'^[A-Z]{2,4}$', away_team):
                        matches.append({
                            "match_id": match_id,
                            "home_team": home_team,
                            "home_score": int(score_m.group(1)),
                            "away_score": int(score_m.group(2)),
                            "away_team": away_team,
                            "league": current_header["league"],
                            "week": current_header["week"],
                            "match_datetime": current_header["match_datetime"],
                        })
                        i = j
                        continue
            i += 1

    except Exception as e:
        print(f"    ⚠ Scrape error: {e}")

    return matches


def scrape_match_details(frame, match_id):
    """
    Click/expand a match row to get detailed odds:
    Half Time/Full Time, Match Result, Double Chance, 1X2+O/U, 
    Correct Score, Total Goals, GG/NG, Over/Under, Multigoal, Handicaps.
    """
    details = {"match_id": match_id}
    try:
        # Click the match row to expand it (by match_id text or chevron)
        row_clicked = False
        try:
            row_el = frame.locator(f"text=#{match_id}").first
            row_el.click()
            row_clicked = True
            human_pause(500, 1000)
        except:
            # Try clicking the chevron/arrow near the match
            try:
                chevron = frame.locator(f"[data-id='{match_id}'], [id*='{match_id}']").first
                chevron.click()
                row_clicked = True
                human_pause(500, 1000)
            except:
                pass

        if not row_clicked:
            return details

        # Now scrape the expanded detail section
        detail_text = frame.evaluate(r"""(matchId) => {
            // Find expanded detail container
            const allText = document.body.innerText;
            // Get section after the match ID until the next match ID or end
            const regex = new RegExp('#' + matchId + '[\\s\\S]*?(?=#\\d{5,}|$)', 'i');
            const match = allText.match(regex);
            return match ? match[0].substring(0, 5000) : '';
        }""", match_id)

        if detail_text:
            # Parse key odds from the detail text
            details["raw_detail"] = detail_text[:3000]

            # Half Time / Full Time
            htm = re.search(r'HALF TIME/FULL TIME\s+([\w/]+):\s*([\d.]+)', detail_text)
            if htm:
                details["ht_ft"] = f"{htm.group(1)}: {htm.group(2)}"

            # Match Result
            mr = re.search(r'MATCH RESULT\s+(\w+):\s*([\d.]+)', detail_text)
            if mr:
                details["match_result"] = f"{mr.group(1)}: {mr.group(2)}"

            # Double Chance
            dc_matches = re.findall(r'DOUBLE CHANCE\s+([\w\d]+):\s*([\d.]+)\s+([\w\d]+):\s*([\d.]+)', detail_text)
            if dc_matches:
                details["double_chance"] = {dc_matches[0][0]: dc_matches[0][1], dc_matches[0][2]: dc_matches[0][3]}

            # Correct Score
            cs = re.search(r'CORRECT SCORE\s+([\d-]+):\s*([\d.]+)', detail_text)
            if cs:
                details["correct_score"] = f"{cs.group(1)}: {cs.group(2)}"

            # Total Goals
            tg = re.search(r'TOTAL GOALS\s+(\d+):\s*([\d.]+)', detail_text)
            if tg:
                details["total_goals"] = f"{tg.group(1)}: {tg.group(2)}"

            # GG/NG
            gg = re.search(r'GOAL GOAL/NO GOAL\s+(\w+):\s*([\d.]+)', detail_text)
            if gg:
                details["gg_ng"] = f"{gg.group(1)}: {gg.group(2)}"

            # Over/Under 2.5
            ou25 = re.search(r'OVER/UNDER 2\.5\s+(\w+\s*[\d.]+):\s*([\d.]+)', detail_text)
            if ou25:
                details["over_under_2_5"] = ou25.group(0).strip()

            # Parse all odds in a generic way — key: value pairs
            all_odds = re.findall(r'([A-Z][A-Z /.\d]+?):\s*([\d.]+)', detail_text)
            if all_odds:
                details["all_odds"] = {k.strip(): float(v) for k, v in all_odds}

        # Collapse the row (click again)
        try:
            row_el = frame.locator(f"text=#{match_id}").first
            row_el.click()
            human_pause(300, 600)
        except:
            pass

    except Exception as e:
        details["error"] = str(e)[:200]

    return details


def click_load_more(frame):
    """Click 'Load more events' button. Returns True if clicked."""
    try:
        # Try various selectors for the load more button
        for selector in [
            "text=Load more events",
            "text=Load more",
            "button:has-text('Load more')",
            "[class*='load-more']",
            "text=load more events",
        ]:
            try:
                btn = frame.locator(selector).first
                if btn.is_visible(timeout=3000):
                    btn.scroll_into_view_if_needed()
                    human_pause(300, 600)
                    btn.click()
                    human_pause(1500, 3000)
                    print("    ✓ Clicked 'Load more events'")
                    return True
            except:
                continue
    except:
        pass
    return False


def already_on_results_history(frame):
    """Return True if the Results History page is currently loaded."""
    try:
        url = frame.url or ""
        return "history" in url
    except:
        return False


# Matches per full virtual season per league (rounds × matches_per_round)
# England/Italy/Spain: 20 teams → 38 rounds × 10 = 380
# France/Germany/Turkey: 18 teams → 34 rounds × 9 = 306
MATCHES_PER_SEASON = {
    "England": 380, "Italy": 380, "Spain": 380,
    "France": 306, "Germany": 306, "Turkey": 306,
}


def assign_season_numbers(matches):
    """
    Walk matches in the order collected (newest → oldest) and assign season numbers.
    Season boundary = when week number INCREASES going back in time
    (e.g. week 1 → week 38 of the prior season).
    Season 1 = most recent season.
    """
    if not matches:
        return matches
    season = 1
    prev_week = matches[0].get("week", 0)
    for m in matches:
        w = m.get("week", 0)
        if w and prev_week and w > prev_week:
            season += 1
        m["season"] = season
        if w:
            prev_week = w
    return matches


HEARTBEAT = None  # set by app.py's watchdog; called at scrape progress points


def _heartbeat():
    if HEARTBEAT:
        try:
            HEARTBEAT()
        except Exception:
            pass


def _scrape_and_load_until(frame, league_name, seen_ids, all_matches,
                            target_total, date_label=""):
    """
    Scrape visible matches then keep clicking Load More until either:
      - all_matches reaches target_total, OR
      - Load More button disappears (no more data on this date).
    Returns number of NEW matches added this call.
    """
    added_total = 0
    click_count = 0

    def _absorb_batch():
        nonlocal added_total
        batch = scrape_matches_structured(frame)
        added = 0
        for m in batch:
            mid = m.get("match_id", "")
            if mid and mid not in seen_ids:
                m["league"] = league_name
                m["date_label"] = date_label
                all_matches.append(m)
                seen_ids.add(mid)
                added += 1
                added_total += 1
        return added

    _absorb_batch()
    print(f"    Initial visible: {added_total} new  |  total so far: {len(all_matches)}")

    while len(all_matches) < target_total:
        _check_pause()
        _heartbeat()
        if not click_load_more(frame):
            print(f"    ℹ Load More exhausted after {click_count} clicks")
            break
        click_count += 1
        human_pause(1200, 2000)
        added = _absorb_batch()
        seasons_done = len(all_matches) / max(target_total, 1) * (target_total / MATCHES_PER_SEASON.get(league_name, 380))
        print(f"    Click {click_count}: +{added}  |  total {len(all_matches)}  "
              f"|  ~{len(all_matches) / MATCHES_PER_SEASON.get(league_name, 380):.1f} seasons")
        if added == 0:
            print(f"    ℹ No new matches returned — stopping Load More")
            break

    return added_total


def scrape_match_detail_dropdown(frame, match_id):
    """
    Expand the match row dropdown and scrape every market box shown.

    Confirmed UI (from screenshot):
      Each market is a labeled card:
        Title: "HALF TIME/FULL TIME"
        Values: "2/X: 17.9"   or multiple pairs like "1X: 2.30  X2: 1.12"

    Markets captured (all labelled cards visible in the expanded panel):
      half_time_full_time, match_result, double_chance,
      1x2_ou_1_5, 1x2_ou_2_5, 1x2_ou_3_5,
      correct_score, total_goals, goal_goal_no_goal,
      over_under_2_5, over_under_3_5, over_under_4_5,
      multigoal, handicap_away_*, handicap_home_*

    Returns a structured dict — no raw text blobs.
    """
    detail = {"match_id": match_id}
    try:
        # ── Step 1: click the row to expand ──────────────────────────────
        clicked = frame.evaluate(f"""(mid) => {{
            // Find the span/div containing exactly "#<mid>"
            const all = Array.from(document.querySelectorAll('span, div, td'));
            for (const el of all) {{
                if (el.children.length === 0
                        && el.textContent.trim() === '#' + mid
                        && el.offsetParent !== null) {{
                    // Walk up to the clickable row container
                    const row = el.closest(
                        '[class*="event"], [class*="ticket"], [class*="result-row"], ' +
                        '[class*="match"], [class*="history-row"], tr, li'
                    );
                    if (row) {{
                        row.click();
                        return true;
                    }}
                    el.click();
                    return true;
                }}
            }}
            return false;
        }}""", match_id)

        if not clicked:
            detail["error"] = "row not found"
            return detail

        human_pause(700, 1200)   # wait for panel to animate open

        # ── Step 2: scrape every market card in the expanded panel ────────
        markets = frame.evaluate(rf"""(mid) => {{
            // Find the expanded detail container — it appears below the row
            // Strategy: gather all visible label+value boxes after the match row
            const results = {{}};

            // The panel uses a grid of cards. Each card has a title div
            // and one or more value divs underneath.
            // Try to find the container that appeared after clicking.
            const allLabels = Array.from(document.querySelectorAll(
                '[class*="market-name"], [class*="market-title"], [class*="label"], ' +
                '[class*="bet-type"], [class*="market-header"], [class*="type-name"]'
            ));

            for (const label of allLabels) {{
                const title = label.textContent.trim().toUpperCase();
                if (!title || title.length < 2) continue;

                // Gather sibling / child value elements
                const parent = label.parentElement;
                if (!parent) continue;
                const valueEls = parent.querySelectorAll(
                    '[class*="odd"], [class*="value"], [class*="price"], ' +
                    '[class*="outcome"], [class*="coeff"]'
                );
                const pairs = {{}};
                valueEls.forEach(v => {{
                    const txt = v.textContent.trim();
                    // Format: "KEY: 1.23"  or  "KEY VALUE"
                    const m = txt.match(/^(.+?)[:]\s*([\d]+\.[\d]+)$/);
                    if (m) pairs[m[1].trim()] = parseFloat(m[2]);
                    else {{
                        const m2 = txt.match(/^(.+?)\s+([\d]+\.[\d]+)$/);
                        if (m2) pairs[m2[1].trim()] = parseFloat(m2[2]);
                    }}
                }});
                if (Object.keys(pairs).length > 0) results[title] = pairs;
            }}

            // ── Fallback: parse the innerText block after the match row ──
            if (Object.keys(results).length === 0) {{
                const bodyText = document.body.innerText;
                const start = bodyText.indexOf('#' + mid);
                if (start !== -1) {{
                    const nextId = bodyText.substring(start + mid.length + 2).search(/#\\d{{5,}}/);
                    const block = bodyText.substring(
                        start,
                        nextId === -1 ? start + 5000 : start + mid.length + 2 + nextId
                    );
                    results['_raw_block'] = block.substring(0, 3000);
                }}
            }}

            return results;
        }}""", match_id)

        # ── Step 3: parse fallback raw block if structured parse gave nothing ──
        if markets and "_raw_block" in markets:
            raw = markets.pop("_raw_block", "")
            detail["_raw"] = raw  # keep for manual inspection

            # Parse labelled sections: TITLE\nKEY: VALUE  ...
            section = None
            for line in raw.split("\n"):
                line = line.strip()
                if not line:
                    continue
                # All-caps label line (market title)
                if re.match(r'^[A-Z][A-Z /+0-9.]{3,}$', line):
                    section = line
                    markets[section] = {}
                    continue
                # Value pairs: "2/X: 17.9"  "1X: 2.30"  "UN 2.5: 2.30"
                pairs_found = re.findall(r'([A-Za-z0-9/+\-. ]{1,20}?):\s*([\d]+\.[\d]+)', line)
                if pairs_found and section:
                    for k, v in pairs_found:
                        k = k.strip()
                        if k:
                            markets[section][k] = float(v)

        # Map raw market titles to clean JSON keys
        KEY_MAP = {
            "HALF TIME/FULL TIME":          "half_time_full_time",
            "MATCH RESULT":                 "match_result",
            "DOUBLE CHANCE":                "double_chance",
            "1X2 + OVER/UNDER 1.5":         "1x2_ou_1_5",
            "1X2 + OVER/UNDER 2.5":         "1x2_ou_2_5",
            "1X2 + OVER/UNDER 3.5":         "1x2_ou_3_5",
            "CORRECT SCORE":                "correct_score",
            "TOTAL GOALS":                  "total_goals",
            "GOAL GOAL/NO GOAL":            "goal_goal_no_goal",
            "OVER/UNDER 2.5":               "over_under_2_5",
            "OVER/UNDER 3.5":               "over_under_3_5",
            "OVER/UNDER 4.5":               "over_under_4_5",
            "MULTIGOAL":                    "multigoal",
        }
        for raw_key, clean_key in KEY_MAP.items():
            if raw_key in markets:
                detail[clean_key] = markets.pop(raw_key)

        # Handicap away / home — group by line name
        hcap_away, hcap_home = {}, {}
        for k in list(markets.keys()):
            if "HANDICAP AWAY" in k:
                val = k.replace("HANDICAP AWAY", "").strip()
                hcap_away.update(markets.pop(k, {}))
            elif "HANDICAP HOME" in k:
                val = k.replace("HANDICAP HOME", "").strip()
                hcap_home.update(markets.pop(k, {}))
        if hcap_away:
            detail["handicap_away"] = hcap_away
        if hcap_home:
            detail["handicap_home"] = hcap_home

        # Any remaining markets go in verbatim
        detail.update(markets)

        # ── Step 4: collapse the row ──────────────────────────────────────
        frame.evaluate(f"""(mid) => {{
            const all = Array.from(document.querySelectorAll('span, div, td'));
            for (const el of all) {{
                if (el.children.length === 0
                        && el.textContent.trim() === '#' + mid
                        && el.offsetParent !== null) {{
                    const row = el.closest(
                        '[class*="event"], [class*="ticket"], [class*="result-row"], ' +
                        '[class*="match"], [class*="history-row"], tr, li'
                    );
                    if (row) {{ row.click(); break; }}
                }}
            }}
        }}""", match_id)
        human_pause(300, 500)

    except Exception as e:
        detail["error"] = str(e)[:150]
    return detail


def scrape_league_results(frame, league_name, target_seasons=5,
                          first_league=True, detail_seasons=0,
                          target_matches=None):
    """
    Scrape Results History for one league.

    target_seasons  — keep clicking Load More + navigate calendar back until
                      we have this many seasons of match results.
    detail_seasons  — additionally expand each match for full dropdown detail
                      (only for the most recent N seasons). 0 = skip.
    target_matches  — override the match target directly (e.g. ~30 for a
                      light "latest gameweeks only" incremental scrape).

    Season detection: week number resets (38→1 going forward in time) mark
    season boundaries. Season 1 = most recent.
    """
    from datetime import date as _date, timedelta as _td

    mps = MATCHES_PER_SEASON.get(league_name, 380)
    if target_matches is None:
        target_matches = target_seasons * mps
    print(f"\n  🎯 Target: {target_matches} matches (~{target_matches / mps:.2f} seasons)")

    # ── Navigate to Results History ────────────────────────────────────────
    if first_league or not already_on_results_history(frame):
        if not navigate_to_results_history(frame):
            print(f"  ❌ Failed to navigate to Results History")
            return [], []

    print(f"  🔀 Selecting league tab: {league_name}")
    if not click_league_top_tab_js(frame, league_name):
        if not navigate_to_results_history(frame):
            return [], []
        if not click_league_top_tab_js(frame, league_name):
            print(f"  ❌ Could not select league: {league_name}")
            return [], []
    human_pause(1500, 2000)

    all_matches = []
    seen_ids    = set()
    today       = _date.today()
    day_offset  = 0

    # ── Keep going back day by day until we hit the target ────────────────
    while len(all_matches) < target_matches:
        _check_pause()
        _heartbeat()

        if day_offset == 0:
            label = "today"
            print(f"\n  📅 Day 0 (today): scraping {league_name}...")
        else:
            target_date = today - _td(days=day_offset)
            label = target_date.strftime("%Y-%m-%d")
            print(f"\n  📅 Day -{day_offset} ({label}): navigating date filter...")

            # Handle month boundary
            prev_date = today - _td(days=day_offset - 1)
            if target_date.month != prev_date.month:
                print(f"    ← Clicking previous month")
                if not click_calendar_prev_month(frame):
                    print(f"    ⚠ Could not go to previous month — stopping")
                    break
                human_pause(800, 1200)

            if not click_calendar_date(frame, target_date.day):
                print(f"    ⚠ Could not click day {target_date.day} — skipping")
                day_offset += 1
                continue
            human_pause(2000, 3000)

            # Date click resets to England — re-select league
            click_league_top_tab_js(frame, league_name)
            human_pause(1000, 1500)

        human_pause(1000, 1500)
        _scrape_and_load_until(frame, league_name, seen_ids, all_matches,
                               target_matches, date_label=label)
        day_offset += 1

        # Safety: don't go back more than 14 days
        if day_offset > 14:
            print(f"  ⚠ Reached 14-day lookback limit with {len(all_matches)} matches")
            break

    # ── Assign season numbers ──────────────────────────────────────────────
    assign_season_numbers(all_matches)
    seasons_found = max((m.get("season", 1) for m in all_matches), default=1)
    print(f"\n  ✅ {league_name}: {len(all_matches)} matches across "
          f"~{seasons_found} seasons (target was {target_seasons})")

    # ── Optional: scrape detailed dropdown for first N seasons ────────────
    detail_records = []
    if detail_seasons > 0:
        detail_matches = [m for m in all_matches
                         if m.get("season", 99) <= detail_seasons]
        print(f"\n  🔍 Scraping dropdown detail for {len(detail_matches)} matches "
              f"(seasons 1–{detail_seasons})...")
        for idx, m in enumerate(detail_matches):
            _check_pause()
            d = scrape_match_detail_dropdown(frame, m["match_id"])
            # Merge basic fields into detail record
            d.update({k: m[k] for k in
                      ("home_team","away_team","home_score","away_score",
                       "week","season","match_datetime","league")
                      if k in m})
            detail_records.append(d)
            if (idx + 1) % 20 == 0:
                print(f"    Detail: {idx + 1}/{len(detail_matches)} done")

    return all_matches, detail_records


def save_to_json(data, league_name, ts):
    """Save league data to JSON file."""
    filename = OUTPUT_JSON.format(league=league_name.lower(), ts=ts)
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  💾 Saved JSON: {filename} ({len(data)} matches)")
    return filename


def save_all_to_excel(all_data, ts):
    """Save all leagues to a single Excel file with one sheet per league."""
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    except ImportError:
        print("  ⚠ openpyxl not installed. Run: pip install openpyxl")
        print("    Saving as CSV instead...")
        # Fallback to CSV
        filename = f"sportybet_results_all_{ts}.csv"
        all_flat = []
        for league, matches in all_data.items():
            for m in matches:
                m["scraped_league"] = league
                all_flat.append(m)
        if all_flat:
            keys = list(all_flat[0].keys())
            with open(filename, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=keys)
                w.writeheader()
                w.writerows(all_flat)
        print(f"  💾 Saved CSV: {filename}")
        return filename

    filename = OUTPUT_XLSX.format(ts=ts)
    wb = openpyxl.Workbook()
    # Remove default sheet
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill("solid", fgColor="2F5233")
    thin_border = Border(
        left=Side(style="thin"), right=Side(style="thin"),
        top=Side(style="thin"), bottom=Side(style="thin"),
    )

    # Base columns for match results
    base_cols = ["match_id", "league", "week", "match_datetime",
                 "home_team", "home_score", "away_score", "away_team"]

    for league, matches in all_data.items():
        if not matches:
            continue
        ws = wb.create_sheet(title=league[:31])

        # Determine columns — base + any extra keys from details
        extra_keys = set()
        for m in matches:
            for k in m.keys():
                if k not in base_cols and k not in ("raw_detail", "raw_text", "error", "all_odds"):
                    extra_keys.add(k)
        cols = base_cols + sorted(extra_keys)

        # Write header
        for ci, col in enumerate(cols, 1):
            cell = ws.cell(row=1, column=ci, value=col.replace("_", " ").title())
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center")
            cell.border = thin_border

        # Write data
        for ri, match in enumerate(matches, 2):
            for ci, col in enumerate(cols, 1):
                val = match.get(col, "")
                if isinstance(val, dict):
                    val = json.dumps(val)
                cell = ws.cell(row=ri, column=ci, value=val)
                cell.border = thin_border

        # Auto-width columns
        for ci, col in enumerate(cols, 1):
            max_len = max(len(str(col)), *[len(str(m.get(col, ""))[:50]) for m in matches[:50]])
            ws.column_dimensions[openpyxl.utils.get_column_letter(ci)].width = min(max_len + 4, 40)

    # Summary sheet
    ws_sum = wb.create_sheet(title="Summary", index=0)
    ws_sum.cell(row=1, column=1, value="League").font = header_font
    ws_sum.cell(row=1, column=1).fill = header_fill
    ws_sum.cell(row=1, column=2, value="Total Matches").font = header_font
    ws_sum.cell(row=1, column=2).fill = header_fill
    ws_sum.cell(row=1, column=3, value="Scraped At").font = header_font
    ws_sum.cell(row=1, column=3).fill = header_fill
    for ri, (league, matches) in enumerate(all_data.items(), 2):
        ws_sum.cell(row=ri, column=1, value=league)
        ws_sum.cell(row=ri, column=2, value=len(matches))
        ws_sum.cell(row=ri, column=3, value=ts)

    wb.save(filename)
    print(f"  💾 Saved Excel: {filename}")
    return filename


# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════

def main():
    print("\n" + "═" * 65)
    print(f"  {BOT_NAME}")
    print("═" * 65)

    # Hardcoded: direct connection, 15s cooldown, advanced stealth
    use_cooldown  = True
    stealth_mode  = "advanced"

    # ── OPTION 1: Headless ──
    print("\n  1. Run headless? [y/N]")
    c = input_with_timeout("  > ", 86400)
    headless = c.lower() == "y"

    # ── OPTION 2: Leagues to scrape ──
    print(f"\n  2. Leagues: [A] All  [S] Select specific")
    c = input_with_timeout("  > ", 86400)
    if c.lower() == "s":
        print("    Available: " + ", ".join(f"[{i+1}] {l}" for i, l in enumerate(LEAGUES)))
        sel = input_with_timeout("    Enter numbers (e.g. 1,3,5): ", 86400)
        selected_leagues = []
        for n in sel.split(","):
            n = n.strip()
            if n.isdigit() and 1 <= int(n) <= len(LEAGUES):
                selected_leagues.append(LEAGUES[int(n) - 1])
        if not selected_leagues:
            selected_leagues = LEAGUES[:]
    else:
        selected_leagues = LEAGUES[:]
    print(f"    → Scraping: {', '.join(selected_leagues)}")

    # ── OPTION 3: How many seasons of history? ──
    print(f"\n  3. How many virtual seasons of history per league?")
    print(f"     England/Italy/Spain = 380 matches/season  "
          f"| France/Germany/Turkey = 306 matches/season")
    print(f"     Scraper auto-clicks Load More + navigates calendar until target is hit.")
    print(f"     [1] 1 season  [2] 2 seasons  [3] 5 seasons  "
          f"[4] 10 seasons  [5] 20 seasons  [6] 50 seasons")
    while True:
        c = input_with_timeout("  > ", 86400)
        if c in ("1", "2", "3", "4", "5", "6"): break
    target_seasons = {"1": 1, "2": 2, "3": 5, "4": 10, "5": 20, "6": 50}[c]

    # ── OPTION 4: Detailed dropdown data? ──
    print(f"\n  4. Also scrape detailed match dropdown (odds, HT score, etc.)?")
    print(f"     This opens each match row and scrapes all extra data.")
    print(f"     [0] No   [1] For most recent 1 season   [2] 2 seasons   [3] 3 seasons")
    while True:
        c = input_with_timeout("  > ", 86400)
        if c in ("0", "1", "2", "3"): break
    detail_seasons = int(c)

    # ── Ready ──
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    print(f"\n  📋 Scraping {len(selected_leagues)} leagues | "
          f"{target_seasons} seasons each | detail: {detail_seasons} seasons")
    print(f"  📁 Output prefix: sportybet_results_*_{ts}")
    print()

    pw = None
    all_league_data = {}
    failed_details = []

    try:
        pw, browser, ctx, page = create_stealth_browser(
            headless=headless, proxy_server=None, stealth_mode=stealth_mode
        )

        # Navigate to SportyBet Virtual
        print(f"\n  🌐 Opening {SITE_URL}")
        page.goto(SITE_URL, timeout=60000, wait_until="networkidle")
        _smart_sleep(8)   # let the virtual iframe fully initialise

        # Find the virtual iframe
        frame = wait_and_find_virtual_frame(page, timeout=45000)

        for li, league in enumerate(selected_leagues):
            _check_pause()

            print(f"\n{'─' * 60}")
            print(f"  LEAGUE {li + 1}/{len(selected_leagues)}: {league}")
            print(f"{'─' * 60}")

            try:
                matches, details = scrape_league_results(
                    frame, league,
                    target_seasons=target_seasons,
                    first_league=(li == 0),
                    detail_seasons=detail_seasons,
                )
                all_league_data[league] = matches

                if matches:
                    save_to_json(matches, league, ts)
                    if details:
                        det_file = f"sportybet_detail_{league.lower()}_{ts}.json"
                        with open(det_file, "w", encoding="utf-8") as f:
                            json.dump(details, f, indent=2, ensure_ascii=False)
                        print(f"  💾 Saved detail JSON: {det_file} ({len(details)} records)")
                else:
                    print(f"  ⚠ No matches found for {league}")
                    failed_details.append(f"{league} | 0 matches found")

            except Exception as e:
                print(f"  ❌ Error scraping {league}: {e}")
                traceback.print_exc()
                all_league_data[league] = []
                failed_details.append(f"{league} | ERROR: {str(e)[:80]}")

            # Cooldown between leagues
            if use_cooldown and li < len(selected_leagues) - 1:
                print(f"\n  ⏳ Cooldown {COOLDOWN_SECS}s before next league...")
                time.sleep(COOLDOWN_SECS)

        # Save all to Excel
        if any(v for v in all_league_data.values()):
            save_all_to_excel(all_league_data, ts)

    except Exception as e:
        print(f"\n  ❌ FATAL: {e}")
        traceback.print_exc()
        failed_details.append(f"FATAL | {str(e)[:100]}")

    finally:
        try:
            if pw:
                browser.close()
                pw.stop()
        except:
            pass

    # ═══════════════════════════════════════════════════════
    #  FINAL SUMMARY
    # ═══════════════════════════════════════════════════════
    print(f"\n\n{'═' * 65}")
    print(f"  SCRAPE SUMMARY")
    print(f"{'═' * 65}")
    total = sum(len(v) for v in all_league_data.values())
    for league, matches in all_league_data.items():
        icon = "✅" if matches else "❌"
        mps  = MATCHES_PER_SEASON.get(league, 380)
        seas = max((m.get("season", 1) for m in matches), default=0) if matches else 0
        print(f"  {icon} {league}: {len(matches)} matches  (~{len(matches)/mps:.1f} seasons, "
              f"season range 1–{seas})")
    print(f"\n  Total: {total} matches across {len(all_league_data)} leagues")

    if failed_details:
        print(f"\n  ❌ FAILURES:")
        for f in failed_details:
            print(f"     • {f}")
    else:
        print(f"\n  ✅ All leagues scraped successfully!")

    print(f"{'═' * 65}\n")


if __name__ == "__main__":
    main()
