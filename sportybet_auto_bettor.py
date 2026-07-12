"""
Sportybet Virtual Auto-Bettor — martingale on the app's verified SINGLE picks.

Runs BESIDE the predictor app (which must be running at 127.0.0.1:5000).
Opens a VISIBLE browser with a persistent profile: you log in manually once;
the bot never sees or stores your password.

Safety model (in priority order):
  1. NEVER place a bet it cannot fully verify on the betslip
     (match + market + selection + odds all confirmed, exactly 1 selection).
  2. NEVER place the next martingale bet until the previous one is SETTLED
     and verified in My Bets (unknown result => stop, never guess).
  3. Anti double-submit: a persisted 'placing' marker is written BEFORE the
     Place Bet click; on any restart the bot reconciles with My Bets before
     it will ever bet again.
  4. Hard rails: max stake cap, daily loss limit, daily profit target —
     any breach exits the program (restarting it is your explicit consent
     to continue).
  5. Ships with dry_run=true: full cycle, verified slip, simulated placement.

Betting windows: bets START only inside a window, but a losing streak is
chased past the window end until the first WIN settles (still subject to
the loss limit), exactly as configured.
"""

import json
import os
import random
import re
import sys
import time
import urllib.request
from datetime import datetime, date

if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    import io
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace", line_buffering=True)
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "bettor_config.json")
STATE_DIR = os.path.join(BASE_DIR, "data", "bettor")
STATE_PATH = os.path.join(STATE_DIR, "state.json")
LEDGER_PATH = os.path.join(STATE_DIR, "bets.jsonl")
PROFILE_DIR = os.path.join(BASE_DIR, "bettor_profile")
APP_API = "http://127.0.0.1:5000"

DEFAULT_CONFIG = {
    "dry_run": True,
    "base_stake": 10,
    "max_stake": 500,
    "daily_loss_limit": 1000,
    "daily_profit_target": 500,
    "betting_windows": [["07:00", "10:00"], ["22:00", "23:59"]],
    "odds_tolerance": 0.15,
    "min_odds": 1.35,
    "max_odds": 2.10,
}


def log(msg):
    print(f"  [{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_config():
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        log(f"Created {os.path.basename(CONFIG_PATH)} with safe defaults (dry_run=true).")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = {**DEFAULT_CONFIG, **json.load(f)}
    return cfg


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2)
    os.replace(tmp, STATE_PATH)


