"""
╔══════════════════════════════════════════════════════════════════╗
║  SPORTYBET VIRTUAL FOOTBALL — Current Odds Scraper (Part 2)      ║
║  ─────────────────────────────────────────────────────────────── ║
║  Site:   https://www.sportybet.com/ng/virtual/                   ║
║  Target: Football League → Upcoming → Current Odds               ║
║  Leagues: England, Italy, France, Germany, Spain                 ║
║                                                                  ║
║  Purpose: Scrape current upcoming match odds for predictions     ║
║           and data analysis. Gets all market filters:            ║
║           Main (1X2), Correct Score, Over/Under, Others          ║
║                                                                  ║
║  Features:                                                       ║
║    ✓ Stealth browser (Basic / Advanced 16-layer)                 ║
║    ✓ Proxy (anyip.io) or system VPN                              ║
║    ✓ Headless toggle                                             ║
║    ✓ Iframe detection + frame switching                          ║
║    ✓ All market tabs: Main, Correct Score, Over/Under, Others    ║
║    ✓ Match result (1X2) + Double Chance + GG/NG tabs             ║
║    ✓ Over/Under 1.5, 2.5, 3.5, 4.5 grids                       ║
║    ✓ Output: JSON + Excel (.xlsx)                                ║
║    ✓ 6 mandatory init options                                    ║
║    ✓ Ctrl+C pause/resume                                         ║
║    ✓ Final summary with failures                                 ║
║                                                                  ║
║  Run:   python sportybet_virtual_odds_v1.py                      ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os, json, csv, random, signal, time, traceback, re
import threading, queue
from datetime import datetime
from playwright.sync_api import sync_playwright

# ═══════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════

BOT_NAME       = "SPORTYBET VIRTUAL ODDS — Part 2: Predictions Data"
SITE_URL       = "https://www.sportybet.com/ng/virtual/"
PROXY_ADDR     = "http://portal.anyip.io:2000"
COOLDOWN_SECS  = 15

LEAGUES        = ["England", "Italy", "France", "Germany", "Spain"]

# Market filter tabs to scrape (from SS13-16)
MARKET_TABS    = ["MAIN", "Correct Score", "Over/Under", "Others"]
# Sub-tabs under MAIN
MAIN_SUBTABS   = ["Match result", "1X2 + Handicap + Over/Under",
                  "1X2 + Double Chance + GG/NG", "Double Chance", "Asian Handicap"]

OUTPUT_JSON    = "sportybet_odds_{league}_{ts}.json"
OUTPUT_XLSX    = "sportybet_odds_all_{ts}.xlsx"


# ═══════════════════════════════════════════════════════════
#  BACKGROUND INPUT THREAD (proven)
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
#  CTRL+C PAUSE/RESUME (proven)
# ═══════════════════════════════════════════════════════════
_pause = False
def _ph(sig, frame):
    global _pause
    if _pause: return
    _pause = True
    print("\n\n  ⏸  PAUSED — ENTER to resume, 'q' to quit.")
    c = input_with_timeout("  > ", 86400)
    if c.lower() == "q":
        print("  ✋ Quitting."); os._exit(0)
    _pause = False
    print("  ▶  Resumed.\n")
signal.signal(signal.SIGINT, _ph)


# ═══════════════════════════════════════════════════════════
#  STEALTH BROWSER (same as Part 1)
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
    chrome_ver = chrome_ver.group(1) if chrome_ver else "136"
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
        "channel": "chrome",          # use real installed Chrome, not Playwright Chromium
        "ignore_default_args": ["--enable-automation"],
    }
    if proxy_server:
        launch_kwargs["proxy"] = {"server": proxy_server}
    browser = pw.chromium.launch(**launch_kwargs)
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
        print(f"  🌐 IP: {page.text_content('body') or '?'}")
    except:
        print("  ⚠ IP check failed")
    label = "🛡️ Advanced (16-layer)" if stealth_mode == "advanced" else "🔒 Basic"
    print(f"  {label} stealth active")
    return pw, browser, ctx, page


# ═══════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════

def human_pause(min_ms=300, max_ms=800):
    time.sleep(random.randint(min_ms, max_ms) / 1000)


MAIN_PAGE_URL = "https://www.sportybet.com/ng/virtual/"

def wait_and_find_virtual_frame(page, timeout=45000):
    """
    Wait for the virtual games iframe and return it.
    Accepts ANY non-main-page frame — no domain whitelist needed.
    """
    start = time.time()
    last_log = 0

    while (time.time() - start) * 1000 < timeout:
        if time.time() - last_log > 3:
            all_urls = [f.url for f in page.frames if f.url]
            print(f"  ℹ All frames ({len(all_urls)}): " + " | ".join(u[:70] for u in all_urls))
            last_log = time.time()

        for f in page.frames:
            url = f.url or ""
            if (url and url != "about:blank"
                    and url != MAIN_PAGE_URL
                    and "sportybet.com/ng/virtual" not in url
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

        page.wait_for_timeout(2000)

    for f in page.frames:
        url = f.url or ""
        if url and url != "about:blank" and url != MAIN_PAGE_URL:
            print(f"  ⚠ Fallback: using frame {url[:100]}")
            return f

    print("  ⚠ No virtual iframe found — operating on main page")
    return page


def dump_frame_debug(frame, label="debug"):
    """Dump frame clickable elements + body text to a file for selector diagnosis."""
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
            f.write(f"URL: {data.get('url','')}\nTitle: {data.get('title','')}\n\n")
            f.write("=== BODY TEXT ===\n" + data.get("body_text", "") + "\n\n")
            f.write("=== CLICKABLE ELEMENTS ===\n")
            for el in data.get("clickable", []):
                vis = "VISIBLE" if el["visible"] else "hidden"
                f.write(f"  [{vis}] <{el['tag']}> cls={el['cls']} | {el['text']}\n")
        print(f"  📄 Debug dump saved: {filename}")
    except Exception as e:
        print(f"  ⚠ Debug dump failed: {e}")


def _try_click(frame, selectors, label, timeout=6000):
    """Try selectors in order; return True on first successful click."""
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


def navigate_to_upcoming(frame, league_name):
    """
    Navigate to Upcoming matches for a given league.
    Tries multiple selector strategies and dumps debug info on failure.
    """
    print(f"\n  📂 Navigating to Upcoming → {league_name}")

    fl_selectors = [
        "text=Football League",
        "text=football league",
        "[class*='football']",
        "[class*='soccer']",
        "li:has-text('Football')",
        "a:has-text('Football')",
        "[class*='sport']:has-text('Football')",
        "[class*='menu-item']:has-text('Football')",
        "[class*='category']:has-text('Football')",
        "text=Football",
    ]
    fl_clicked = _try_click(frame, fl_selectors, "Football League", timeout=10000)
    if not fl_clicked:
        print(f"    ⚠ Could not click 'Football League' — dumping frame for diagnosis")
        dump_frame_debug(frame, "football_league_fail")
    else:
        human_pause(800, 1500)

    up_selectors = [
        "text=Upcoming",
        "text=upcoming",
        "a:has-text('Upcoming')",
        "li:has-text('Upcoming')",
        "[class*='tab']:has-text('Upcoming')",
        "[class*='menu-item']:has-text('Upcoming')",
        "text=Schedule",
        "text=Next",
    ]
    up_clicked = _try_click(frame, up_selectors, "Upcoming", timeout=8000)
    if not up_clicked:
        print(f"    ⚠ Could not click 'Upcoming' — dumping frame")
        dump_frame_debug(frame, "upcoming_fail")
    else:
        human_pause(800, 1500)

    league_selectors = [
        f"text={league_name}",
        f"a:has-text('{league_name}')",
        f"li:has-text('{league_name}')",
        f"[class*='tab']:has-text('{league_name}')",
        f"[class*='league']:has-text('{league_name}')",
        f"button:has-text('{league_name}')",
        f"[class*='item']:has-text('{league_name}')",
        f"[class*='menu']:has-text('{league_name}')",
    ]
    league_clicked = _try_click(frame, league_selectors, league_name, timeout=8000)
    if not league_clicked:
        print(f"    ⚠ Could not select league '{league_name}' — dumping frame")
        dump_frame_debug(frame, f"league_{league_name}_fail")
        return False
    human_pause(1000, 2000)
    return True


def scrape_match_result_odds(frame):
    """
    Scrape the Match Result (1X2) tab — HOME / DRAW / AWAY odds.
    Returns list of: {match_num, home, away, home_odds, draw_odds, away_odds}
    From SS13: table with columns HOME, DRAW, AWAY
    """
    matches = []
    try:
        # Click "Match result" sub-tab if available
        try:
            mr_tab = frame.locator("text=Match result").first
            if mr_tab.is_visible(timeout=3000):
                mr_tab.click()
                human_pause(500, 1000)
        except:
            pass

        # Parse the match rows
        data = frame.evaluate(r"""() => {
            const rows = [];
            // Look for the odds table rows
            // Each row: number, HOME_TEAM - AWAY_TEAM, stats_icon, HOME_ODD, DRAW_ODD, AWAY_ODD
            const bodyText = document.body.innerText;
            return bodyText;
        }""")

        if data:
            lines = data.split('\n')
            week_info = ""
            i = 0
            while i < len(lines):
                line = lines[i].strip()

                # Detect week header: "Week 27 - England" or "Football League: Spain  Week 29"
                wm = re.search(r'Week\s*(\d+)\s*[-–]\s*(\w+)', line)
                if wm:
                    week_info = f"Week {wm.group(1)} - {wm.group(2)}"

                # Detect match rows: "1.  EVE  -  AST  [icon]  1  X  2"
                # Then next line might have odds: "3.08  3.58  2.23"
                match_line = re.match(
                    r'(\d+)\.\s+(\w{2,4})\s*[-–]\s*(\w{2,4})',
                    line
                )
                if match_line:
                    match = {
                        "position": int(match_line.group(1)),
                        "home_team": match_line.group(2),
                        "away_team": match_line.group(3),
                        "week": week_info,
                    }
                    # Look ahead for odds (numbers like 3.08, 3.58, 2.23)
                    for j in range(i, min(i + 5, len(lines))):
                        odds_line = lines[j].strip()
                        odds_match = re.findall(r'(\d+\.\d{2})', odds_line)
                        if len(odds_match) >= 3:
                            match["home_odds"] = float(odds_match[0])
                            match["draw_odds"] = float(odds_match[1])
                            match["away_odds"] = float(odds_match[2])
                            break
                    matches.append(match)
                i += 1

    except Exception as e:
        print(f"    ⚠ Error scraping match result odds: {e}")

    return matches


def scrape_over_under_odds(frame):
    """
    Scrape the Over/Under tab — O/U 1.5, 2.5, 3.5, 4.5 for each match.
    From SS14/SS16: table with OV/UN columns for each threshold.
    """
    ou_data = []
    try:
        # Click "Over/Under" market filter tab
        try:
            ou_tab = frame.locator("text=Over/Under").first
            if ou_tab.is_visible(timeout=3000):
                ou_tab.click()
                human_pause(800, 1500)
        except:
            pass

        data = frame.evaluate("() => document.body.innerText")

        if data:
            lines = data.split('\n')
            i = 0
            while i < len(lines):
                line = lines[i].strip()

                # Detect match row: "1.  OSA  -  LEV  [icon]"
                match_line = re.match(r'(\d+)\.\s+(\w{2,4})\s*[-–]\s*(\w{2,4})', line)
                if match_line:
                    entry = {
                        "position": int(match_line.group(1)),
                        "home_team": match_line.group(2),
                        "away_team": match_line.group(3),
                    }
                    # Look ahead for O/U odds values
                    # Format from SS14: OV 1.5 | UN 1.5 | OV 2.5 | UN 2.5 | OV 3.5 | UN 3.5 | OV 4.5 | UN 4.5
                    for j in range(i, min(i + 8, len(lines))):
                        odds_text = lines[j].strip()
                        # Extract OV/UN pairs
                        ov_matches = re.findall(r'OV\s*([\d.]+)\s*([\d.]+)', odds_text)
                        un_matches = re.findall(r'UN\s*([\d.]+)\s*([\d.]+)', odds_text)
                        for ov in ov_matches:
                            entry[f"over_{ov[0]}"] = float(ov[1])
                        for un in un_matches:
                            entry[f"under_{un[0]}"] = float(un[1])

                    # Also try to parse a flat line of numbers
                    for j in range(i, min(i + 5, len(lines))):
                        nums = re.findall(r'(\d+\.\d{2})', lines[j].strip())
                        if len(nums) >= 8:
                            entry["over_1.5"] = float(nums[0])
                            entry["under_1.5"] = float(nums[1])
                            entry["over_2.5"] = float(nums[2])
                            entry["under_2.5"] = float(nums[3])
                            entry["over_3.5"] = float(nums[4])
                            entry["under_3.5"] = float(nums[5])
                            entry["over_4.5"] = float(nums[6])
                            entry["under_4.5"] = float(nums[7])
                            break

                    ou_data.append(entry)
                i += 1

    except Exception as e:
        print(f"    ⚠ Error scraping O/U odds: {e}")

    return ou_data


def scrape_correct_score_odds(frame):
    """Scrape Correct Score tab odds."""
    cs_data = []
    try:
        try:
            cs_tab = frame.locator("text=Correct Score").first
            if cs_tab.is_visible(timeout=3000):
                cs_tab.click()
                human_pause(800, 1500)
        except:
            pass

        data = frame.evaluate("() => document.body.innerText")
        if data:
            lines = data.split('\n')
            i = 0
            while i < len(lines):
                line = lines[i].strip()
                match_line = re.match(r'(\d+)\.\s+(\w{2,4})\s*[-–]\s*(\w{2,4})', line)
                if match_line:
                    entry = {
                        "position": int(match_line.group(1)),
                        "home_team": match_line.group(2),
                        "away_team": match_line.group(3),
                        "correct_score_odds": {},
                    }
                    # Look for score:odds pairs like "1-0: 5.50", "0-1: 6.20", etc.
                    for j in range(i, min(i + 20, len(lines))):
                        score_odds = re.findall(r'(\d+-\d+)\s*[:\s]\s*([\d.]+)', lines[j].strip())
                        for sc, odd in score_odds:
                            entry["correct_score_odds"][sc] = float(odd)
                    if entry["correct_score_odds"]:
                        cs_data.append(entry)
                i += 1

    except Exception as e:
        print(f"    ⚠ Error scraping correct score odds: {e}")

    return cs_data


def scrape_league_odds(frame, league_name):
    """
    Full odds scraping pipeline for one league:
    1. Navigate to Upcoming for the league
    2. Scrape Match Result (1X2) odds
    3. Scrape Over/Under odds
    4. Scrape Correct Score odds
    5. Merge all into unified match objects
    """
    if not navigate_to_upcoming(frame, league_name):
        print(f"  ❌ Failed to navigate to {league_name}")
        return []

    human_pause(1500, 2500)

    # Get week info from header
    week_info = ""
    try:
        header_text = frame.evaluate("() => document.body.innerText.substring(0, 500)")
        wm = re.search(r'Week\s*(\d+)\s*[-–]\s*' + league_name, header_text)
        if wm:
            week_info = f"Week {wm.group(1)}"
    except:
        pass

    print(f"  📊 Scraping odds for {league_name} ({week_info})...")

    # 1. Match Result (1X2) — Main tab
    print("    → Match Result (1X2)...")
    match_results = scrape_match_result_odds(frame)
    print(f"      Found {len(match_results)} matches with 1X2 odds")

    # 2. Over/Under
    print("    → Over/Under...")
    ou_odds = scrape_over_under_odds(frame)
    print(f"      Found {len(ou_odds)} matches with O/U odds")

    # 3. Correct Score
    print("    → Correct Score...")
    cs_odds = scrape_correct_score_odds(frame)
    print(f"      Found {len(cs_odds)} matches with CS odds")

    # Merge data by position/teams
    merged = []
    for mr in match_results:
        entry = {
            "league": league_name,
            "week": week_info or mr.get("week", ""),
            "position": mr.get("position"),
            "home_team": mr.get("home_team"),
            "away_team": mr.get("away_team"),
            "home_odds": mr.get("home_odds"),
            "draw_odds": mr.get("draw_odds"),
            "away_odds": mr.get("away_odds"),
            "scraped_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        # Merge O/U odds for same position
        for ou in ou_odds:
            if ou.get("position") == mr.get("position"):
                for k, v in ou.items():
                    if k.startswith("over_") or k.startswith("under_"):
                        entry[k] = v
                break

        # Merge CS odds for same position
        for cs in cs_odds:
            if cs.get("position") == mr.get("position"):
                entry["correct_score_odds"] = cs.get("correct_score_odds", {})
                break

        merged.append(entry)

    # If no match_results but we have O/U, use those as base
    if not merged and ou_odds:
        for ou in ou_odds:
            entry = {
                "league": league_name, "week": week_info,
                "position": ou.get("position"),
                "home_team": ou.get("home_team"),
                "away_team": ou.get("away_team"),
                "scraped_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            for k, v in ou.items():
                if k.startswith("over_") or k.startswith("under_"):
                    entry[k] = v
            merged.append(entry)

    print(f"  ✅ {league_name}: {len(merged)} matches with odds scraped")
    return merged


def save_to_json(data, league_name, ts):
    filename = OUTPUT_JSON.format(league=league_name.lower(), ts=ts)
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  💾 Saved JSON: {filename} ({len(data)} matches)")
    return filename


def save_all_to_excel(all_data, ts):
    """Save all league odds to Excel with one sheet per league."""
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    except ImportError:
        print("  ⚠ openpyxl not installed — saving as CSV")
        filename = f"sportybet_odds_all_{ts}.csv"
        flat = []
        for league, matches in all_data.items():
            for m in matches:
                # Flatten correct_score_odds dict
                cs = m.pop("correct_score_odds", {})
                for k, v in cs.items():
                    m[f"cs_{k}"] = v
                flat.append(m)
        if flat:
            keys = sorted(set(k for row in flat for k in row.keys()))
            with open(filename, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=keys)
                w.writeheader()
                w.writerows(flat)
        print(f"  💾 Saved CSV: {filename}")
        return filename

    filename = OUTPUT_XLSX.format(ts=ts)
    wb = openpyxl.Workbook()
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill("solid", fgColor="1B4F72")
    odds_fill = PatternFill("solid", fgColor="D5F5E3")
    thin_border = Border(
        left=Side(style="thin"), right=Side(style="thin"),
        top=Side(style="thin"), bottom=Side(style="thin"),
    )

    # Core columns
    core_cols = ["position", "league", "week", "home_team", "away_team",
                 "home_odds", "draw_odds", "away_odds",
                 "over_1.5", "under_1.5", "over_2.5", "under_2.5",
                 "over_3.5", "under_3.5", "over_4.5", "under_4.5",
                 "scraped_at"]

    for league, matches in all_data.items():
        if not matches:
            continue
        ws = wb.create_sheet(title=league[:31])

        # Determine extra columns
        extra = set()
        for m in matches:
            for k in m.keys():
                if k not in core_cols and k != "correct_score_odds":
                    extra.add(k)
        cols = core_cols + sorted(extra)

        # Header
        for ci, col in enumerate(cols, 1):
            cell = ws.cell(row=1, column=ci, value=col.replace("_", " ").title())
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center")
            cell.border = thin_border

        # Data
        for ri, match in enumerate(matches, 2):
            for ci, col in enumerate(cols, 1):
                val = match.get(col, "")
                if isinstance(val, dict):
                    val = json.dumps(val)
                cell = ws.cell(row=ri, column=ci, value=val)
                cell.border = thin_border
                # Highlight odds columns
                if col in ("home_odds", "draw_odds", "away_odds"):
                    cell.fill = odds_fill
                    if isinstance(val, (int, float)):
                        cell.number_format = "0.00"

        # Auto-width
        for ci, col in enumerate(cols, 1):
            ws.column_dimensions[openpyxl.utils.get_column_letter(ci)].width = min(
                max(len(col) + 4, 12), 30
            )

    # Correct Score sheet (separate because it's wide)
    cs_rows = []
    for league, matches in all_data.items():
        for m in matches:
            cs = m.get("correct_score_odds", {})
            if cs:
                row = {"league": league, "home": m.get("home_team"), "away": m.get("away_team")}
                row.update(cs)
                cs_rows.append(row)

    if cs_rows:
        ws_cs = wb.create_sheet(title="Correct Scores")
        cs_cols = ["league", "home", "away"] + sorted(set(k for r in cs_rows for k in r if k not in ("league", "home", "away")))
        for ci, col in enumerate(cs_cols, 1):
            cell = ws_cs.cell(row=1, column=ci, value=col)
            cell.font = header_font
            cell.fill = header_fill
        for ri, row in enumerate(cs_rows, 2):
            for ci, col in enumerate(cs_cols, 1):
                ws_cs.cell(row=ri, column=ci, value=row.get(col, ""))

    # Summary sheet
    ws_sum = wb.create_sheet(title="Summary", index=0)
    ws_sum.cell(row=1, column=1, value="League").font = header_font
    ws_sum.cell(row=1, column=1).fill = header_fill
    ws_sum.cell(row=1, column=2, value="Matches").font = header_font
    ws_sum.cell(row=1, column=2).fill = header_fill
    ws_sum.cell(row=1, column=3, value="Scraped At").font = header_font
    ws_sum.cell(row=1, column=3).fill = header_fill
    for ri, (league, matches) in enumerate(all_data.items(), 2):
        ws_sum.cell(row=ri, column=1, value=league)
        ws_sum.cell(row=ri, column=2, value=len(matches))
        ws_sum.cell(row=ri, column=3, value=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

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

    # ── OPTION 1: Connection mode ──
    print("\n  1. Connection: [1] Proxy (anyip.io)  [2] System VPN / direct")
    while True:
        c = input_with_timeout("  > ", 86400)
        if c in ("1", "2"): break
    use_vpn = (c == "2")

    # ── OPTION 2: Headless ──
    print("\n  2. Run headless? [y/N]")
    c = input_with_timeout("  > ", 86400)
    headless = c.lower() == "y"

    # ── OPTION 3: Leagues to scrape ──
    print(f"\n  3. Leagues: [A] All 5  [S] Select specific")
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

    # ── OPTION 4: Cooldown between leagues ──
    print("\n  4. 15s cooldown between leagues? [Y/n]")
    c = input_with_timeout("  > ", 86400)
    use_cooldown = c.lower() != "n"

    # ── OPTION 5: Stealth mode ──
    print("\n  5. Stealth: [1] Advanced (16-layer)  [2] Basic (webdriver only)")
    while True:
        c = input_with_timeout("  > ", 86400)
        if c in ("1", "2"): break
    stealth_mode = "advanced" if c == "1" else "basic"

    # ── OPTION 6: Wait for next week? ──
    print("\n  6. If current week is live/in-progress, wait for next? [y/N]")
    c = input_with_timeout("  > ", 86400)
    wait_for_next = c.lower() == "y"

    # ── Ready ──
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    print(f"\n  📋 Scraping odds for {len(selected_leagues)} leagues")
    print(f"  📁 Output prefix: sportybet_odds_*_{ts}")
    print()

    pw = None
    all_league_data = {}
    failed_details = []

    try:
        proxy = PROXY_ADDR if not use_vpn else None
        pw, browser, ctx, page = create_stealth_browser(
            headless=headless, proxy_server=proxy, stealth_mode=stealth_mode
        )

        print(f"\n  🌐 Opening {SITE_URL}")
        page.goto(SITE_URL, timeout=60000, wait_until="networkidle")
        page.wait_for_timeout(8000)

        frame = wait_and_find_virtual_frame(page, timeout=45000)

        for li, league in enumerate(selected_leagues):
            while _pause: time.sleep(0.5)

            print(f"\n{'─' * 60}")
            print(f"  LEAGUE {li + 1}/{len(selected_leagues)}: {league}")
            print(f"{'─' * 60}")

            try:
                odds = scrape_league_odds(frame, league)
                all_league_data[league] = odds

                if odds:
                    save_to_json(odds, league, ts)
                else:
                    print(f"  ⚠ No odds found for {league}")
                    failed_details.append(f"{league} | 0 matches with odds")

            except Exception as e:
                print(f"  ❌ Error scraping {league}: {e}")
                traceback.print_exc()
                all_league_data[league] = []
                failed_details.append(f"{league} | ERROR: {str(e)[:80]}")

            if use_cooldown and li < len(selected_leagues) - 1:
                print(f"\n  ⏳ Cooldown {COOLDOWN_SECS}s...")
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
    print(f"  ODDS SCRAPE SUMMARY")
    print(f"{'═' * 65}")
    total = sum(len(v) for v in all_league_data.values())
    for league, matches in all_league_data.items():
        icon = "✅" if matches else "❌"
        has_1x2 = sum(1 for m in matches if m.get("home_odds"))
        has_ou = sum(1 for m in matches if m.get("over_2.5"))
        print(f"  {icon} {league}: {len(matches)} matches | 1X2: {has_1x2} | O/U: {has_ou}")
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
