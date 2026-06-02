"""
╔══════════════════════════════════════════════════════════════════╗
║  SPORTYBET VIRTUAL RACING — Historical Results Scraper  (v2)     ║
║  ─────────────────────────────────────────────────────────────── ║
║  Site:   https://www.sportybet.com/ng/virtual/                   ║
║  Target: Greyhound Racing, Horse Racing,                         ║
║          Speedway Racing, Motorbike Racing → Results History      ║
║                                                                  ║
║  Markets per race:                                               ║
║    WIN · PLACE · EXACTA · EVEN/ODD · OVER/UNDER                  ║
║    QUINELLA · TRIFECTA · SHOW                                    ║
║                                                                  ║
║  Features:                                                       ║
║    ✓ Stealth browser (Basic / Advanced 16-layer)                 ║
║    ✓ Proxy (anyip.io) or system VPN                              ║
║    ✓ Headless toggle                                             ║
║    ✓ Iframe detection + frame switching                          ║
║    ✓ Runner badge + finish position per race                     ║
║    ✓ All 8 betting markets per race                              ║
║    ✓ "Load more events" pagination                               ║
║    ✓ Date filter navigation for historical data                  ║
║    ✓ Output: JSON + Excel (.xlsx)                                ║
║    ✓ Ctrl+C pause/resume  (Ctrl+C again = force quit)            ║
║    ✓ Final summary with any failures                             ║
║                                                                  ║
║  Run:   python sportybet_virtual_racing_scraper_v2.py            ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os, json, csv, random, signal, time, traceback, re
import threading, queue
from datetime import datetime, timedelta, date as _date
from playwright.sync_api import sync_playwright

# ═══════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════

BOT_NAME      = "SPORTYBET VIRTUAL SCRAPER — Part 3: Racing Results (v2)"
SITE_URL      = "https://www.sportybet.com/ng/virtual/"
PROXY_ADDR    = "http://portal.anyip.io:2000"
COOLDOWN_SECS = 15

RACING_SPORTS = [
    "Greyhound Racing",
    "Horse Racing",
    "Speedway Racing",
    "Motorbike Racing",
]

OUTPUT_JSON = "sportybet_racing_{sport}_{ts}.json"
OUTPUT_XLSX = "sportybet_racing_all_{ts}.xlsx"


# ═══════════════════════════════════════════════════════════
#  BACKGROUND INPUT THREAD  (proven from football scraper)
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
#  Fix: signal handler only sets a flag.  The main loop and
#  _smart_sleep() poll the flag so Playwright never blocks it.
#  Second Ctrl+C while paused = immediate os._exit(0).
# ═══════════════════════════════════════════════════════════
_pause = False

def _ph(sig, frame):
    global _pause
    if _pause:
        # Already paused — second Ctrl+C = force kill
        print("\n  ✋ Force quit.")
        os._exit(0)
    _pause = True
    print("\n\n  ⏸  PAUSED — press ENTER to resume, Ctrl+C to quit.")

signal.signal(signal.SIGINT, _ph)


def _check_pause():
    """Call inside tight loops. If paused, blocks until resumed or force-quit."""
    global _pause
    if not _pause:
        return
    # We're paused — wait for user input from the background thread
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
    """
    Sleep in 200 ms chunks so Ctrl+C / pause can fire.
    Replaces page.wait_for_timeout() for all delays.
    """
    end = time.time() + secs
    while time.time() < end:
        _check_pause()
        time.sleep(min(0.2, max(0, end - time.time())))


# ═══════════════════════════════════════════════════════════
#  STEALTH BROWSER  (copied 1-to-1 from football scraper)
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
        "channel": "chrome",
        "ignore_default_args": ["--enable-automation"],
    }
    if proxy_server:
        launch_kwargs["proxy"] = {"server": proxy_server}
    browser = pw.chromium.launch(**launch_kwargs)
    ctx_kwargs = {
        "viewport": {"width": 1280, "height": 900},
        "locale": "en-US",
        "timezone_id": "America/New_York",
        "extra_http_headers": {"Accept-Language": "en-US,en;q=0.9"},
    }
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


def safe_text(frame):
    try: return frame.evaluate("() => document.body.innerText").lower()
    except: return ""


def dump_frame_debug(frame, label="debug"):
    """Dump frame text + clickable elements to a file for selector diagnosis."""
    try:
        data = frame.evaluate(r"""() => {
            const clickable = Array.from(document.querySelectorAll(
                'a, button, [role="button"], li, [class*="menu"], [class*="nav"], ' +
                '[class*="item"], [class*="tab"], [class*="link"], [class*="sport"], ' +
                '[class*="race"], [class*="runner"], [class*="category"], [class*="toggler"]'
            )).map(el => ({
                tag: el.tagName,
                cls: el.className.substring(0, 80),
                text: (el.innerText || el.textContent || "").trim().substring(0, 100),
                visible: el.offsetParent !== null,
            })).filter(e => e.text.length > 0);
            // Extra: dump all togglers with their sub-UL heights
            const togglerInfo = Array.from(document.querySelectorAll('a.toggler, a[class*="toggler"]'))
                .map(a => {
                    const sub = a.nextElementSibling;
                    return {
                        text: a.textContent.trim(),
                        cls: a.className,
                        subTag: sub ? sub.tagName : 'none',
                        subH: sub ? sub.offsetHeight : -1,
                        subCls: sub ? sub.className.substring(0, 80) : '',
                    };
                });
            return {
                body_text: (document.body.innerText || "").substring(0, 5000),
                clickable: clickable,
                togglers: togglerInfo,
                title: document.title,
                url: location.href,
            };
        }""")
        filename = f"debug_racing_{label}.txt"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(f"URL: {data.get('url','')}\n")
            f.write(f"Title: {data.get('title','')}\n\n")
            f.write("=== TOGGLER SUB-UL HEIGHTS ===\n")
            for t in data.get("togglers", []):
                f.write(f"  toggler='{t['text']}' cls={t['cls']}\n")
                f.write(f"    nextSibling: tag={t['subTag']} offsetH={t['subH']} cls={t['subCls']}\n")
            f.write("\n=== BODY TEXT ===\n")
            f.write(data.get("body_text", "") + "\n\n")
            f.write("=== CLICKABLE ELEMENTS ===\n")
            for el in data.get("clickable", []):
                vis = "VISIBLE" if el["visible"] else "hidden"
                f.write(f"  [{vis}] <{el['tag']}> cls={el['cls']} | {el['text']}\n")
        print(f"  📄 Debug dump: {filename}")
    except Exception as e:
        print(f"  ⚠ Debug dump failed: {e}")


MAIN_PAGE_URL = "https://www.sportybet.com/ng/virtual/"

def wait_and_find_virtual_frame(page, timeout=45000):
    """Wait for the virtual games iframe and return it."""
    start = time.time()
    last_log = 0
    while (time.time() - start) * 1000 < timeout:
        _check_pause()
        if time.time() - last_log > 3:
            all_urls = [f.url for f in page.frames if f.url]
            print(f"  ℹ All frames ({len(all_urls)}): " + " | ".join(u[:70] for u in all_urls))
            last_log = time.time()
        for f in page.frames:
            url = f.url or ""
            if (url and url != "about:blank"
                    and "sportybet.com/ng/virtual" not in url
                    and url != MAIN_PAGE_URL
                    and len(url) > 10):
                try:
                    body = f.evaluate("() => document.body ? document.body.innerText.length : 0")
                    if body and body > 50:
                        print(f"  ✓ Found virtual frame: {url[:100]}")
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
                print(f"  ℹ iframe[src] in DOM: {srcs}")
        except:
            pass
        _smart_sleep(2)

    for f in page.frames:
        url = f.url or ""
        if url and url != "about:blank" and url != MAIN_PAGE_URL:
            print(f"  ⚠ Fallback: using frame {url[:100]}")
            return f

    print("  ⚠ No virtual iframe found — operating on main page")
    return page


# ═══════════════════════════════════════════════════════════
#  SIDEBAR NAVIGATION  (expand sport → click Results History)
# ═══════════════════════════════════════════════════════════

def expand_racing_sport(frame, sport_name):
    """
    Click the sport toggler in the sidebar to expand its submenu.

    DOM confirmed from debug dump:
      <A class="toggler text-overflow gr-icon-dogs ng-star-inserted collapsed">

    Angular animates the sub-UL open via CSS height/max-height — the 'collapsed'
    class on the <a> may or may not be removed.  The only reliable check is
    whether the sub-UL (toggler's nextElementSibling) has offsetHeight > 0.

    NEVER re-click while waiting — that would collapse the menu again.
    """
    # JS: find toggler, check if sub-UL already expanded
    _is_expanded = rf"""(sport) => {{
        const a = Array.from(document.querySelectorAll('a.toggler, a[class*="toggler"]'))
                    .find(el => el.textContent.trim() === sport);
        if (!a) return false;
        const sub = a.nextElementSibling;
        return sub ? sub.offsetHeight > 0 : false;
    }}"""

    # If already open, no need to click
    try:
        if frame.evaluate(_is_expanded, sport_name):
            print(f"    ✓ '{sport_name}' submenu already expanded")
            return True
    except:
        pass

    # Try Playwright click first (dispatches real mouse events Angular listens to)
    clicked = False
    selectors = [
        f"a.toggler:has-text('{sport_name}')",
        f"a[class*='toggler']:has-text('{sport_name}')",
    ]
    for sel in selectors:
        try:
            el = frame.locator(sel).first
            el.wait_for(state="visible", timeout=8000)
            el.scroll_into_view_if_needed()
            el.click()
            print(f"    ✓ Clicked '{sport_name}' toggler via: {sel}")
            clicked = True
            break
        except:
            continue

    # JS dispatch fallback — fires both click and mousedown/mouseup so Angular picks it up
    if not clicked:
        clicked = bool(frame.evaluate(rf"""(sport) => {{
            const a = Array.from(document.querySelectorAll('a.toggler, a[class*="toggler"]'))
                        .find(el => el.textContent.trim() === sport);
            if (!a) return false;
            ['mousedown','mouseup','click'].forEach(ev =>
                a.dispatchEvent(new MouseEvent(ev, {{bubbles: true, cancelable: true}})));
            return true;
        }}""", sport_name))
        if clicked:
            print(f"    ✓ Clicked '{sport_name}' toggler via JS dispatch")

    if not clicked:
        print(f"    ⚠ Could not click '{sport_name}' toggler")
        dump_frame_debug(frame, f"{sport_name.replace(' ', '_').lower()}_toggler_fail")
        return False

    # Wait for sub-UL height > 0  (up to 10 s)
    print(f"    ⏳ Waiting for '{sport_name}' submenu to expand...")
    for _ in range(20):
        _smart_sleep(0.5)
        try:
            if frame.evaluate(_is_expanded, sport_name):
                print(f"    ✓ '{sport_name}' submenu expanded")
                return True
        except:
            pass

    print(f"    ⚠ Submenu did not expand for '{sport_name}'")
    dump_frame_debug(frame, f"{sport_name.replace(' ', '_').lower()}_submenu_fail")
    return False


def click_results_history_for_sport(frame, sport_name):
    """
    Click the Results History link scoped to the given sport's submenu.
    Mirrors click_results_history_in_submenu() from the football scraper.
    """
    # JS-scoped click — find the sport toggler's parent <li>, then click
    # Results History only if it has actual rendered height (sub-UL is open).
    clicked = frame.evaluate(rf"""(sport) => {{
        const toggle = Array.from(document.querySelectorAll('a.toggler, a[class*="toggler"]'))
                         .find(a => a.textContent.trim() === sport);
        if (!toggle) return 'no-toggler';
        const li = toggle.closest('li');
        if (!li) return 'no-li';
        for (const a of li.querySelectorAll('a')) {{
            if (a.textContent.trim() === 'Results History') {{
                const r = a.getBoundingClientRect();
                if (r.height > 0 && r.width > 0) {{
                    a.click(); return 'clicked';
                }}
            }}
        }}
        return 'rh-not-visible';
    }}""", sport_name)

    if clicked == "clicked":
        return True

    print(f"    ℹ Results History scoped click returned: {clicked}")

    # Playwright fallback — use Playwright locator scoped inside the sport's li
    try:
        clicked2 = frame.evaluate(rf"""(sport) => {{
            const toggle = Array.from(document.querySelectorAll('a.toggler, a[class*="toggler"]'))
                             .find(a => a.textContent.trim() === sport);
            if (!toggle) return false;
            const li = toggle.closest('li');
            if (!li) return false;
            for (const a of li.querySelectorAll('a')) {{
                if (a.textContent.trim() === 'Results History') {{
                    a.click(); return true;
                }}
            }}
            return false;
        }}""", sport_name)
        if clicked2:
            print("    ✓ Clicked Results History via scoped fallback (no visibility check)")
            return True
    except:
        pass

    return False


def navigate_to_racing_results_history(frame, sport_name):
    """
    Full navigation: expand sport toggler → click Results History.
    Returns True when the Results History page is loaded.
    """
    print(f"\n  📂 Navigating to {sport_name} → Results History")

    if not expand_racing_sport(frame, sport_name):
        return False
    human_pause(800, 1200)   # let CSS slide animation finish before clicking

    if not click_results_history_for_sport(frame, sport_name):
        print(f"    ⚠ Could not click Results History for '{sport_name}'")
        dump_frame_debug(frame, f"rh_{sport_name.replace(' ', '_').lower()}_fail")
        return False

    human_pause(2500, 3500)
    dump_frame_debug(frame, f"rh_{sport_name.replace(' ', '_').lower()}_loaded")
    print(f"    ✓ Results History loaded for '{sport_name}'")
    return True


# ═══════════════════════════════════════════════════════════
#  DATE FILTER  (identical to football scraper)
# ═══════════════════════════════════════════════════════════

def click_calendar_prev_month(frame):
    return bool(frame.evaluate(r"""() => {
        const prev = document.querySelector(
            '.ui-datepicker-prev, [class*="datepicker-prev"], ' +
            '.p-datepicker-prev, [class*="prev-month"], ' +
            'a[class*="prev"], button[class*="prev"]'
        );
        if (prev) { prev.click(); return true; }
        const byAria = document.querySelector('[aria-label*="prev"], [aria-label*="Previous"]');
        if (byAria) { byAria.click(); return true; }
        return false;
    }"""))


def click_calendar_date(frame, day_number):
    return bool(frame.evaluate(f"""(day) => {{
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
    }}""", day_number))


# ═══════════════════════════════════════════════════════════
#  RACE RESULT PARSING
#  JS groups innerText lines by section keyword, then Python
#  parses each section into structured market data.
# ═══════════════════════════════════════════════════════════

_MARKET_KEYS = ('WIN', 'PLACE', 'EXACTA', 'EVEN/ODD', 'OVER/UNDER',
                'QUINELLA', 'TRIFECTA', 'SHOW')


def _is_badge(s):
    """Small integer 0–20 = likely a runner badge number."""
    try: v = int(s); return 0 <= v <= 20
    except: return False


def _is_odds(s):
    """Float ≥ 1.0 with decimal point = likely betting odds."""
    try: return '.' in str(s) and float(s) >= 1.0
    except: return False


def _badge_odds_pairs(tokens):
    pairs, i = [], 0
    while i < len(tokens) - 1:
        if _is_badge(tokens[i]) and _is_odds(tokens[i + 1]):
            pairs.append((tokens[i], float(tokens[i + 1])))
            i += 2
        else:
            i += 1
    return pairs


def _parse_runners(lines):
    """
    Parse runner section lines → [{finish_pos, badge, name}, ...]

    Each runner in innerText typically reads as:
        "1"          ← finish position (1st, 2nd, …)
        "3"          ← runner starting badge number
        "Capricorno" ← runner name
    Badge line is absent when the badge is an icon-only element.
    """
    runners, pending = [], []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if re.match(r'^\d+$', line):
            pending.append(int(line))
        else:
            if len(pending) >= 2:
                pos, badge = pending[0], str(pending[1])
            elif len(pending) == 1:
                pos, badge = pending[0], None
            else:
                pos, badge = len(runners) + 1, None
            runners.append({"finish_pos": pos, "badge": badge, "name": line})
            pending = []
    return runners


def _parse_win(lines):
    tokens = [l for l in lines if l.strip()]
    for i, t in enumerate(tokens):
        if _is_badge(t) and i + 1 < len(tokens) and _is_odds(tokens[i + 1]):
            return {"runner_badge": t, "odds": float(tokens[i + 1])}
    for t in tokens:
        if _is_odds(t): return {"runner_badge": None, "odds": float(t)}
    return {}


def _parse_place_or_show(lines):
    tokens = [l for l in lines if l.strip()]
    return [{"runner_badge": b, "odds": o} for b, o in _badge_odds_pairs(tokens)]


def _parse_combination(lines):
    """EXACTA / QUINELLA / TRIFECTA: badges then one odds value."""
    tokens = [l for l in lines if l.strip()]
    badges = [t for t in tokens if _is_badge(t)]
    odds_vals = [float(t) for t in tokens if _is_odds(t) and not _is_badge(t)]
    result = {"runners": badges}
    if odds_vals: result["odds"] = max(odds_vals)
    return result


def _parse_even_odd(lines):
    text = " ".join(lines)
    m = re.search(r'\b([OoEe])[\s:.]+([\d.]+)', text)
    if m: return {"result": m.group(1).upper(), "odds": float(m.group(2))}
    return {}


def _parse_over_under(lines):
    text = " ".join(lines)
    m = re.search(r'\b([OoUu])[\s:.]+([\d.]+)', text)
    if m: return {"result": m.group(1).upper(), "odds": float(m.group(2))}
    return {}


_MARKET_PARSERS = {
    "WIN":        _parse_win,
    "PLACE":      _parse_place_or_show,
    "EXACTA":     _parse_combination,
    "EVEN/ODD":   _parse_even_odd,
    "OVER/UNDER": _parse_over_under,
    "QUINELLA":   _parse_combination,
    "TRIFECTA":   _parse_combination,
    "SHOW":       _parse_place_or_show,
}


def _build_race(item, sport_name):
    sections = item.get("sections", {})
    runners  = _parse_runners(sections.get("RUNNERS", []))
    race = {
        "race_id":      item["race_id"],
        "sport":        item.get("sport", sport_name),
        "race_name":    item.get("race_name", ""),
        "race_date":    item.get("race_date", ""),
        "race_time":    item.get("race_time", ""),
        "runners":      runners,
        "winner":       runners[0]["name"]  if runners else "",
        "winner_badge": runners[0]["badge"] if runners else None,
        "markets":      {},
    }
    for key, fn in _MARKET_PARSERS.items():
        data = sections.get(key, [])
        if data:
            parsed = fn(data)
            if parsed:
                race["markets"][key.lower().replace("/", "_")] = parsed
    return race


def scrape_racing_page(frame, sport_name):
    """
    Parse all race results visible on the Results History page.

    Uses JS to collect innerText lines grouped by section keyword
    (RUNNERS, WIN, PLACE, EXACTA, EVEN/ODD, OVER/UNDER, QUINELLA,
    TRIFECTA, SHOW), then Python parses each section.

    Returns a list of race dicts, each with:
      race_id, sport, race_name, race_date, race_time,
      winner, winner_badge, runners[], markets{}
    """
    races = []
    try:
        raw = frame.evaluate(r"""() => {
            const MARKETS = ['WIN','PLACE','EXACTA','EVEN/ODD','OVER/UNDER',
                             'QUINELLA','TRIFECTA','SHOW'];
            const HDR = /(.+?):\s*(.+?)\s*[-\u2013]\s*#?(\d{5,})\s*[-\u2013]\s*(\d{2}\/\d{2}\/\d{4})\s+(\d{2}:\d{2})/;
            const lines = (document.body.innerText || '').split('\n').map(l => l.trim());
            const out = [];
            let cur = null, sec = null, sdata = {};

            const save = () => { if (cur && cur.race_id) { cur.sections = sdata; out.push(cur); } };

            for (const line of lines) {
                if (!line) continue;
                const hm = line.match(HDR);
                if (hm) {
                    save();
                    cur   = { race_id: hm[3], sport: hm[1].trim(),
                              race_name: hm[2].trim(), race_date: hm[4], race_time: hm[5] };
                    sec   = 'RUNNERS';
                    sdata = { RUNNERS: [] };
                    continue;
                }
                if (!cur) continue;
                if (MARKETS.includes(line.toUpperCase())) {
                    sec = line.toUpperCase();
                    if (!sdata[sec]) sdata[sec] = [];
                    continue;
                }
                if (sec) { if (!sdata[sec]) sdata[sec] = []; sdata[sec].push(line); }
            }
            save();
            return out;
        }""")

        seen = set()
        for item in (raw or []):
            rid = item.get("race_id", "")
            if not rid or rid in seen: continue
            seen.add(rid)
            race = _build_race(item, sport_name)
            if race["runners"] or race["markets"]:
                races.append(race)

    except Exception as e:
        print(f"    ⚠ Parse error: {e}")
        traceback.print_exc()

    return races


# ═══════════════════════════════════════════════════════════
#  LOAD MORE
# ═══════════════════════════════════════════════════════════

def click_load_more(frame):
    """Click 'Load more events' button. Returns True if clicked."""
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
                human_pause(1500, 2500)
                print("    ✓ Clicked 'Load more events'")
                return True
        except:
            continue
    return False


# ═══════════════════════════════════════════════════════════
#  MAIN SCRAPING LOOP
# ═══════════════════════════════════════════════════════════

def scrape_sport_results(frame, sport_name, target_days=7):
    """
    Scrape Results History for one racing sport across target_days days.
    Returns a list of race dicts.
    """
    print(f"\n  🎯 Target: {target_days} day(s) of results for {sport_name}")

    if not navigate_to_racing_results_history(frame, sport_name):
        print(f"  ❌ Failed to navigate to '{sport_name}' Results History")
        return []

    all_races = []
    seen_ids  = set()
    today     = _date.today()

    def _absorb(label=""):
        added = 0
        for r in scrape_racing_page(frame, sport_name):
            rid = r.get("race_id", "")
            if rid and rid not in seen_ids:
                r["date_label"] = label
                all_races.append(r)
                seen_ids.add(rid)
                added += 1
        return added

    for day_offset in range(target_days):
        _check_pause()

        if day_offset == 0:
            label = "today"
            print(f"\n  📅 Day 0 (today): scraping {sport_name}...")
        else:
            target_date = today - timedelta(days=day_offset)
            label = target_date.strftime("%Y-%m-%d")
            print(f"\n  📅 Day -{day_offset} ({label}): navigating date filter...")

            prev_date = today - timedelta(days=day_offset - 1)
            if target_date.month != prev_date.month:
                print(f"    ← Clicking previous month")
                if not click_calendar_prev_month(frame):
                    print(f"    ⚠ Could not go to previous month — stopping")
                    break
                human_pause(800, 1200)

            if not click_calendar_date(frame, target_date.day):
                print(f"    ⚠ Could not click day {target_date.day} — skipping")
                continue
            human_pause(2000, 3000)

        human_pause(1000, 1500)
        added = _absorb(label)

        lm_clicks = 0
        while lm_clicks < 30:
            _check_pause()
            if not click_load_more(frame):
                break
            lm_clicks += 1
            human_pause(1200, 2000)
            new = _absorb(label)
            added += new
            if new == 0 and lm_clicks > 2:
                break

        print(f"    Day -{day_offset}: +{added} new races  |  total {len(all_races)}")

    print(f"\n  ✅ {sport_name}: {len(all_races)} races over {target_days} day(s)")
    return all_races


# ═══════════════════════════════════════════════════════════
#  OUTPUT
# ═══════════════════════════════════════════════════════════

def save_to_json(data, sport_name, ts):
    safe = sport_name.lower().replace(" ", "_")
    filename = OUTPUT_JSON.format(sport=safe, ts=ts)
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  💾 Saved JSON: {filename} ({len(data)} races)")
    return filename


def _flatten_race(race, max_runners):
    """Flatten a race dict to a spreadsheet-ready row dict."""
    runners = race.get("runners", [])
    markets = race.get("markets", {})

    row = {
        "race_id":      race.get("race_id", ""),
        "sport":        race.get("sport", ""),
        "race_name":    race.get("race_name", ""),
        "race_date":    race.get("race_date", ""),
        "race_time":    race.get("race_time", ""),
        "winner":       race.get("winner", ""),
        "winner_badge": race.get("winner_badge", ""),
        "date_label":   race.get("date_label", ""),
    }

    for i in range(max_runners):
        n = i + 1
        rn = runners[i] if i < len(runners) else {}
        row[f"pos{n}_badge"] = rn.get("badge", "")
        row[f"pos{n}_name"]  = rn.get("name", "")

    win = markets.get("win", {})
    row["win_badge"] = win.get("runner_badge", "")
    row["win_odds"]  = win.get("odds", "")

    place = markets.get("place", [])
    for i in range(4):
        n = i + 1
        pl = place[i] if i < len(place) else {}
        row[f"place_{n}_badge"] = pl.get("runner_badge", "")
        row[f"place_{n}_odds"]  = pl.get("odds", "")

    ex = markets.get("exacta", {})
    row["exacta_runners"] = "-".join(ex.get("runners", []))
    row["exacta_odds"]    = ex.get("odds", "")

    eo = markets.get("even_odd", {})
    row["even_odd_result"] = eo.get("result", "")
    row["even_odd_odds"]   = eo.get("odds", "")

    ou = markets.get("over_under", {})
    row["over_under_result"] = ou.get("result", "")
    row["over_under_odds"]   = ou.get("odds", "")

    qu = markets.get("quinella", {})
    row["quinella_runners"] = "-".join(qu.get("runners", []))
    row["quinella_odds"]    = qu.get("odds", "")

    tri = markets.get("trifecta", {})
    row["trifecta_runners"] = "-".join(tri.get("runners", []))
    row["trifecta_odds"]    = tri.get("odds", "")

    show = markets.get("show", [])
    for i in range(4):
        n = i + 1
        sh = show[i] if i < len(show) else {}
        row[f"show_{n}_badge"] = sh.get("runner_badge", "")
        row[f"show_{n}_odds"]  = sh.get("odds", "")

    return row


def _build_col_list(max_runners):
    base = ["race_id", "sport", "race_name", "race_date", "race_time",
            "winner", "winner_badge", "date_label"]
    runner_cols = [f"pos{i}_badge" for i in range(1, max_runners + 1)]
    runner_cols += [f"pos{i}_name" for i in range(1, max_runners + 1)]
    # interleave: pos1_badge, pos1_name, pos2_badge, pos2_name, ...
    runner_cols = [c for i in range(1, max_runners + 1)
                   for c in (f"pos{i}_badge", f"pos{i}_name")]
    market_cols = [
        "win_badge", "win_odds",
        "place_1_badge", "place_1_odds", "place_2_badge", "place_2_odds",
        "place_3_badge", "place_3_odds", "place_4_badge", "place_4_odds",
        "exacta_runners", "exacta_odds",
        "even_odd_result", "even_odd_odds",
        "over_under_result", "over_under_odds",
        "quinella_runners", "quinella_odds",
        "trifecta_runners", "trifecta_odds",
        "show_1_badge", "show_1_odds", "show_2_badge", "show_2_odds",
        "show_3_badge", "show_3_odds", "show_4_badge", "show_4_odds",
    ]
    return base + runner_cols + market_cols


def save_all_to_excel(all_data, ts):
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    except ImportError:
        print("  ⚠ openpyxl not installed — saving CSV instead")
        filename = f"sportybet_racing_all_{ts}.csv"
        max_r = max((len(r.get("runners", [])) for races in all_data.values() for r in races), default=4)
        all_cols = _build_col_list(max_r)
        rows = [_flatten_race(r, max_r) for races in all_data.values() for r in races]
        if rows:
            with open(filename, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=all_cols, extrasaction="ignore")
                w.writeheader(); w.writerows(rows)
        print(f"  💾 Saved CSV: {filename}")
        return filename

    filename = f"sportybet_racing_all_{ts}.xlsx"
    wb = openpyxl.Workbook()
    if "Sheet" in wb.sheetnames: del wb["Sheet"]

    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill("solid", fgColor="7B3F00")   # brown — racing theme
    runner_fill = PatternFill("solid", fgColor="FFF8EE")   # warm cream
    market_fill = PatternFill("solid", fgColor="EEF4FF")   # cool blue
    thin = Border(
        left=Side(style="thin"), right=Side(style="thin"),
        top=Side(style="thin"), bottom=Side(style="thin"),
    )

    max_runners = max(
        (len(r.get("runners", [])) for races in all_data.values() for r in races),
        default=4
    )
    max_runners = max(max_runners, 4)
    all_cols  = _build_col_list(max_runners)
    base_end  = 8
    runner_end = base_end + max_runners * 2

    for sport, races in all_data.items():
        if not races: continue
        ws = wb.create_sheet(title=sport[:31])

        for ci, col in enumerate(all_cols, 1):
            cell = ws.cell(row=1, column=ci, value=col.replace("_", " ").title())
            cell.font      = header_font
            cell.fill      = header_fill
            cell.alignment = Alignment(horizontal="center")
            cell.border    = thin

        for ri, race in enumerate(races, 2):
            row_data = _flatten_race(race, max_runners)
            for ci, col in enumerate(all_cols, 1):
                cell = ws.cell(row=ri, column=ci, value=row_data.get(col, ""))
                cell.border = thin
                if ri % 2 == 0 and ci > base_end:
                    cell.fill = runner_fill if ci <= runner_end else market_fill

        sample = [_flatten_race(races[i], max_runners) for i in range(min(50, len(races)))]
        for ci, col in enumerate(all_cols, 1):
            vals = [str(sr.get(col, ""))[:40] for sr in sample]
            w = max(len(col), *(len(v) for v in vals)) + 3
            ws.column_dimensions[openpyxl.utils.get_column_letter(ci)].width = min(w, 38)

    # Summary sheet
    ws_sum = wb.create_sheet(title="Summary", index=0)
    for ci, hdr in enumerate(["Sport", "Total Races", "Scraped At"], 1):
        cell = ws_sum.cell(row=1, column=ci, value=hdr)
        cell.font = header_font
        cell.fill = header_fill
    for ri, (sport, races) in enumerate(all_data.items(), 2):
        ws_sum.cell(row=ri, column=1, value=sport)
        ws_sum.cell(row=ri, column=2, value=len(races))
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

    stealth_mode = "advanced"
    use_cooldown = True

    # ── 1. Headless ──────────────────────────────────────────
    print("\n  1. Run headless? [y/N]")
    c = input_with_timeout("  > ", 86400)
    headless = c.lower() == "y"

    # ── 2. Proxy ─────────────────────────────────────────────
    print(f"\n  2. Connection: [1] Direct  [2] Proxy ({PROXY_ADDR})")
    c = input_with_timeout("  > ", 86400)
    proxy_server = PROXY_ADDR if c == "2" else None
    print(f"    → {'Proxy' if proxy_server else 'Direct'}")

    # ── 3. Sports to scrape ──────────────────────────────────
    print(f"\n  3. Sports: [A] All {len(RACING_SPORTS)}  [S] Select specific")
    c = input_with_timeout("  > ", 86400)
    if c.lower() == "s":
        print("    " + "  ".join(f"[{i+1}] {s}" for i, s in enumerate(RACING_SPORTS)))
        sel = input_with_timeout("    Enter numbers e.g. 1,3: ", 86400)
        selected = []
        for n in sel.split(","):
            n = n.strip()
            if n.isdigit() and 1 <= int(n) <= len(RACING_SPORTS):
                selected.append(RACING_SPORTS[int(n) - 1])
        if not selected: selected = RACING_SPORTS[:]
    else:
        selected = RACING_SPORTS[:]
    print(f"    → Scraping: {', '.join(selected)}")

    # ── 4. Days of history ───────────────────────────────────
    print(f"\n  4. Days of results history per sport?")
    print(f"     [1] 1 day  [2] 3 days  [3] 7 days  [4] 14 days  [5] 30 days")
    while True:
        c = input_with_timeout("  > ", 86400)
        if c in ("1", "2", "3", "4", "5"): break
    target_days = {"1": 1, "2": 3, "3": 7, "4": 14, "5": 30}[c]

    # ── Ready ─────────────────────────────────────────────────
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    print(f"\n  📋 {len(selected)} sport(s) | {target_days} day(s) each")
    print(f"  📁 Output: sportybet_racing_*_{ts}")
    print(f"  ℹ  Ctrl+C = pause/resume  |  Ctrl+C twice = quit\n")

    pw = None
    all_sport_data = {}
    failed = []

    try:
        pw, browser, ctx, page = create_stealth_browser(
            headless=headless, proxy_server=proxy_server, stealth_mode=stealth_mode
        )

        print(f"\n  🌐 Opening {SITE_URL}")
        page.goto(SITE_URL, timeout=60000, wait_until="networkidle")
        _smart_sleep(8)

        frame = wait_and_find_virtual_frame(page, timeout=45000)

        for si, sport in enumerate(selected):
            _check_pause()

            print(f"\n{'─' * 60}")
            print(f"  SPORT {si + 1}/{len(selected)}: {sport}")
            print(f"{'─' * 60}")

            try:
                races = scrape_sport_results(frame, sport, target_days=target_days)
                all_sport_data[sport] = races
                if races:
                    save_to_json(races, sport, ts)
                else:
                    print(f"  ⚠ No races found for {sport}")
                    failed.append(f"{sport} | 0 races found")
            except Exception as e:
                print(f"  ❌ Error scraping {sport}: {e}")
                traceback.print_exc()
                all_sport_data[sport] = []
                failed.append(f"{sport} | ERROR: {str(e)[:80]}")

            if use_cooldown and si < len(selected) - 1:
                print(f"\n  ⏳ Cooldown {COOLDOWN_SECS}s...")
                _smart_sleep(COOLDOWN_SECS)

        if any(v for v in all_sport_data.values()):
            save_all_to_excel(all_sport_data, ts)

    except Exception as e:
        print(f"\n  ❌ FATAL: {e}")
        traceback.print_exc()
        failed.append(f"FATAL | {str(e)[:100]}")

    finally:
        try:
            if pw:
                browser.close()
                pw.stop()
        except:
            pass

    # ── Final summary ─────────────────────────────────────────
    print(f"\n\n{'═' * 65}")
    print(f"  SCRAPE SUMMARY")
    print(f"{'═' * 65}")
    total = sum(len(v) for v in all_sport_data.values())
    for sport, races in all_sport_data.items():
        icon = "✅" if races else "❌"
        print(f"  {icon} {sport}: {len(races)} races")
    print(f"\n  Total: {total} races across {len(all_sport_data)} sport(s)")
    if failed:
        print(f"\n  ❌ FAILURES:")
        for f in failed: print(f"     • {f}")
    else:
        print(f"\n  ✅ All sports scraped successfully!")
    print(f"{'═' * 65}\n")


if __name__ == "__main__":
    main()