def append_ledger(rec):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(LEDGER_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def api_get(path, timeout=10):
    try:
        with urllib.request.urlopen(APP_API + path, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
#  Day ledger / rails
# ─────────────────────────────────────────────────────────────

def today_key():
    return date.today().isoformat()


def day_profit(st):
    d = st.get("days", {}).get(today_key(), {})
    return d.get("profit", 0.0)


def record_outcome(st, stake, odds, won):
    d = st.setdefault("days", {}).setdefault(today_key(), {"profit": 0.0, "bets": 0})
    d["bets"] += 1
    d["profit"] += (stake * (odds - 1)) if won else (-stake)
    save_state(st)


def in_window(cfg, now=None):
    now = now or datetime.now()
    cur = now.strftime("%H:%M")
    for start, end in cfg["betting_windows"]:
        if start <= cur <= end:
            return True
    return False


def next_stake(cfg, cum_loss, odds):
    """Martingale: recover all losses + one base-stake profit at these odds."""
    base = cfg["base_stake"]
    if cum_loss <= 0:
        return base
    import math
    need = (cum_loss + base) / max(odds - 1.0, 0.01)
    return max(base, int(math.ceil(need)))


# ─────────────────────────────────────────────────────────────
#  Browser
# ─────────────────────────────────────────────────────────────

def launch_browser():
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    ctx = None
    for channel in ("chrome", "msedge", None):
        try:
            kw = dict(headless=False, viewport={"width": 1420, "height": 900},
                      args=["--disable-blink-features=AutomationControlled"])
            if channel:
                kw["channel"] = channel
            ctx = pw.chromium.launch_persistent_context(PROFILE_DIR, **kw)
            break
        except Exception:
            continue
    if ctx is None:
        pw.stop()
        raise RuntimeError("Could not launch any browser")
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return pw, ctx, page


def find_virtual_frame(page, timeout_s=60):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for fr in page.frames:
            if "virtustec" in (fr.url or ""):
                try:
                    txt = fr.evaluate("() => (document.body.innerText || '').slice(0, 300)")
                    if "Login error" in txt or "CODE: CORE" in txt:
                        return None
                    if txt.strip():
                        return fr
                except Exception:
                    pass
        time.sleep(2)
    return None


def goto_virtuals(page):
    for attempt in range(4):
        try:
            page.goto("https://www.sportybet.com/ng/virtual/", timeout=60000,
                      wait_until="domcontentloaded")
        except Exception:
            pass
        time.sleep(8)
        fr = find_virtual_frame(page)
        if fr:
            return fr
        log("Virtual games frame not ready (or session rejected) — reloading...")
        time.sleep(6)
    return None


def wait_for_login(page):
    """User logs in manually; we only proceed once the balance is visible."""
    warned = False
    while True:
        try:
            txt = page.evaluate("() => (document.body.innerText || '').slice(0, 3000)")
        except Exception:
            txt = ""
        if re.search(r"NGN\s*[\d,]+\.?\d*", txt) and "Log In" not in txt:
            m = re.search(r"NGN\s*([\d,]+\.?\d*)", txt)
            bal = m.group(1) if m else "?"
            log(f"Logged in. Balance: NGN {bal}")
            return
        if not warned:
            log(">>> Please LOG IN in the browser window (the bot never sees your password). Waiting...")
            warned = True
        time.sleep(4)


def read_balance(page):
    try:
        txt = page.evaluate("() => (document.body.innerText || '').slice(0, 3000)")
        m = re.search(r"NGN\s*([\d,]+\.?\d*)", txt)
        if m:
            return float(m.group(1).replace(",", ""))
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────────
#  Slip building & verification
# ─────────────────────────────────────────────────────────────

def clear_betslip(frame):
    try:
        frame.evaluate("""() => {
            for (const el of document.querySelectorAll('a, button, span, div')) {
                const t = (el.textContent || '').trim();
                if (t === 'Clear' && el.offsetParent !== null) { el.click(); return true; }
            }
            return false;
        }""")
        time.sleep(0.8)
    except Exception:
        pass


def nav_to_league_upcoming(frame, league):
    """Sidebar: Football League -> Upcoming -> league tab."""
    try:
        frame.evaluate("""() => {
            const links = Array.from(document.querySelectorAll('a'));
            const fl = links.find(a => a.textContent.trim() === 'Football League');
            if (fl && fl.classList.contains('collapsed')) fl.click();
        }""")
        time.sleep(random.uniform(0.8, 1.6))
        frame.evaluate("""() => {
            const links = Array.from(document.querySelectorAll('a'));
            const fl = links.find(a => a.textContent.trim() === 'Football League');
            const li = fl && fl.closest('li');
            if (!li) return;
            for (const a of li.querySelectorAll('a')) {
                if (a.textContent.trim() === 'Upcoming' && a.offsetParent !== null) { a.click(); return; }
            }
        }""")
        time.sleep(random.uniform(1.2, 2.0))
        frame.evaluate("""(league) => {
            for (const el of document.querySelectorAll('div.item, div[class*="item"]')) {
                if (el.textContent.trim() === league && el.offsetParent !== null) { el.click(); return; }
            }
            for (const el of document.querySelectorAll('a, button, span, div')) {
                if (el.textContent.trim() === league && !el.closest('ul.nav')
                        && el.offsetParent !== null) { el.click(); return; }
            }
        }""", league)
        time.sleep(random.uniform(1.5, 2.5))
        return True
    except Exception as e:
        log(f"nav error: {e}")
        return False


def select_market_tab(frame, market):
    label = "Match result" if market == "1X2" else "Over/Under"
    try:
        r = frame.evaluate("""(label) => {
            for (const el of document.querySelectorAll('a, li, span, div, button')) {
                const t = (el.textContent || '').trim();
                if (t === label && el.offsetParent !== null && el.offsetWidth > 0) {
                    el.click(); return true;
                }
            }
            return false;
        }""", label)
        time.sleep(random.uniform(1.5, 2.5))
        return bool(r)
    except Exception:
        return False


def click_pick_odds(frame, pick):
    """Click the odds cell for this pick inside its match row and week
    section. Returns the odds value of the clicked cell, or None."""
    js = r"""(args) => {
        const { home, away, week, call } = args;
        const rows = Array.from(document.querySelectorAll('div, tr, li'));
        // find the smallest container mentioning both teams
        let target = null;
        for (const el of rows) {
            const t = (el.textContent || '');
            if (t.length < 400 && t.includes(home) && t.includes(away)
                    && el.querySelectorAll('*').length < 120) {
                if (!target || el.textContent.length < target.textContent.length) target = el;
            }
        }
        if (!target) return { err: 'row_not_found' };
        // collect clickable odds cells (decimal numbers) in DOM order
        const cells = [];
        for (const el of target.querySelectorAll('span, div, button, a')) {
            const t = (el.textContent || '').trim();
            if (/^\d+\.\d{2}$/.test(t) && el.offsetParent !== null) {
                let clickable = el;
                for (let up = el; up && up !== target; up = up.parentElement) {
                    const cls = (up.className || '') + '';
                    if (cls.includes('odd') || cls.includes('outcome') || up.onclick) { clickable = up; break; }
                }
                cells.push({ el: clickable, val: parseFloat(t) });
            }
        }
        if (!cells.length) return { err: 'no_cells' };
        // 1X2 view: cells [home, draw, away]; O/U 2.5 view: threshold columns
        // pairs (O1.5 U1.5 O2.5 U2.5 O3.5 U3.5 O4.5 U4.5)
        let idx = -1;
        if (call === '1') idx = 0;
        else if (call === '2') idx = 2;
        else if (call === 'O2.5') idx = cells.length >= 8 ? 2 : (cells.length >= 4 ? 2 : 0);
        else if (call === 'U2.5') idx = cells.length >= 8 ? 3 : (cells.length >= 4 ? 3 : 1);
        if (idx < 0 || idx >= cells.length) return { err: 'idx_out_of_range', n: cells.length };
        cells[idx].el.click();
        return { clicked: cells[idx].val, n: cells.length };
    }"""
    try:
        return frame.evaluate(js, {"home": pick["home"], "away": pick["away"],
                                   "week": pick["week"], "call": pick["call"]})
    except Exception as e:
        return {"err": str(e)[:100]}


def read_betslip(frame):
    try:
        return frame.evaluate(r"""() => {
            const body = document.body.innerText || '';
            const i = body.indexOf('Betslip');
            if (i < 0) return '';
            return body.slice(i, i + 1200);
        }""")
    except Exception:
        return ""


def verify_betslip(slip_text, pick, tolerance):
    """Every check must pass or we do NOT bet."""
    problems = []
    if not slip_text:
        return ["betslip not readable"]
    if slip_text.count(pick["home"]) < 1 or slip_text.count(pick["away"]) < 1:
        problems.append("teams not on slip")
    if f"Week {pick['week']}" not in slip_text:
        problems.append(f"week {pick['week']} not on slip")
    want_market = "Match result" if pick["market"] == "1X2" else "Over/Under"
    if want_market.lower() not in slip_text.lower():
        problems.append(f"market '{want_market}' not on slip")
    want_sel = {"1": "Home", "2": "Away",
                "O2.5": "Over 2.5", "U2.5": "Under 2.5"}[pick["call"]]
    if want_sel.lower() not in slip_text.lower():
        problems.append(f"selection '{want_sel}' not on slip")
    # exactly one selection
    n_sel = len(re.findall(r"#\d{6,}", slip_text))
    if n_sel != 1:
        problems.append(f"{n_sel} selections on slip (need exactly 1)")
    # odds close to the pick's odds
    m = re.findall(r"\b(\d\.\d{2})\b", slip_text)
    slip_odds = None
    for v in m:
        fv = float(v)
        if abs(fv - pick["odds"]) <= tolerance:
            slip_odds = fv
            break
    if slip_odds is None:
        problems.append(f"no odds within {tolerance} of {pick['odds']} on slip "
                        f"(saw {m[:4]})")
    return problems if problems else slip_odds


def enter_stake_and_place(frame, stake, slip_odds, dry_run):
    """Type the stake, sanity-check potential return, click Place Bet.
    Returns 'placed', 'dry_run', or an error string."""
    try:
        inputs = frame.locator("input")
        target = None
        for i in range(min(inputs.count(), 8)):
            el = inputs.nth(i)
            try:
                if el.is_visible():
                    ph = (el.get_attribute("placeholder") or "").lower()
                    tp = (el.get_attribute("type") or "").lower()
                    if "stake" in ph or "amount" in ph or tp in ("number", "tel", "text"):
                        target = el
                        break
            except Exception:
                continue
        if target is None:
            return "stake box not found"
        target.click()
        time.sleep(random.uniform(0.3, 0.8))
        target.fill("")
        target.type(str(int(stake)), delay=random.randint(60, 140))
        time.sleep(random.uniform(0.8, 1.5))

        body = frame.evaluate("() => document.body.innerText || ''")
        expect = stake * slip_odds
        pot_ok = False
        for v in re.findall(r"([\d,]+\.\d{2})", body):
            fv = float(v.replace(",", ""))
            if abs(fv - expect) / expect < 0.03:
                pot_ok = True
                break
        if not pot_ok:
            return f"potential return {expect:.0f} not shown — aborting"

        if dry_run:
            return "dry_run"

        r = frame.evaluate("""() => {
            for (const el of document.querySelectorAll('button, a, div, span')) {
                const t = (el.textContent || '').trim();
                if (t === 'Place Bet' && el.offsetParent !== null) { el.click(); return true; }
            }
            return false;
        }""")
        if not r:
            return "Place Bet button not found"
        time.sleep(2.5)
        # confirm dialog if present
        frame.evaluate("""() => {
            for (const el of document.querySelectorAll('button, a, div, span')) {
                const t = (el.textContent || '').trim();
                if ((t === 'Confirm' || t === 'OK') && el.offsetParent !== null) { el.click(); return; }
            }
        }""")
        time.sleep(2.0)
        return "placed"
    except Exception as e:
        return f"place error: {str(e)[:120]}"


# ─────────────────────────────────────────────────────────────
#  My Bets settlement
# ─────────────────────────────────────────────────────────────

def read_my_bets(frame, max_rows=10):
    """Open My Bets and parse recent tickets:
    [{id, stake, win, settled}] newest first."""
    try:
        frame.evaluate("""() => {
            for (const el of document.querySelectorAll('a, li, span, div, button')) {
                const t = (el.textContent || '').trim();
                if (t === 'My Bets' && el.offsetParent !== null) { el.click(); return; }
            }
        }""")
        time.sleep(2.0)
        txt = frame.evaluate("() => document.body.innerText || ''")
    except Exception:
        return []
    rows = []
    pat = re.compile(
        r"#(\d{6,})\s*\|?\s*(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2})?[\s\S]{0,80}?"
        r"₦([\d,]+)\s+₦([\d,]+|--|—)", re.M)
    for m in pat.finditer(txt):
        tid, _, stake_s, win_s = m.groups()
        settled = win_s not in ("--", "—")
        rows.append({
            "id": tid,
            "stake": float(stake_s.replace(",", "")),
            "win": float(win_s.replace(",", "")) if settled else None,
            "settled": settled,
        })
        if len(rows) >= max_rows:
            break
    return rows


def find_ticket(frame, ticket_id=None, stake=None, retries=3):
    for _ in range(retries):
        rows = read_my_bets(frame)
        for i, r in enumerate(rows):
            if ticket_id and r["id"] == ticket_id:
                return r
            # stake-based matching only trusts the newest few rows so an
            # old same-stake bet can't be mistaken for this one
            if ticket_id is None and stake is not None and i < 3 \
                    and abs(r["stake"] - stake) < 0.5:
                return r
        time.sleep(4)
    return None


# ─────────────────────────────────────────────────────────────
#  Main loop
# ─────────────────────────────────────────────────────────────

def stop_program(reason, st=None):
    log("=" * 56)
    log(f"STOPPED: {reason}")
    if st is not None:
        log(f"Today: profit NGN {day_profit(st):+,.0f} "
            f"over {st.get('days', {}).get(today_key(), {}).get('bets', 0)} bets")
    log("Restart the program when you want to continue.")
    log("=" * 56)
    sys.exit(0)


def rails_check(cfg, st):
    p = day_profit(st)
    if p >= cfg["daily_profit_target"]:
        stop_program(f"daily PROFIT TARGET reached (NGN {p:+,.0f})", st)
    if p <= -cfg["daily_loss_limit"]:
        stop_program(f"daily LOSS LIMIT hit (NGN {p:+,.0f})", st)


def reconcile_unfinished(frame, st, cfg):
    """If a previous run died mid-placement, resolve it via My Bets BEFORE
    ever betting again (anti double-submit)."""
    ph = st.get("placing")
    if not ph:
        return
    log(f"Found unfinished bet marker from {ph.get('ts')} — reconciling with My Bets...")
    row = find_ticket(frame, stake=ph.get("stake"), retries=4)
    if row and row["settled"]:
        won = (row["win"] or 0) > 0
        log(f"  -> that bet exists and settled: {'WON' if won else 'LOST'}")
        record_outcome(st, ph["stake"], ph.get("odds", 2.0), won)
        st["cum_loss"] = 0 if won else st.get("cum_loss", 0) + ph["stake"]
    elif row:
        log("  -> that bet exists and is still pending; waiting for settlement...")
        row2 = None
        for _ in range(60):
            time.sleep(8)
            row2 = find_ticket(frame, ticket_id=row["id"], retries=1)
            if row2 and row2["settled"]:
                break
        if not (row2 and row2["settled"]):
            stop_program("could not settle the unfinished bet — check My Bets manually", st)
        won = (row2["win"] or 0) > 0
        record_outcome(st, ph["stake"], ph.get("odds", 2.0), won)
        st["cum_loss"] = 0 if won else st.get("cum_loss", 0) + ph["stake"]
    else:
        log("  -> no matching ticket found: the bet was never placed. Safe to continue.")
    st.pop("placing", None)
    save_state(st)


def main():
    cfg = load_config()
    st = load_state()
    st.setdefault("cum_loss", 0)

    print("=" * 60)
    print("  SPORTYBET AUTO-BETTOR" + ("  [DRY RUN — no real bets]" if cfg["dry_run"] else "  [LIVE MONEY]"))
    print(f"  base stake NGN {cfg['base_stake']}  |  max stake NGN {cfg['max_stake']}")
    print(f"  daily: profit target +{cfg['daily_profit_target']}  loss limit -{cfg['daily_loss_limit']}")
    print(f"  windows: {cfg['betting_windows']}   (streaks are chased past window end until a WIN)")
    print("=" * 60)

    if api_get("/api/status") is None:
        stop_program("predictor app is not running at 127.0.0.1:5000 — start it first (python app.py)")

    rails_check(cfg, st)

    pw = ctx = page = None
    try:
        pw, ctx, page = launch_browser()
        page.goto("https://www.sportybet.com/ng/", timeout=60000, wait_until="domcontentloaded")
        if not cfg["dry_run"]:
            wait_for_login(page)
        frame = goto_virtuals(page)
        if frame is None:
            stop_program("could not open the virtual games (session rejected repeatedly)")

        if not cfg["dry_run"]:
            reconcile_unfinished(frame, st, cfg)

        chasing = st.get("cum_loss", 0) > 0  # resume mid-streak after restart
        while True:
            rails_check(cfg, st)

            # frame health — reconnect instead of dying on internet blips
            try:
                frame.evaluate("() => 1")
            except Exception:
                log("Browser frame lost — reconnecting...")
                frame = goto_virtuals(page)
                if frame is None:
                    time.sleep(30)
                    continue

            inside = in_window(cfg)
            if not inside and not chasing:
                time.sleep(20)
                continue
            if not inside and chasing:
                log("Past window end but on a losing streak — chasing until the next WIN.")

            # 1. Get the pick for the NEXT gameweek
            pick = api_get("/api/bet_pick")
            if not pick or not pick.get("home"):
                log("No qualifying pick right now — retrying shortly...")
                time.sleep(20)
                continue
            if not (cfg["min_odds"] <= pick["odds"] <= cfg["max_odds"]):
                log(f"Pick odds {pick['odds']} outside [{cfg['min_odds']}, {cfg['max_odds']}] — skipping this GW.")
                time.sleep(30)
                continue

            stake = next_stake(cfg, st.get("cum_loss", 0), pick["odds"])
            if stake > cfg["max_stake"]:
                stop_program(f"martingale needs NGN {stake} > max stake cap {cfg['max_stake']} "
                             f"(cum loss NGN {st.get('cum_loss', 0)}) — refusing to bet a capped "
                             f"stake that cannot recover the streak", st)

            log(f"PICK: {pick['league']} GW{pick['week']}  {pick['home']}—{pick['away']}  "
                f"[{pick['call']}] @{pick['odds']}  est {pick['est_win_pct']}%  ->  stake NGN {stake}"
                f"{'  (recovering NGN %d)' % st['cum_loss'] if st.get('cum_loss') else ''}")

            # humanized pause before building the slip
            time.sleep(random.uniform(4, 22))

            # 2. Build + verify the slip
            clear_betslip(frame)
            if not nav_to_league_upcoming(frame, pick["league"]):
                log("Navigation failed — skipping this GW.")
                time.sleep(30)
                continue
            select_market_tab(frame, pick["market"])
            res = click_pick_odds(frame, pick)
            if not res or res.get("err"):
                log(f"Could not click the pick's odds ({res}) — skipping this GW.")
                time.sleep(30)
                continue

            time.sleep(random.uniform(1.0, 2.0))
            slip = read_betslip(frame)
            v = verify_betslip(slip, pick, cfg["odds_tolerance"])
            if isinstance(v, list):
                log(f"SLIP REJECTED: {'; '.join(v)} — clearing, skipping this GW.")
                clear_betslip(frame)
                time.sleep(30)
                continue
            slip_odds = v
            log(f"Slip verified: 1 selection, odds {slip_odds}")

            # 3. Stake + place (anti double-submit marker FIRST)
            st["placing"] = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                             "stake": stake, "odds": slip_odds,
                             "pick": {k: pick[k] for k in
                                      ("league", "week", "home", "away", "call")}}
            save_state(st)
            outcome = enter_stake_and_place(frame, stake, slip_odds, cfg["dry_run"])

            if outcome == "dry_run":
                log(f"DRY RUN: would place NGN {stake} @ {slip_odds}")
                st.pop("placing", None)
                save_state(st)
                clear_betslip(frame)
                won = wait_result_via_app(pick)
                if won is None:
                    log("Result unknown after timeout — NOT counting this cycle.")
                    continue
            elif outcome == "placed":
                log(f"BET PLACED: NGN {stake} @ {slip_odds}")
                row = find_ticket(frame, stake=stake, retries=5)
                if row is None:
                    stop_program("placed a bet but cannot find it in My Bets — "
                                 "VERIFY MANUALLY before restarting", st)
                st["placing"]["ticket_id"] = row["id"]
                save_state(st)
                won = wait_settlement(frame, row["id"], pick)
                if won is None:
                    stop_program("bet did not settle in time — check My Bets manually "
                                 "before restarting (the bot will reconcile)", st)
                st.pop("placing", None)
                save_state(st)
            else:
                log(f"NOT placed: {outcome} — clearing slip, skipping this GW.")
                st.pop("placing", None)
                save_state(st)
                clear_betslip(frame)
                time.sleep(30)
                continue

            # 4. Book the outcome, update martingale
            record_outcome(st, stake, slip_odds, won)
            append_ledger({"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                           "dry_run": cfg["dry_run"], **{k: pick[k] for k in
                           ("league", "week", "home", "away", "call")},
                           "odds": slip_odds, "stake": stake, "won": won})
            if won:
                profit = stake * (slip_odds - 1)
                st["cum_loss"] = 0
                chasing = False
                log(f"WON +NGN {profit:,.0f}  |  today NGN {day_profit(st):+,.0f}")
                if not in_window(cfg):
                    log("Win landed after window end — stopping for this window as configured.")
                    save_state(st)
                    rails_check(cfg, st)
                    continue  # loop: won't start a new bet until next window
            else:
                st["cum_loss"] = st.get("cum_loss", 0) + stake
                chasing = True
                log(f"LOST -NGN {stake:,.0f}  |  streak loss NGN {st['cum_loss']:,.0f}  "
                    f"|  today NGN {day_profit(st):+,.0f}")
            save_state(st)
            rails_check(cfg, st)

            # small breather before the next cycle
            time.sleep(random.uniform(6, 18))

    except KeyboardInterrupt:
        log("Stopped by you (Ctrl+C).")
    finally:
        try:
            if ctx:
                ctx.close()
            if pw:
                pw.stop()
        except Exception:
            pass


def wait_result_via_app(pick, timeout_s=420):
    """Dry-run settlement via the predictor app's scraped results."""
    q = (f"/api/bet_result?league={pick['league']}&week={pick['week']}"
         f"&home={pick['home']}&away={pick['away']}&call={pick['call']}")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = api_get(q)
        if r and r.get("status") in ("won", "lost"):
            log(f"Result: {r['status'].upper()} ({r.get('score')})")
            return r["status"] == "won"
        time.sleep(12)
    return None


def wait_settlement(frame, ticket_id, pick, timeout_s=480):
    """LIVE settlement: My Bets is the source of truth; the app's result
    feed is used as a cross-check when both are available."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        row = find_ticket(frame, ticket_id=ticket_id, retries=1)
        if row and row["settled"]:
            won = (row["win"] or 0) > 0
            app_r = api_get(f"/api/bet_result?league={pick['league']}&week={pick['week']}"
                            f"&home={pick['home']}&away={pick['away']}&call={pick['call']}")
            if app_r and app_r.get("status") in ("won", "lost"):
                app_won = app_r["status"] == "won"
                if app_won != won:
                    log(f"WARNING: My Bets says {'WON' if won else 'LOST'} but scraped "
                        f"result says {'WON' if app_won else 'LOST'} — trusting My Bets.")
            log(f"Settled: {'WON' if won else 'LOST'} (ticket #{ticket_id})")
            return won
        time.sleep(random.uniform(7, 12))
    return None


if __name__ == "__main__":
    main()
