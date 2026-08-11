"""
Sportybet Virtual Auto-Bettor.

Bet types (chosen at startup or in bettor_config.json):
  single        - football SINGLE banker (best verified pick, any league)
  two           - football 2-odds banker ticket (2+ legs, any league)
  three         - football 3-odds ticket
  racing_place  - racing: predicted 2nd AND 3rd runners on the PLACE market
                  (two flat bets per race; martingale not applicable)

Staking: martingale ON -> losses are recovered next bet (with hard rails);
         martingale OFF -> flat base stake every bet.

Runs BESIDE the predictor app (python app.py must be running).
You log in MANUALLY in the bot's browser window — the bot never sees,
stores, or types your password. Login persists in bettor_profile/.

Safety model:
  1. Never place a bet it cannot fully verify on the betslip.
  2. Never bet the next martingale step until the previous bet is SETTLED
     (My Bets is the source of truth; unknown result => stop, never guess).
  3. Anti double-submit marker persisted BEFORE every Place Bet click,
     reconciled against My Bets on every startup.
  4. Hard rails: max stake cap, daily loss limit, daily profit target.
  5. dry_run=true by default: everything real except the final click.
"""

import itertools
import json
import math
import os
import random
import re
import sys
import time
import urllib.parse
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
# Set per-mode in main(): real money keeps its own pristine streak/ledger;
# sim and dry-run share a separate one so virtual losses never inflate the
# first real-money stake.
STATE_PATH = os.path.join(STATE_DIR, "state.json")
LEDGER_PATH = os.path.join(STATE_DIR, "bets.jsonl")


def set_state_paths(cfg):
    global STATE_PATH, LEDGER_PATH
    if cfg.get("sim_mode") or cfg.get("dry_run"):
        STATE_PATH = os.path.join(STATE_DIR, "state_practice.json")
        LEDGER_PATH = os.path.join(STATE_DIR, "bets_practice.jsonl")
    else:
        STATE_PATH = os.path.join(STATE_DIR, "state.json")
        LEDGER_PATH = os.path.join(STATE_DIR, "bets.jsonl")
PROFILE_DIR = os.path.join(BASE_DIR, "bettor_profile")
APP_API = "http://127.0.0.1:5000"

DEFAULT_CONFIG = {
    "dry_run": True,
    "bet_type": "single",
    "racing_market": "show",
    "leagues": "",
    "skip_odds": [],
    "allow_provisional": True,
    "sim_mode": False,
    "martingale": True,
    "martingale_type": "flat",
    "base_stake": 10,
    "max_stake": 500,
    "daily_loss_limit": 1000,
    "daily_profit_target": 500,
    "betting_windows": [["07:00", "10:00"], ["22:00", "04:00"]],
    # SINGLE banker odds window (narrow it to raise the win rate) and an
    # optional call whitelist, e.g. ["O2.5"] to only play Over 2.5 singles.
    "single_odds_min": 1.60,
    "single_odds_max": 2.04,
    "allowed_calls": [],
    # Upgraded single: only bet an Over 2.5 single when this many secondary
    # signals (BTTS + expected goals) also agree. 0 = off (bet every GW),
    # 2 = recommended (~61% win vs ~55%, far fewer bets, shorter streaks).
    "require_signal_agreement": 0,
    # Goal-fest singles are priced cheaper, so when the upgrade is ON the
    # single uses THIS lower odds floor instead of single_odds_min (most
    # 2+ signal Overs sit at 1.50-1.60).
    "upgraded_odds_min": 1.50,
    # After this many consecutive losses, abandon the recovery and go back to
    # the base stake (0 = never). Protects you when connectivity makes the
    # ladder deeper than your real losing streak.
    "max_martingale_steps": 6,
    # Don't START a placement attempt with fewer than this many seconds left
    # before kickoff. A full click->stake->place->confirm run takes ~20-30s,
    # so anything under ~10 is usually a wasted attempt.
    "min_place_seconds": 12,
    "odds_tolerance": 0.15,
    "min_odds": 1.35,
    "max_odds": 4.0,
    "racing_sport": "Greyhound Racing",
    "racing_venue": "Santa Monica",
}

BET_TYPES = ("single", "upgraded_single", "two_odds_single",
             "two", "three", "racing_place")

# Selectable strategies. Each preset is just a named bundle of the knobs
# below, so switching strategy at startup is one answer instead of four.
# api_type = what /api/banker_pick understands (it only knows one/two/three).
BET_TYPE_PRESETS = {
    # Plain O/U single across the normal 1.60-2.04 window.
    "single": {
        "api_type": "single",
        "desc": "plain single, odds 1.60-2.04",
        "cfg": {"require_signal_agreement": 0, "single_odds_min": 1.60,
                "single_odds_max": 2.04, "allowed_calls": ["O2.5"]},
    },
    # Goal-fest single: only fires when 2+ Over signals agree. Backtested
    # ~61-63% vs ~55% baseline and it HELD split-half — the one verified
    # win-rate lift. Priced cheap (~1.50-1.60), hence the lower floor.
    "upgraded_single": {
        "api_type": "single",
        "desc": "goal-fest Over 2.5 single (2+ signals), odds 1.50-2.04",
        "cfg": {"require_signal_agreement": 2, "upgraded_odds_min": 1.50,
                "single_odds_max": 2.04, "allowed_calls": ["O2.5"]},
    },
    # A SINGLE priced near 2.00 (one leg, margin paid once) instead of a
    # 2-leg ticket. NOTE: split-half testing (2026-07-26) showed the England
    # 1.90-2.04 Over edge was noise — treat this as a coin flip at ~2 odds,
    # not an edge.
    "two_odds_single": {
        "api_type": "single",
        "desc": "single priced ~2.00 (odds 1.90-2.04) — no verified edge",
        "cfg": {"require_signal_agreement": 0, "single_odds_min": 1.90,
                "single_odds_max": 2.04, "allowed_calls": ["O2.5"]},
    },
    # Two independent legs. Margin multiplies (~-13% EV) — kept for choice.
    "two": {"api_type": "two", "desc": "2-leg ticket (margin paid twice)", "cfg": {}},
    "three": {"api_type": "three", "desc": "3-leg ticket (margin paid 3x)", "cfg": {}},
    "racing_place": {"api_type": "racing_place", "desc": "racing place/show", "cfg": {}},
}


def api_bet_type(bet_type):
    """Map a strategy preset onto the ticket type the predictor API knows."""
    return BET_TYPE_PRESETS.get(bet_type, {}).get("api_type", bet_type)


def apply_bet_type_preset(cfg):
    """Write the chosen strategy's knobs into cfg (called at startup so the
    running config always matches the strategy the user picked)."""
    preset = BET_TYPE_PRESETS.get(cfg.get("bet_type"))
    if preset:
        cfg.update(preset["cfg"])
    return cfg


_FAST = False


def set_fast_mode(on):
    """When a gameweek is about to lock, drop the humanising pauses so the
    click->stake->place run fits in the time left."""
    global _FAST
    _FAST = bool(on)


def rsleep(lo, hi):
    """Randomised pause, squeezed hard in fast mode."""
    if _FAST:
        lo, hi = lo * 0.15, hi * 0.15
    time.sleep(random.uniform(lo, hi))


def log(msg):
    print(f"  [{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ─────────────────────────────────────────────────────────────
#  Config + startup prompts
# ─────────────────────────────────────────────────────────────

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def _ask(prompt, current, cast=str, choices=None):
    """Prompt with the current value as default (Enter keeps it)."""
    extra = f" {list(choices)}" if choices else ""
    try:
        raw = input(f"  {prompt}{extra} [{current}]: ").strip()
    except EOFError:
        return current
    if not raw:
        return current
    try:
        val = cast(raw)
    except ValueError:
        print(f"    invalid — keeping {current}")
        return current
    if choices and val not in choices:
        print(f"    must be one of {list(choices)} — keeping {current}")
        return current
    return val


def _ask_bool(prompt, current):
    try:
        raw = input(f"  {prompt} (y/n) [{'y' if current else 'n'}]: ").strip().lower()
    except EOFError:
        return current
    if raw in ("y", "yes", "1", "true", "on"):
        return True
    if raw in ("n", "no", "0", "false", "off"):
        return False
    return current


def startup_prompts(cfg):
    print()
    print("  ── Setup (Enter keeps the value in [brackets]) " + "─" * 20)
    for name, p in BET_TYPE_PRESETS.items():
        print(f"    {name:16s} — {p['desc']}")
    cfg["bet_type"] = _ask("Bet type", cfg["bet_type"], str, BET_TYPES)
    apply_bet_type_preset(cfg)
    if cfg["bet_type"] in ("single", "upgraded_single", "two_odds_single"):
        # Show (and allow tweaking) the window the preset just set
        lo_key = ("upgraded_odds_min" if cfg["bet_type"] == "upgraded_single"
                  else "single_odds_min")
        cfg[lo_key] = _ask("  single odds MIN", cfg[lo_key], float)
        cfg["single_odds_max"] = _ask("  single odds MAX",
                                      cfg["single_odds_max"], float)
    if cfg["bet_type"] == "racing_place":
        cfg["racing_sport"] = _ask("Racing sport", cfg["racing_sport"])
        cfg["racing_venue"] = _ask("Racing venue", cfg["racing_venue"])
        def _markets(raw):
            parts = [p.strip().lower() for p in str(raw).split(",") if p.strip()]
            if not parts or any(p not in ("win", "place", "show") for p in parts):
                raise ValueError(raw)
            return ",".join(dict.fromkeys(parts))
        cfg["racing_market"] = _ask(
            "Racing market(s) for the 2nd & 3rd picks — win/place/show, "
            "comma for several (show = finish top 3)",
            cfg.get("racing_market", "show"), _markets)
        cfg["martingale"] = False
        print("  martingale: OFF (flat stakes for racing bets)")
    else:
        cfg["leagues"] = _ask("Leagues to bet (comma list, blank = best of ALL)",
                              cfg.get("leagues", ""))

        def _odds_list(raw):
            raw = str(raw).strip()
            if not raw:
                return []
            out = []
            for tok in raw.replace(" ", "").split(","):
                if tok:
                    out.append(round(float(tok), 2))
            return out
        cur_skip_list = cfg.get("skip_odds", [])
        if not isinstance(cur_skip_list, list):
            cur_skip_list = []
        cur_skip = ",".join(str(x) for x in cur_skip_list)
        ans = _ask(
            "Skip a GW if the pick's odds equals any of these (comma list, "
            "e.g. 1.72,1.73,1.74 — blank = never skip)", cur_skip, _odds_list)
        cfg["skip_odds"] = cur_skip_list if ans == cur_skip else ans
        cfg["martingale"] = _ask_bool("Martingale (recover losses next bet)?",
                                      cfg["martingale"])
    cfg["base_stake"] = _ask("Base stake (NGN)", cfg["base_stake"], int)
    if cfg["martingale"]:
        def _mtype(raw):
            r = str(raw).strip().lower()
            if r in ("flat", "f", "1"):
                return "flat"
            if r in ("progressive", "prog", "p", "2"):
                return "progressive"
            raise ValueError(raw)
        cfg["martingale_type"] = _ask(
            "Martingale type — 'flat' (same profit each recovery) or "
            "'progressive' (profit grows each step)",
            cfg.get("martingale_type", "flat"), _mtype)
        cfg["max_stake"] = _ask("Max stake cap (NGN)", cfg["max_stake"], int)
        cfg["max_martingale_steps"] = _ask(
            "Max martingale steps before resetting to base (0 = never)",
            cfg.get("max_martingale_steps", 6), int)
    cfg["daily_profit_target"] = _ask("Daily profit target (NGN)",
                                      cfg["daily_profit_target"], int)
    cfg["daily_loss_limit"] = _ask("Daily loss limit (NGN)",
                                   cfg["daily_loss_limit"], int)
    cfg["sim_mode"] = _ask_bool("SIMULATION mode (no browser — validate "
                                "picks/martingale/settlement only)?",
                                cfg.get("sim_mode", False))
    if not cfg["sim_mode"]:
        cfg["dry_run"] = _ask_bool("DRY RUN (open browser, verify slip, "
                                   "but never Place Bet)?", cfg["dry_run"])
    save_config(cfg)
    print("  ── saved to bettor_config.json " + "─" * 34)
    print()
    return cfg


# ─────────────────────────────────────────────────────────────
#  State / ledger / rails
# ─────────────────────────────────────────────────────────────

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


def today_key():
    return date.today().isoformat()


def day_profit(st):
    return st.get("days", {}).get(today_key(), {}).get("profit", 0.0)


def record_outcome(st, stake, odds, won):
    d = st.setdefault("days", {}).setdefault(today_key(), {"profit": 0.0, "bets": 0})
    d["bets"] += 1
    d["profit"] += (stake * (odds - 1)) if won else (-stake)
    save_state(st)


def in_window(cfg, now=None):
    """Supports overnight windows like 22:00-04:00."""
    now = now or datetime.now()
    cur = now.strftime("%H:%M")
    for start, end in cfg["betting_windows"]:
        if start <= end:
            if start <= cur <= end:
                return True
        else:  # crosses midnight
            if cur >= start or cur <= end:
                return True
    return False


def next_window_start(cfg, now=None):
    """HH:MM of the next window start, for friendly idle logging."""
    now = now or datetime.now()
    cur = now.strftime("%H:%M")
    starts = sorted(w[0] for w in cfg["betting_windows"])
    for s in starts:
        if s > cur:
            return f"today {s}"
    return f"tomorrow {starts[0]}" if starts else None


def next_stake(cfg, st, odds):
    """Stake for the current attempt. Rounds UP to the nearest base_stake.

    Two martingale styles (config 'martingale_type'):
      'flat'        — every recovery aims for the SAME profit = base*(odds-1).
      'progressive' — step n aims for profit = base*(odds-1)*n, so the win at
                      step n nets a bit more each deeper step.
    Formula (both):  stake = ceil((cum_loss + target) / (odds-1) / base) * base
    """
    base = cfg["base_stake"]
    if not cfg.get("martingale"):
        return base
    pl = odds - 1.0
    if pl <= 0.01:
        return base
    cum_loss = st.get("cum_loss", 0)
    step = st.get("mart_losses", 0) + 1          # 1-based attempt number
    if cfg.get("martingale_type", "flat") == "progressive":
        target = base * pl * step
    else:
        target = base * pl
    exact = (cum_loss + target) / pl
    # epsilon guards against float error (e.g. 50.0000001) rounding a clean
    # stake UP a whole extra base multiple — a fresh 50 must stay 50, not 100
    rounded = math.ceil(exact / base - 1e-9) * base
    return int(max(base, rounded))


def on_win(st):
    st["cum_loss"] = 0
    st["mart_losses"] = 0


def on_loss(st, stake, cfg):
    if cfg.get("martingale"):
        st["cum_loss"] = st.get("cum_loss", 0) + stake
        st["mart_losses"] = st.get("mart_losses", 0) + 1
        cap = int(cfg.get("max_martingale_steps") or 0)
        if cap and st["mart_losses"] >= cap:
            log(f"Martingale hit its {cap}-step cap (NGN {st['cum_loss']:,.0f} "
                f"unrecovered) — RESETTING to base stake and taking the loss. "
                f"This stops one bad run from compounding forever.")
            st["cum_loss"] = 0
            st["mart_losses"] = 0


def stop_program(reason, st=None):
    log("=" * 56)
    log(f"STOPPED: {reason}")
    if st is not None:
        d = st.get("days", {}).get(today_key(), {})
        log(f"Today: profit NGN {day_profit(st):+,.0f} over {d.get('bets', 0)} bets")
    log("Restart the program when you want to continue.")
    log("=" * 56)
    sys.exit(0)


def rails_check(cfg, st):
    p = day_profit(st)
    if p >= cfg["daily_profit_target"]:
        stop_program(f"daily PROFIT TARGET reached (NGN {p:+,.0f})", st)
    if p <= -cfg["daily_loss_limit"]:
        stop_program(f"daily LOSS LIMIT hit (NGN {p:+,.0f})", st)


# ─────────────────────────────────────────────────────────────
#  Browser
# ─────────────────────────────────────────────────────────────

def launch_browser():
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    ctx = None
    for channel in ("chrome", "msedge", None):
        try:
            kw = dict(headless=False, viewport={"width": 1240, "height": 900},
                      args=["--disable-blink-features=AutomationControlled",
                            "--window-size=1270,960", "--window-position=40,20"])
            if channel:
                kw["channel"] = channel
            ctx = pw.chromium.launch_persistent_context(PROFILE_DIR, **kw)
            break
        except Exception:
            continue
    if ctx is None:
        pw.stop()
        raise RuntimeError("Could not launch any browser (Chrome/Edge/Chromium)")
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return pw, ctx, page


GAME_MARKERS = ("Football League", "Greyhound Racing", "Results History",
                "Horse Racing")


def find_virtual_frame(ctx, timeout_s=45, quiet=False):
    """Scan EVERY frame of EVERY open tab for the virtual-games CONTENT
    (sidebar markers) — the provider's domain varies between sessions, so
    never match by URL alone. Child frames are preferred over main pages."""
    deadline = time.time() + timeout_s
    seen_urls = set()
    while time.time() < deadline:
        candidates = []
        for pg in list(ctx.pages):
            for fr in pg.frames:
                seen_urls.add((fr.url or "")[:90])
                try:
                    txt = fr.evaluate(
                        "() => (document.body.innerText || '').slice(0, 2500)")
                except Exception:
                    continue
                if "Login error" in txt or "CODE: CORE" in txt:
                    if not quiet:
                        log("Games session rejected (CORE login error) — will reload.")
                    return "login_error"
                hits = sum(1 for mk in GAME_MARKERS if mk in txt)
                if hits >= 2:
                    is_child = fr != pg.main_frame
                    candidates.append((is_child, fr))
        if candidates:
            # prefer a child iframe over the outer sportybet page
            candidates.sort(key=lambda t: not t[0])
            return candidates[0][1]
        time.sleep(2)
    log("Frames seen while searching: "
        + " | ".join(sorted(u for u in seen_urls if u)[:6]))
    return None


def logged_out(ctx):
    """True when the site is showing Log In / a password prompt instead of
    an account. (Balance readable => definitely logged in.)"""
    if read_balance() is not None:
        return False
    for txt in _all_texts():
        if re.search(r"\bLog In\b|\bLogin\b|Enter your password|Forgot Password",
                     txt, re.I) and "My Account" not in txt:
            return True
    return False


def hard_reload(ctx, page, attempts=3):
    """What you do by hand when the page half-loads: a FULL reload (cache
    bypassed), a couple of times, until the games actually render.
    Returns the games frame, or None."""
    for i in range(1, attempts + 1):
        log(f"HARD REFRESH {i}/{attempts} — reloading the whole page...")
        try:
            # close any stray tabs the site opened; keep one clean page
            for pg in list(ctx.pages):
                if pg is not page:
                    try:
                        pg.close()
                    except Exception:
                        pass
            page.goto("about:blank", timeout=20000)
            time.sleep(1.0)
            page.goto("https://www.sportybet.com/ng/virtual/",
                      timeout=90000, wait_until="domcontentloaded")
        except Exception as e:
            log(f"  reload attempt failed: {str(e)[:80]}")
            time.sleep(5)
            continue
        time.sleep(9)
        try:
            page.reload(timeout=60000, wait_until="domcontentloaded")
            time.sleep(7)
        except Exception:
            pass

        if logged_out(ctx):
            log("  page came back LOGGED OUT.")
            wait_for_login(ctx, page)

        fr = find_virtual_frame(ctx, timeout_s=45)
        if fr and fr != "login_error":
            log("  games loaded after hard refresh.")
            return fr
        log("  games still not rendering...")
        time.sleep(6)
    return None


def goto_virtuals(ctx, page):
    for attempt in range(5):
        # Only navigate on the first attempt and every second retry — the
        # games app takes a while to boot and constant reloads reset it
        if attempt % 2 == 0:
            try:
                page.goto("https://www.sportybet.com/ng/virtual/", timeout=60000,
                          wait_until="domcontentloaded")
            except Exception:
                pass
            time.sleep(7)
        fr = find_virtual_frame(ctx, timeout_s=40)
        if fr == "login_error":
            time.sleep(6)
            continue
        if fr is not None:
            log("Virtual games found.")
            try:
                # zoom out so the virtualized list renders more rows at once
                fr.evaluate("() => { document.body.style.zoom = '0.8'; }")
            except Exception:
                pass
            return fr
        log(f"Virtual games not detected yet (attempt {attempt + 1}/5, "
            f"tabs open: {len(ctx.pages)})...")
    return None


_CTX = None   # browser context, set in main() — used for balance / error toast


def set_browser_ctx(ctx):
    global _CTX
    _CTX = ctx


def _all_texts():
    """innerText of every frame of every tab (the error toast and the balance
    can live outside the games iframe)."""
    out = []
    if _CTX is None:
        return out
    for pg in list(_CTX.pages):
        for fr in pg.frames:
            try:
                out.append(fr.evaluate(
                    "() => (document.body.innerText || '').slice(0, 4000)") or "")
            except Exception:
                continue
    return out


def read_balance():
    """Account balance from the header (anchored to 'Deposit' so promo
    numbers can't be mistaken for it). None if not readable / logged out."""
    for txt in _all_texts():
        # the balance is the NGN figure immediately before 'Deposit' — no other
        # NGN may sit between, else a promo banner gets read as the balance
        m = re.search(r"NGN\s*([\d,]+\.\d{2})(?:(?!NGN)[\s\S]){0,40}?Deposit", txt)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                pass
    return None


def site_error_shown():
    """SportyBet's transient red toast — a DEFINITIVE 'bet did not go
    through' signal (common on a flaky connection)."""
    for txt in _all_texts():
        if re.search(r"Error\.\s*Please try again|problem persists contact support"
                     r"|Bet failed|not accepted|try again later", txt, re.I):
            return True
    return False


def wait_for_login(ctx, page):
    warned = False
    while True:
        for pg in list(ctx.pages):
            try:
                txt = pg.evaluate("() => (document.body.innerText || '').slice(0, 3000)")
            except Exception:
                continue
            # balance sits right before 'Deposit' in the header — anchor
            # there so promo banners can't be mistaken for the balance
            m = re.search(r"NGN\s*([\d,]+\.\d{2})[\s\S]{0,40}?Deposit", txt)
            if not m:
                m = re.search(r"CREDIT\s*₦?\s*([\d,]+)", txt)
            if m and "My Account" in txt:
                log(f"Logged in. Balance: NGN {m.group(1)}")
                return
        if not warned:
            print("\a", end="", flush=True)   # terminal bell
            print("")
            print("  " + "!" * 64)
            print("  !!  LOGIN NEEDED — type your password in the browser window.")
            print("  !!  The bot is PAUSED and will resume by itself the moment")
            print("  !!  it sees your balance again. Nothing is lost.")
            print("  " + "!" * 64)
            print("")
            warned = True
        else:
            # keep nudging so you notice if you stepped away
            if int(time.time()) % 30 < 4:
                print("\a", end="", flush=True)
        time.sleep(4)


# ─────────────────────────────────────────────────────────────
#  Betslip helpers
# ─────────────────────────────────────────────────────────────

def clear_betslip(frame):
    """Empty the betslip. The 'Clear' control needs a real pointer event
    (Angular ignores a plain JS .click()), so tag + Playwright-click it,
    and verify the slip actually emptied."""
    def is_empty():
        s = read_betslip(frame)
        return ("clicking on the respective odds" in s.lower()
                or not re.search(r"#\d{6,}", s))

    for _ in range(4):
        if is_empty():
            return True
        clicked = False
        try:
            loc = frame.locator("a.clear").first
            if loc.count() and loc.is_visible():
                loc.click(timeout=3000)
                clicked = True
        except Exception:
            clicked = False
        if not clicked:
            try:
                frame.evaluate("""() => {
                    const a = document.querySelector('a.clear')
                        || Array.from(document.querySelectorAll('a,button,div'))
                            .find(e => /^Clear\\b/i.test((e.textContent||'').trim())
                                       && (e.textContent||'').trim().length < 24);
                    if (a) a.dispatchEvent(new MouseEvent('click',
                        { bubbles: true, cancelable: true, view: window }));
                }""")
            except Exception:
                pass
        time.sleep(1.2)
    return is_empty()


def read_betslip(frame):
    try:
        return frame.evaluate(r"""() => {
            const body = document.body.innerText || '';
            const i = body.indexOf('Betslip');
            if (i < 0) return '';
            return body.slice(i, i + 1600);
        }""")
    except Exception:
        return ""


def _sel_label(call):
    return {"1": "Home", "2": "Away",
            "O2.5": "Over 2.5", "U2.5": "Under 2.5"}.get(call, call)


def verify_football_slip(slip_text, legs, tolerance):
    """All legs present, nothing extra, market+selection+odds all right.
    Returns list of problems, or the slip's total odds (float) if clean."""
    problems = []
    if not slip_text:
        return ["betslip not readable"]
    n_sel = len(re.findall(r"#\d{6,}", slip_text))
    if n_sel != len(legs):
        problems.append(f"{n_sel} selections on slip (need {len(legs)})")
    for leg in legs:
        if leg["home"] not in slip_text or leg["away"] not in slip_text:
            problems.append(f"{leg['home']}-{leg['away']} not on slip")
            continue
        want_market = "Match result" if leg["market"] == "1X2" else "Over/Under"
        if want_market.lower() not in slip_text.lower():
            problems.append(f"market '{want_market}' missing")
        if _sel_label(leg["call"]).lower() not in slip_text.lower():
            problems.append(f"selection '{_sel_label(leg['call'])}' missing")
    if problems:
        return problems
    # Read the ACTUAL odds off the slip (we bet at live odds; a pre-known
    # odds is only used as a sanity check when available).
    slip_total = _read_slip_odds(slip_text, len(legs))
    if slip_total is None:
        return ["could not read the slip's odds"]
    # sanity: if we knew each leg's odds, the slip total shouldn't be wildly
    # different (guards against clicking the wrong cell)
    known = [l.get("odds") for l in legs if l.get("odds")]
    if len(known) == len(legs):
        expect = 1.0
        for o in known:
            expect *= o
        if expect > 0 and abs(slip_total - expect) / expect > 0.25:
            return [f"slip odds {slip_total} differ from expected {round(expect, 2)} "
                    f"(likely wrong cell clicked)"]
    return round(slip_total, 2)


def _read_slip_odds(slip_text, n_legs):
    """Total odds from the betslip. Prefers an explicit 'Total Odds' figure
    (accumulators); for a single, reads the selection's own odds, excluding
    O/U thresholds (2.5 etc.) and any figure after the stake/₦ section."""
    tm = re.search(r"Total Odds[:\s]*([\d]+\.\d{2})", slip_text, re.I)
    if tm:
        return float(tm.group(1))
    head = re.split(r"Total stake|Stake mode|Total Odds|Place Bet",
                    slip_text, 1)[0]
    thresholds = {0.5, 1.5, 2.5, 3.5, 4.5, 5.5}
    odds = [float(v) for v in re.findall(r"\b(\d{1,2}\.\d{2})\b", head)
            if 1.01 <= float(v) <= 30 and float(v) not in thresholds]
    if not odds:
        return None
    if n_legs == 1:
        return odds[-1]          # selection odds is the last such decimal
    total = 1.0
    for o in odds[:n_legs]:
        total *= o
    return round(total, 2)


def verify_racing_slip(slip_text, runner_name, odds_hint, tolerance, market="place"):
    problems = []
    if not slip_text:
        return ["betslip not readable"]
    n_sel = len(re.findall(r"#\d{6,}", slip_text))
    if n_sel != 1:
        problems.append(f"{n_sel} selections on slip (need 1)")
    if runner_name not in slip_text:
        problems.append(f"runner '{runner_name}' not on slip")
    if market.lower() not in slip_text.lower():
        problems.append(f"{market.upper()} market not on slip")
    if problems:
        return problems
    nums = [float(v) for v in re.findall(r"\b(\d{1,2}\.\d{2})\b", slip_text)]
    if odds_hint:
        near = [v for v in nums if abs(v - odds_hint) <= max(tolerance, 0.3)]
        if not near:
            return [f"odds near {odds_hint} not on slip (saw {nums[:4]})"]
        return near[0]
    return nums[0] if nums else ["no odds on slip"]


def enter_stake_and_place(frame, stake, expect_odds, dry_run):
    """Type stake, verify potential return, place. -> 'placed'|'dry_run'|error"""
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
        # Type the stake and VERIFY it landed. Angular re-renders the betslip
        # while you type, which can swallow/scramble characters (that is what
        # produced '550' instead of '5300' and cost a whole gameweek).
        # So: try, check, and retry with a harder method before giving up.
        want = str(int(stake))
        typed = ""
        for attempt in range(4):
            try:
                target.click()
                rsleep(0.25, 0.6)
                target.fill("")
                time.sleep(0.25)
                if attempt == 0:
                    target.type(want, delay=random.randint(60, 140))
                else:
                    # fill() sets the value atomically — no per-key re-render
                    target.fill(want)
                rsleep(0.7, 1.2)
                typed = (target.input_value() or "").replace(",", "").strip()
            except Exception as e:
                typed = ""
                log(f"  stake entry attempt {attempt + 1} errored: {str(e)[:60]}")
            if typed in (want, f"{stake:.2f}", str(stake)):
                break
            if attempt < 3:
                log(f"  stake box shows '{typed}' not {want} — retrying "
                    f"({attempt + 2}/4)...")
                time.sleep(0.6)
        if typed not in (want, f"{stake:.2f}", str(stake)):
            return (f"not placed: stake box would not accept {want} "
                    f"(shows '{typed}') after 4 tries")

        # Potential-return figure is a nice cross-check but its on-page format
        # varies (1 vs 2 decimals, commas, currency) — warn, don't abort.
        body = frame.evaluate("() => document.body.innerText || ''")
        expect = stake * expect_odds
        nums = re.findall(r"([\d,]+(?:\.\d{1,2})?)", body)
        pot_ok = any(abs(float(v.replace(",", "")) - expect) <= max(1.0, expect * 0.03)
                     for v in nums if v.replace(",", "").replace(".", "").isdigit())
        if not pot_ok:
            log(f"  note: expected return ~{expect:.0f} not clearly shown; "
                f"selection+odds+stake are verified, proceeding.")

        if dry_run:
            return "dry_run"

        # Real mouse click — Angular ignores plain JS .click() (same lesson
        # as the odds cells and the Clear button). We do NOT infer success
        # here; execute_bet confirms via My Bets (the only reliable signal).
        if not _real_click_text(frame, "Place Bet"):
            return "not placed: Place Bet button not found"
        time.sleep(2.0)
        _real_click_text(frame, "Confirm")   # optional confirm dialog
        _real_click_text(frame, "OK")
        time.sleep(2.5)
        return "clicked"
    except Exception as e:
        return f"place error: {str(e)[:120]}"


def _real_click_text(frame, text, timeout=3000):
    """Tag the visible element whose trimmed text == `text`, then click it
    with a real Playwright mouse event (Angular ignores JS .click())."""
    try:
        tagged = frame.evaluate("""(text) => {
            document.querySelectorAll('[data-bot-click]')
                .forEach(e => e.removeAttribute('data-bot-click'));
            for (const el of document.querySelectorAll('button, a, div, span')) {
                if ((el.textContent || '').trim() === text
                        && el.offsetParent !== null && el.offsetWidth > 0) {
                    el.setAttribute('data-bot-click', '1');
                    return true;
                }
            }
            return false;
        }""", text)
        if not tagged:
            return False
        frame.locator("[data-bot-click='1']").first.click(timeout=timeout)
        return True
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────
#  Football slip building
# ─────────────────────────────────────────────────────────────

def nav_to_league_upcoming(frame, league):
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
        # The league is picked from the SIDEBAR submenu under Football
        # League (there are no league tabs in the upcoming view)
        frame.evaluate("""(league) => {
            const links = Array.from(document.querySelectorAll('a'));
            const fl = links.find(a => a.textContent.trim() === 'Football League');
            const li = fl && fl.closest('li');
            if (li) {
                for (const a of li.querySelectorAll('a')) {
                    if (a.textContent.trim() === league && a.offsetParent !== null) {
                        a.click(); return;
                    }
                }
            }
            for (const el of document.querySelectorAll('div.item, div[class*="item"], a')) {
                if (el.textContent.trim() === league && el.offsetParent !== null) {
                    el.click(); return;
                }
            }
        }""", league)

        # verify the content pane actually switched to this league
        for _ in range(8):
            time.sleep(1.0)
            try:
                body = frame.evaluate(
                    "() => (document.body.innerText || '').slice(0, 4000)")
            except Exception:
                body = ""
            if re.search(rf"(Football League:?\s*{league}|Week\s*\d+\s*-\s*{league})",
                         body):
                return True
        log(f"Page did not switch to {league} after clicking — will retry next cycle.")
        return False
    except Exception as e:
        log(f"nav error: {e}")
        return False


def select_market_tab(frame, market):
    """Switch the market filter and VERIFY the grid actually changed —
    a plain click on the tab text doesn't always trigger the SPA handler,
    so escalate strategies until the right columns are visible."""
    if market == "1X2":
        label, marker = "MAIN", "DRAW"
    else:
        label, marker = "Over/Under", "OV 2.5"

    click_js = r"""(args) => {
        const { label, strategy } = args;
        const cands = [];
        for (const el of document.querySelectorAll('a, li, span, div, button')) {
            const t = (el.textContent || '').trim();
            if (t === label && el.offsetParent !== null && el.offsetWidth > 0
                    && el.textContent.length < 30) {
                cands.push(el);
            }
        }
        if (!cands.length) return 'not_found';
        for (const el of cands) {
            if (strategy === 0) {
                el.click();
            } else if (strategy === 1 && el.parentElement) {
                el.parentElement.click();
            } else {
                el.dispatchEvent(new MouseEvent('click',
                    { bubbles: true, cancelable: true, view: window }));
            }
        }
        return 'clicked_' + cands.length;
    }"""

    def body_text():
        try:
            return frame.evaluate(
                "() => (document.body.innerText || '').slice(0, 8000)") or ""
        except Exception:
            return ""

    # Wait for the games app to finish rendering before deciding anything —
    # a half-loaded page has no tabs to click, and giving up here is what
    # used to trigger a refresh that restarted the load (an endless cycle).
    for _ in range(20):                      # up to ~10s
        t = body_text()
        if ("Football League" in t or "Market filter" in t
                or marker in t or "MAIN" in t):
            break
        time.sleep(0.5)

    for attempt in range(4):
        try:
            if marker in body_text():
                return True                   # already on the right grid
            r = frame.evaluate(click_js, {"label": label,
                                          "strategy": min(attempt, 2)})
            # POLL for the grid to change. These waits must NOT be shrunk by
            # fast mode — the SPA needs real time to re-render, and shrinking
            # them was exactly what broke this.
            for _ in range(16):               # up to ~8s per attempt
                time.sleep(0.5)
                if marker in body_text():
                    log(f"Market tab '{label}' selected ({r}).")
                    return True
        except Exception:
            time.sleep(1.0)
    log(f"Could not switch market tab to '{label}' — grid never showed "
        f"'{marker}' after ~40s of waiting.")
    return False


_SCROLL_JS = r"""(dir) => {
    // scroll the fixtures panel (it's virtualized — rows only exist near
    // the current scroll position). dir: 'top' | 'down'
    let target = null;
    for (const el of document.querySelectorAll('div')) {
        if (el.scrollHeight > el.clientHeight + 80 && el.clientHeight > 150) {
            if (!target || el.scrollHeight > target.scrollHeight) target = el;
        }
    }
    if (dir === 'top') {
        if (target) target.scrollTop = 0; else window.scrollTo(0, 0);
        return true;
    }
    if (target) {
        const before = target.scrollTop;
        target.scrollTop = before + target.clientHeight * 0.8;
        return target.scrollTop > before;
    }
    const by = window.scrollY;
    window.scrollBy(0, window.innerHeight * 0.8);
    return window.scrollY > by;
}"""


def click_leg_odds(frame, leg):
    """Find and click the leg's odds cell, scrolling the virtualized list
    step by step until the match row is actually rendered."""
    try:
        frame.evaluate(_SCROLL_JS, "top")
        time.sleep(0.6)
    except Exception:
        pass
    for step in range(10):
        res = _try_click_leg(frame, leg)
        if res and not res.get("err"):
            return res
        if res and res.get("err") not in ("row_not_found", "no_cells"):
            return res  # real failure, not a rendering issue
        try:
            moved = frame.evaluate(_SCROLL_JS, "down")
        except Exception:
            return {"err": "scroll_failed"}
        if not moved:
            return res  # reached the bottom without finding the row
        rsleep(0.5, 0.9)
    return {"err": "row_not_found_after_scroll"}


def _try_click_leg(frame, leg):
    """Click the odds cell by its LABEL — cells render as label+odds
    ('1  2.05', 'X 3.76', 'OV 2.5 1.76', 'UN 2.5 2.06'), so we match the
    exact market label and never count positions."""
    js = r"""(args) => {
        const { home, away, call } = args;
        const wanted = { '1': '1', 'X': 'X', '2': '2',
                         'O2.5': 'OV 2.5', 'U2.5': 'UN 2.5' }[call];
        if (!wanted) return { err: 'bad_call' };
        const expected = (call === 'O2.5' || call === 'U2.5') ? 8 : 3;

        const readCell = (odd) => {
            const nameEl = odd.querySelector('.odd-name');
            const valEl = odd.querySelector('.odd-value');
            if (!nameEl || !valEl) return null;
            const name = (nameEl.textContent || '').trim().replace(/\s+/g, ' ');
            const m = (valEl.textContent || '').trim().match(/(\d+\.\d{2})/);
            return m ? { name, val: parseFloat(m[1]) } : null;
        };

        // A match ROW is the SMALLEST element holding exactly `expected`
        // app-odd cells. Collect them, then match by team codes.
        const rows = [];
        for (const el of document.querySelectorAll('div, tr, li')) {
            const odds = el.querySelectorAll('app-odd');
            if (odds.length !== expected) continue;
            // smallest: no descendant also has exactly `expected`
            let inner = false;
            for (const ch of el.querySelectorAll('div, tr, li')) {
                if (ch.querySelectorAll('app-odd').length === expected) { inner = true; break; }
            }
            if (!inner) rows.push(el);
        }
        if (!rows.length) return { err: 'no_rows', expected };

        const codeRe = /\b[A-Z]{2,4}\b/g;
        let matchRow = null;
        for (const r of rows) {
            const codes = (r.textContent || '').match(codeRe) || [];
            if (codes.includes(home) && codes.includes(away)) { matchRow = r; break; }
        }
        if (!matchRow) {
            const sample = rows.slice(0, 3).map(r =>
                ((r.textContent || '').match(codeRe) || []).slice(0, 2).join('-'));
            return { err: 'row_not_found', rows: rows.length, sample };
        }

        for (const odd of matchRow.querySelectorAll('app-odd')) {
            const c = readCell(odd);
            if (c && c.name === wanted) {
                if (odd.offsetParent === null) return { err: 'cell_hidden' };
                document.querySelectorAll('[data-bot-pick]')
                    .forEach(e => e.removeAttribute('data-bot-pick'));
                (odd.querySelector('.odd-value') || odd)
                    .setAttribute('data-bot-pick', '1');
                return { tagged: true, val: c.val };
            }
        }
        const seen = Array.from(matchRow.querySelectorAll('app-odd'))
            .map(o => (readCell(o) || {}).name).filter(Boolean);
        return { err: 'label_not_found', want: wanted, saw: seen };
    }"""
    try:
        res = frame.evaluate(js, {"home": leg["home"], "away": leg["away"],
                                  "call": leg["call"]})
    except Exception as e:
        return {"err": str(e)[:100]}
    if not res.get("tagged"):
        return res
    # Real click via Playwright. Retry: the virtualised list can scroll the
    # cell out from under the click, which times out and used to cost the
    # whole gameweek.
    loc = frame.locator("[data-bot-pick='1']").first
    last = ""
    for attempt in range(3):
        try:
            if attempt:
                loc.scroll_into_view_if_needed(timeout=3000)
                time.sleep(0.4)
            loc.click(timeout=6000)
            return {"clicked": res["val"]}
        except Exception as e:
            last = str(e)[:70]
            time.sleep(0.5)
    return {"err": "locator_click after 3 tries: " + last}


def gw_seconds_left(frame, league, week):
    """Seconds until this GW kicks off, from the section header
    ('01:13 Football League: England Week 36'). None if not visible."""
    try:
        body = frame.evaluate("() => (document.body.innerText || '').slice(0, 6000)")
    except Exception:
        return None
    m = re.search(rf"(\d{{1,2}}):(\d{{2}})\s*Football League:?\s*{league}\s*Week\s*{week}\b",
                  body)
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def build_football_slip(frame, pick, cfg):
    """Add every leg (possibly across market tabs), verify the whole slip.
    Returns slip total odds (float) or None."""
    clear_betslip(frame)
    if not nav_to_league_upcoming(frame, pick["league"]):
        log("Navigation failed — skipping this GW.")
        return None

    # Bettable until min_place_seconds before kickoff — only then move on
    left = gw_seconds_left(frame, pick["league"], pick["week"])
    floor = int(cfg.get("min_place_seconds", 12))
    if left is not None:
        if left < floor:
            log(f"GW{pick['week']} kicks off in {left}s (floor {floor}s) — "
                f"too close, moving on.")
            return None
        set_fast_mode(left < 60)
        log(f"GW{pick['week']} kicks off in {left}s — proceeding"
            + ("  [FAST MODE]" if left < 60 else "") + ".")
    # group legs by market so we only switch tabs when needed
    for market in ("1X2", "Over/Under 2.5"):
        legs = [l for l in pick["legs"] if l["market"] == market]
        if not legs:
            continue
        if not select_market_tab(frame, "1X2" if market == "1X2" else "OU"):
            clear_betslip(frame)
            return None
        for leg in legs:
            res = click_leg_odds(frame, leg)
            if not res or res.get("err"):
                log(f"Could not click {leg['home']}-{leg['away']} [{leg['call']}] "
                    f"({res}) — skipping this GW.")
                clear_betslip(frame)
                return None
            rsleep(0.7, 1.4)
    rsleep(0.8, 1.5)
    slip = read_betslip(frame)
    v = verify_football_slip(slip, pick["legs"], cfg["odds_tolerance"])
    if isinstance(v, list):
        log(f"SLIP REJECTED: {'; '.join(v)} — skipping this GW.")
        clear_betslip(frame)
        return None
    log(f"Slip verified: {len(pick['legs'])} selection(s), total odds {v}")
    return v


# ─────────────────────────────────────────────────────────────
#  Racing slip building
# ─────────────────────────────────────────────────────────────

def nav_to_racing_venue(frame, sport, venue):
    try:
        frame.evaluate("""(sport) => {
            const links = Array.from(document.querySelectorAll('a'));
            const tg = links.find(a => a.textContent.trim() === sport);
            if (tg && tg.classList.contains('collapsed')) tg.click();
        }""", sport)
        time.sleep(random.uniform(1.0, 1.8))
        frame.evaluate("""(args) => {
            const links = Array.from(document.querySelectorAll('a'));
            const tg = links.find(a => a.textContent.trim() === args.sport);
            const li = tg && tg.closest('li');
            if (!li) return;
            for (const a of li.querySelectorAll('a')) {
                if (a.textContent.trim() === args.venue && a.offsetParent !== null) { a.click(); return; }
            }
        }""", {"sport": sport, "venue": venue})
        # venue pages switch slowly — verify header names the venue
        for _ in range(10):
            time.sleep(1.2)
            txt = frame.evaluate("() => (document.body.innerText || '').slice(0, 4000)")
            if re.search(rf"{re.escape(sport)}:\s*{re.escape(venue)}", txt):
                return True
        return False
    except Exception as e:
        log(f"racing nav error: {e}")
        return False


def click_racing_odds(frame, race_id, runner_name, market="place"):
    """Click the WIN/PLACE/SHOW odds of runner_name in the race block with
    this race_id, scrolling the virtualized list until it's rendered."""
    try:
        frame.evaluate("""() => {
            for (const el of document.querySelectorAll('a, li, span, div, button')) {
                const t = (el.textContent || '').trim();
                if (t === 'Win/Place/Show' && el.offsetParent !== null) { el.click(); return; }
            }
        }""")
        time.sleep(random.uniform(1.2, 2.0))
        frame.evaluate(_SCROLL_JS, "top")
        time.sleep(0.6)
    except Exception:
        pass
    for step in range(8):
        res = _try_click_racing(frame, race_id, runner_name, market)
        if res and not res.get("err"):
            return res
        if res and res.get("err") not in ("race_block_not_found", "runner_row_not_found"):
            return res
        try:
            moved = frame.evaluate(_SCROLL_JS, "down")
        except Exception:
            return {"err": "scroll_failed"}
        if not moved:
            return res
        time.sleep(random.uniform(0.5, 0.9))
    return {"err": "race_not_found_after_scroll"}


def _try_click_racing(frame, race_id, runner_name, market):
    js = r"""(args) => {
        const { raceId, runner } = args;
        // find the race block containing #raceId
        let block = null;
        for (const el of document.querySelectorAll('div, section, table')) {
            const t = (el.textContent || '');
            if (t.includes('#' + raceId) && t.includes(runner)
                    && t.length < 3000) {
                if (!block || el.textContent.length < block.textContent.length) block = el;
            }
        }
        if (!block) return { err: 'race_block_not_found' };
        // find the runner's row
        let row = null;
        for (const el of block.querySelectorAll('div, tr, li')) {
            const t = (el.textContent || '');
            if (t.includes(runner) && t.length < 300) {
                if (!row || el.textContent.length < row.textContent.length) row = el;
            }
        }
        if (!row) return { err: 'runner_row_not_found' };
        const cells = [];
        for (const el of row.querySelectorAll('span, div, button, a')) {
            const t = (el.textContent || '').trim();
            if (/^\d+\.\d{2}$/.test(t) && el.offsetParent !== null) {
                let clickable = el;
                for (let up = el; up && up !== row; up = up.parentElement) {
                    const cls = (up.className || '') + '';
                    if (cls.includes('odd') || cls.includes('outcome') || up.onclick) { clickable = up; break; }
                }
                cells.push({ el: clickable, val: parseFloat(t) });
            }
        }
        // Win/Place/Show columns => index 0 WIN, 1 PLACE, 2 SHOW
        if (cells.length < 3) return { err: 'cells_' + cells.length };
        const idx = args.market === 'win' ? 0 : (args.market === 'place' ? 1 : 2);
        // tag it for a real Playwright click (Angular ignores JS .click())
        document.querySelectorAll('[data-bot-pick]')
            .forEach(e => e.removeAttribute('data-bot-pick'));
        cells[idx].el.setAttribute('data-bot-pick', '1');
        return { tagged: true, val: cells[idx].val };
    }"""
    try:
        res = frame.evaluate(js, {"raceId": str(race_id),
                                  "runner": runner_name, "market": market})
    except Exception as e:
        return {"err": str(e)[:100]}
    if not res.get("tagged"):
        return res
    try:
        frame.locator("[data-bot-pick='1']").first.click(timeout=4000)
        return {"clicked": res["val"]}
    except Exception as e:
        return {"err": "locator_click: " + str(e)[:80]}


# ─────────────────────────────────────────────────────────────
#  My Bets settlement
# ─────────────────────────────────────────────────────────────

def open_my_bets(frame):
    _real_click_text(frame, "My Bets")
    time.sleep(1.8)


def open_betslip(frame):
    _real_click_text(frame, "Betslip")
    time.sleep(1.0)


def read_my_bets(frame, max_rows=12):
    """Return recent tickets newest-first with their full text block, so
    callers can match by team names (not just stake)."""
    try:
        open_my_bets(frame)
        txt = frame.evaluate("() => document.body.innerText || ''")
    except Exception:
        return []
    # split into per-ticket chunks starting at each '#<id>'
    ids = [(m.start(), m.group(1))
           for m in re.finditer(r"#(\d{6,})", txt)]
    rows = []
    for k, (pos, tid) in enumerate(ids):
        end = ids[k + 1][0] if k + 1 < len(ids) else min(pos + 400, len(txt))
        chunk = txt[pos:end]
        sm = re.search(r"₦\s*([\d,]+)", chunk)
        stake = float(sm.group(1).replace(",", "")) if sm else None
        wm = re.findall(r"₦\s*([\d,]+|--|—)", chunk)
        win_s = wm[1] if len(wm) > 1 else "--"
        settled = win_s not in ("--", "—")
        rows.append({"id": tid, "text": chunk, "stake": stake,
                     "settled": settled,
                     "win": float(win_s.replace(",", "")) if settled else None})
        if len(rows) >= max_rows:
            break
    return rows


def confirm_placed(frame, names, stake, retries=6):
    """Definitive placement check: a ticket whose text contains ALL `names`
    (team/runner codes) at ~this stake, appearing in My Bets. Generous
    retries because My Bets can lag a few seconds after placing."""
    names = [n for n in names if n]
    for _ in range(retries):
        for r in read_my_bets(frame)[:6]:
            if all(n in r["text"] for n in names) \
                    and (r["stake"] is None or abs(r["stake"] - stake) < 0.5):
                return r
        time.sleep(4)
    return None


def find_ticket(frame, ticket_id=None, stake=None, retries=3):
    for _ in range(retries):
        rows = read_my_bets(frame)
        for i, r in enumerate(rows):
            if ticket_id and r["id"] == ticket_id:
                return r
            if ticket_id is None and stake is not None and i < 3 \
                    and r["stake"] is not None and abs(r["stake"] - stake) < 0.5:
                return r
        time.sleep(4)
    return None


def wait_settlement(frame, ticket_id, timeout_s=480):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        row = find_ticket(frame, ticket_id=ticket_id, retries=1)
        if row and row["settled"]:
            won = (row["win"] or 0) > 0
            log(f"Settled: {'WON' if won else 'LOST'} (ticket #{ticket_id})")
            return won
        time.sleep(random.uniform(7, 12))
    return None


def wait_football_result(pick, timeout_s=420):
    leg_qs = []
    for leg in pick["legs"]:
        leg_qs.append(f"/api/bet_result?league={urllib.parse.quote(pick['league'])}"
                      f"&week={pick['week']}&home={urllib.parse.quote(leg['home'])}"
                      f"&away={urllib.parse.quote(leg['away'])}&call={urllib.parse.quote(leg['call'])}")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        results = [api_get(q) for q in leg_qs]
        if all(r and r.get("status") in ("won", "lost") for r in results):
            all_won = all(r["status"] == "won" for r in results)
            log("Result: " + ", ".join(
                f"{leg['home']}-{leg['away']} {r['status'].upper()} ({r.get('score')})"
                for leg, r in zip(pick["legs"], results)))
            return all_won
        time.sleep(12)
    return None


def wait_racing_result(pick, name, market="place", timeout_s=420):
    q = (f"/api/racing_result?sport={urllib.parse.quote(pick['sport'])}"
         f"&venue={urllib.parse.quote(pick['venue'])}"
         f"&race_id={pick['race_id']}&name={urllib.parse.quote(name)}"
         f"&market={market}")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = api_get(q)
        if r and r.get("status") in ("placed", "missed"):
            log(f"Race result: {name} {market.upper()} "
                f"{'HIT' if r['status'] == 'placed' else 'MISSED'} (pos {r.get('pos')})")
            return r["status"] == "placed"
        time.sleep(12)
    return None


# ─────────────────────────────────────────────────────────────
#  Bet execution (shared)
# ─────────────────────────────────────────────────────────────

def execute_bet(frame, st, cfg, stake, slip_odds, desc, names):
    """Place (or dry-run) the verified slip and CONFIRM via My Bets.
    `names` = team/runner codes that must appear in the created ticket.
    Returns ('placed', ticket_id) | ('dry_run', None) | (None, error).

    Safety: after clicking Place Bet we confirm a matching ticket exists in
    My Bets. If confirmed -> placed. If we cannot confirm, we STOP the program
    for manual check — we NEVER re-attempt (that is what caused multi-betting).
    """
    pre_bal = read_balance()
    st["placing"] = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                     "stake": stake, "odds": slip_odds, "desc": desc,
                     "names": names, "pre_balance": pre_bal}
    save_state(st)
    outcome = enter_stake_and_place(frame, stake, slip_odds, cfg["dry_run"])

    if outcome == "dry_run":
        log(f"DRY RUN: would place NGN {stake} @ {slip_odds}  ({desc})")
        st.pop("placing", None)
        save_state(st)
        clear_betslip(frame)
        return "dry_run", None

    if outcome == "clicked":
        # 1) The site's error toast = definitive "did NOT place". Common on a
        #    flaky connection; safe to skip and carry on (no double-bet risk).
        for _ in range(6):
            if site_error_shown():
                log("Site showed an error right after Place Bet — the bet did "
                    "NOT go through. Skipping this GW and continuing.")
                st.pop("placing", None)
                save_state(st)
                clear_betslip(frame)
                return None, "site error on Place Bet"
            time.sleep(0.7)

        # 2) Confirm by EITHER a matching My Bets ticket OR the balance
        #    dropping by the stake (balance survives re-login clearing My Bets)
        row = None
        for _ in range(6):
            row = confirm_placed(frame, names, stake, retries=1)
            if row is not None:
                break
            bal = read_balance()
            if pre_bal is not None and bal is not None and (pre_bal - bal) >= stake * 0.9:
                log(f"Balance fell NGN {pre_bal - bal:,.0f} — bet CONFIRMED placed "
                    f"(My Bets unreadable): NGN {stake} @ {slip_odds}  ({desc})")
                st["placing"]["ticket_id"] = None
                save_state(st)
                open_betslip(frame)
                return "placed", None
            time.sleep(3)

        if row is not None:
            log(f"BET PLACED & CONFIRMED in My Bets: NGN {stake} @ {slip_odds} "
                f"(#{row['id']}, {desc})")
            st["placing"]["ticket_id"] = row["id"]
            save_state(st)
            open_betslip(frame)
            return "placed", row["id"]

        # 3) No ticket AND balance unchanged => definitively not placed
        post_bal = read_balance()
        if pre_bal is not None and post_bal is not None \
                and abs(post_bal - pre_bal) < 0.5:
            log("No ticket and balance unchanged — the bet did NOT place. "
                "Skipping this GW and continuing.")
            st.pop("placing", None)
            save_state(st)
            clear_betslip(frame)
            return None, "not placed (balance unchanged)"

        # 4) Balance unreadable => probably logged out by a refresh. Wait for
        #    the user to log back in, then re-check before deciding.
        if post_bal is None:
            log("Balance unreadable — the session may have logged out.")
            wait_for_login(_CTX, frame.page if hasattr(frame, "page") else None)
            row = confirm_placed(frame, names, stake, retries=2)
            if row is not None:
                log(f"After re-login, ticket found (#{row['id']}) — placed.")
                st["placing"]["ticket_id"] = row["id"]
                save_state(st)
                return "placed", row["id"]
            bal = read_balance()
            if pre_bal is not None and bal is not None and (pre_bal - bal) >= stake * 0.9:
                log("After re-login, balance shows the stake was taken — placed.")
                return "placed", None
            if pre_bal is not None and bal is not None and abs(bal - pre_bal) < 0.5:
                log("After re-login, balance unchanged — the bet did NOT place. "
                    "Continuing.")
                st.pop("placing", None)
                save_state(st)
                clear_betslip(frame)
                return None, "not placed (balance unchanged after re-login)"

        # 5) Genuinely ambiguous — never re-bet blindly.
        stop_program(
            f"clicked Place Bet for '{desc}' but could not confirm it either "
            f"way (no ticket, balance unclear) — VERIFY MANUALLY before "
            f"restarting. (The bot never re-bets an unconfirmed slip.)", st)

    # explicit not-placed (button missing / error) — safe to skip, no bet made
    log(f"NOT placed: {outcome}")
    st.pop("placing", None)
    save_state(st)
    clear_betslip(frame)
    return None, outcome


def settle_from_balance(ph, timeout_s=420):
    """Settle a bet we KNOW placed but have no ticket id for (My Bets gets
    wiped by a re-login). Pure arithmetic on the balance:
        won  -> balance ends ABOVE pre_balance  (stake back + profit)
        lost -> balance stays ~stake BELOW pre_balance
    A virtual GW settles in a few minutes, so if the marker is already old
    the balance has finished moving and we can read it straight away.
    Returns True / False / None (undecidable)."""
    pre = ph.get("pre_balance")
    stake = float(ph.get("stake") or 0)
    odds = float(ph.get("odds") or 0)
    if pre is None or stake <= 0 or odds <= 1:
        return None
    win_level = pre + stake * (odds - 1) * 0.85   # allow for rounding/fees
    lost_level = pre - stake * 0.85

    try:
        age = (datetime.now() -
               datetime.strptime(ph["ts"], "%Y-%m-%d %H:%M:%S")).total_seconds()
    except Exception:
        age = 0
    # old marker => the GW is long finished; decide immediately
    deadline = time.time() + (0 if age > 600 else timeout_s)

    while True:
        bal = read_balance()
        if bal is not None:
            if bal >= win_level:
                log(f"  -> balance is NGN {bal - pre:,.0f} ABOVE the pre-bet "
                    f"figure — that bet WON.")
                return True
            if time.time() >= deadline and bal <= lost_level:
                log(f"  -> balance still NGN {pre - bal:,.0f} below pre-bet and "
                    f"the GW has finished — that bet LOST.")
                return False
        if time.time() >= deadline:
            break
        time.sleep(10)
    return None


def reconcile_unfinished(frame, st, cfg):
    ph = st.get("placing")
    if not ph:
        return
    log(f"Found unfinished bet marker from {ph.get('ts')} — reconciling with My Bets...")
    if ph.get("ticket_id"):
        row = find_ticket(frame, ticket_id=ph["ticket_id"], retries=4)
    elif ph.get("names"):
        # match the exact selection, not just stake, so we never confuse it
        row = confirm_placed(frame, ph["names"], ph.get("stake", 0), retries=4)
    else:
        row = find_ticket(frame, stake=ph.get("stake"), retries=4)
    if row is None and ph.get("pre_balance") is not None:
        # My Bets can come back EMPTY after a refresh/re-login, so "no ticket"
        # is not proof. Compare the balance against what it was before the
        # click: unchanged => never placed; short by ~the stake => it placed.
        bal = read_balance()
        if bal is not None:
            drop = ph["pre_balance"] - bal
            if drop >= ph.get("stake", 0) * 0.9:
                log(f"  -> My Bets empty, but balance is NGN {drop:,.0f} lower "
                    f"than before the click — that bet DID place. Settling it "
                    f"from the balance (no ticket id to watch).")
                won = settle_from_balance(ph)
                if won is None:
                    log("  -> could not tell win from loss on the balance — "
                        "leaving the streak untouched and moving on. "
                        "(Check My Bets if you want the exact result.)")
                    st.pop("placing", None)
                    save_state(st)
                    return
                log(f"  -> reconciled from balance: {'WON' if won else 'LOST'}")
                record_outcome(st, ph["stake"], ph.get("odds", 2.0), won)
                if won:
                    on_win(st)
                else:
                    on_loss(st, ph["stake"], cfg)
                st.pop("placing", None)
                save_state(st)
                return
            elif abs(drop) < 0.5:
                log("  -> balance unchanged since the click: the bet was never "
                    "placed. Safe to continue.")
                st.pop("placing", None)
                save_state(st)
                return
    if row is None:
        log("  -> no matching ticket: the bet was never placed. Safe to continue.")
    else:
        if not row["settled"]:
            if not row.get("id"):
                # no ticket id to watch — fall back to the balance
                won = settle_from_balance(ph)
                if won is None:
                    log("  -> cannot settle without a ticket id; leaving the "
                        "streak untouched and moving on.")
                    st.pop("placing", None)
                    save_state(st)
                    return
            else:
                log("  -> ticket exists, waiting for settlement...")
                won = wait_settlement(frame, row["id"], timeout_s=480)
                if won is None:
                    stop_program("could not settle the unfinished bet — check My "
                                 "Bets manually before restarting", st)
        else:
            won = (row["win"] or 0) > 0
        log(f"  -> reconciled: {'WON' if won else 'LOST'}")
        record_outcome(st, ph["stake"], ph.get("odds", 2.0), won)
        if won:
            on_win(st)
        else:
            on_loss(st, ph["stake"], cfg)
    st.pop("placing", None)
    save_state(st)


# ─────────────────────────────────────────────────────────────
#  Cycle handlers per bet type
# ─────────────────────────────────────────────────────────────

def football_cycle(frame, st, cfg):
    """One football bet (single / two / three), fully PAGE-DRIVEN: the live
    page supplies the next bettable GW + fixtures + O/U odds + countdown; the
    predictor supplies each fixture's call. All legs are O/U 2.5 in one GW, so
    the ticket settles from that GW's final scores."""
    league = (cfg.get("leagues") or "England").split(",")[0].strip().title()
    btype = cfg["bet_type"]
    pick = page_pick_ticket(frame, cfg, league, btype)
    if not pick:
        time.sleep(15)
        return False
    legs = pick["legs"]
    slip_target = pick["total_odds"]

    stake = next_stake(cfg, st, slip_target)
    if cfg.get("martingale") and stake > cfg["max_stake"]:
        stop_program(f"martingale needs NGN {stake} > cap {cfg['max_stake']} "
                     f"(cum loss NGN {st.get('cum_loss', 0)})", st)

    legs_desc = " + ".join(f"{l['home']}-{l['away']}[{l['call']}]" for l in legs)
    desc = f"{league} GW{pick['week']} {legs_desc}"
    prov = "  [prov.]" if pick.get("provisional") else ""
    log(f"PICK ({btype}){prov}: {league} GW{pick['week']} (starts in "
        f"{pick['countdown_s']}s)  {legs_desc}  @{slip_target}  ->  stake NGN {stake}"
        + (f"  (recovering NGN {st['cum_loss']})" if st.get("cum_loss") else ""))

    names = []
    for l in legs:
        names += [l["home"], l["away"]]

    # Build + place, RETRYING THIS SAME GAMEWEEK while the clock allows.
    # A failed click/stake used to forfeit the whole GW; on a martingale that
    # means climbing the ladder without ever getting the chance to reset.
    # Every retry is safe: we only get here when placement was *confirmed
    # not to have happened* (balance unchanged / site error / slip empty).
    time.sleep(random.uniform(3, 8))
    status = ticket = slip_odds = None
    for attempt in range(1, 4):
        left = gw_seconds_left(frame, league, pick["week"])
        floor = int(cfg.get("min_place_seconds", 12))
        if left is not None and left < floor:
            log(f"GW{pick['week']} kicks off in {left}s (floor {floor}s) — "
                f"too late to retry, moving to the next GW.")
            break
        if left is not None and left < 60:
            log(f"  hurrying — only {left}s left on GW{pick['week']}.")
            set_fast_mode(True)
        else:
            set_fast_mode(False)

        slip_odds = build_football_slip(
            frame, {"league": league, "week": pick["week"], "legs": legs}, cfg)
        if slip_odds is None:
            if attempt < 3:
                log(f"Slip build failed — retrying GW{pick['week']} "
                    f"(attempt {attempt + 1}/3, {left if left else '?'}s left).")
                time.sleep(3)
                continue
            break

        status, ticket = execute_bet(frame, st, cfg, stake, slip_odds, desc, names)
        if status in ("dry_run", "placed"):
            break
        if attempt < 3:
            log(f"Placement failed and the bet is confirmed NOT on — retrying "
                f"GW{pick['week']} (attempt {attempt + 1}/3).")
            clear_betslip(frame)
            time.sleep(3)

    set_fast_mode(False)   # sprint is over — never leak into the next cycle
    if status not in ("dry_run", "placed"):
        log(f"Could not get GW{pick['week']} on after 3 tries — skipping it. "
            f"(Ladder unchanged: no money was staked.)")
        time.sleep(15)
        return False

    # settle from the GW's final scores (all legs share the week)
    won = wait_page_result_legs(frame, league, pick["week"], legs,
                                pick["countdown_s"])
    if won is None and ticket:
        won = wait_settlement(frame, ticket, timeout_s=180)
    if status == "placed":
        st.pop("placing", None)
        save_state(st)
    if won is None:
        log("Result unknown after timeout — NOT counting this cycle "
            "(check My Bets if this was a live bet).")
        return False
    book_outcome(st, cfg, stake, slip_odds, won,
                 {"type": btype, "league": league, "week": pick["week"],
                  "legs": legs_desc})
    return True


def racing_cycle(frame, st, cfg):
    """Two flat PLACE bets (predicted 2nd and 3rd) on the next race."""
    pick = api_get(f"/api/bet_pick?type=racing_place"
                   f"&sport={urllib.parse.quote(cfg['racing_sport'])}"
                   f"&venue={urllib.parse.quote(cfg['racing_venue'])}")
    if not pick or not pick.get("picks"):
        log("No bettable race right now — retrying shortly...")
        time.sleep(15)
        return False
    if not pick.get("rank_learned"):
        log("NOTE: venue rank pattern not learned yet (<30 evaluated races) — "
            "using model ranks 2 & 3.")

    log(f"RACE #{pick['race_id']} {pick['venue']} (starts in {pick['countdown']}): "
        + " & ".join(f"{p['name']} PLACE" for p in pick["picks"]))

    last_key = f"race_{pick['race_id']}"
    if st.get("last_race") == last_key:
        time.sleep(10)
        return False  # already bet this race

    if not nav_to_racing_venue(frame, pick["sport"], pick["venue"]):
        log("Venue navigation failed — skipping this race.")
        time.sleep(20)
        return False

    stake = cfg["base_stake"]
    markets = [m for m in str(cfg.get("racing_market", "show")).split(",")
               if m in ("win", "place", "show")] or ["show"]
    placed_any = False
    tickets = []
    for p in pick["picks"]:
        for market in markets:
            clear_betslip(frame)
            time.sleep(random.uniform(1.0, 3.0))
            res = click_racing_odds(frame, pick["race_id"], p["name"], market)
            if not res or res.get("err"):
                log(f"Could not click {market.upper()} odds for {p['name']} "
                    f"({res}) — skipping.")
                continue
            time.sleep(random.uniform(0.8, 1.5))
            slip = read_betslip(frame)
            hint = {"win": p.get("win_odds"), "place": p.get("place_odds"),
                    "show": p.get("show_odds")}.get(market)
            v = verify_racing_slip(slip, p["name"], hint,
                                   cfg["odds_tolerance"], market)
            if isinstance(v, list):
                log(f"SLIP REJECTED for {p['name']} {market.upper()}: {'; '.join(v)}")
                clear_betslip(frame)
                continue
            status, ticket = execute_bet(
                frame, st, cfg, stake, v,
                f"{pick['venue']} #{pick['race_id']} {p['name']} {market.upper()}",
                [p["name"]])
            if status in ("dry_run", "placed"):
                tickets.append((p["name"], market, v, ticket, status))
                placed_any = True

    st["last_race"] = last_key
    save_state(st)
    if not placed_any:
        time.sleep(20)
        return False

    for name, market, odds, ticket, status in tickets:
        if status == "placed":
            won = wait_settlement(frame, ticket)
            if won is None:
                stop_program("racing bet did not settle — check My Bets manually", st)
            st.pop("placing", None)
            save_state(st)
        else:
            won = wait_racing_result(pick, name, market)
        if won is None:
            log(f"{name} {market.upper()}: result unknown — not counted.")
            continue
        book_outcome(st, cfg, stake, odds, won,
                     {"type": "racing", "market": market, "venue": pick["venue"],
                      "race_id": pick["race_id"], "runner": name})
    return True


def book_outcome(st, cfg, stake, odds, won, meta):
    record_outcome(st, stake, odds, won)
    append_ledger({"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   "dry_run": cfg["dry_run"], "stake": stake, "odds": odds,
                   "won": won, **meta})
    if won:
        on_win(st)
        log(f"WON +NGN {stake * (odds - 1):,.0f}  |  today NGN {day_profit(st):+,.0f}")
    else:
        on_loss(st, stake, cfg)
        if cfg.get("martingale"):
            log(f"LOST -NGN {stake:,.0f}  |  streak {st['mart_losses']} loss "
                f"NGN {st['cum_loss']:,.0f}  |  today NGN {day_profit(st):+,.0f}")
        else:
            log(f"LOST -NGN {stake:,.0f}  |  today NGN {day_profit(st):+,.0f}")
    save_state(st)


# ─────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────
#  PAGE-DRIVEN PICKING — the live page is the source of truth for
#  which GW is next, its fixtures, odds, countdown and results.
#  (The predictor app only supplies the model's call per fixture,
#  which never lags; its odds feed can trail the site by GWs.)
# ─────────────────────────────────────────────────────────────

def parse_league_page(text, league):
    """Parse the league Upcoming page (O/U tab) innerText into:
    now_week, now_scores {(home, away): (hs, as)}, and upcoming sections
    [{week, countdown_s, status, rows: [{home, away, ou: {label: odds}}]}]."""
    out = {"now_week": None, "now_scores": {}, "sections": []}
    if not text:
        return out

    m = re.search(rf"Week\s*(\d+)\s*-\s*{league}", text)
    if m:
        out["now_week"] = int(m.group(1))
    # live/final scores in the ranking block: CHE\n0 : 0\nSUN
    for sm in re.finditer(r"\b([A-Z]{2,4})\n(\d+)\s*:\s*(\d+)\n([A-Z]{2,4})\b", text):
        out["now_scores"][(sm.group(1), sm.group(4))] = (int(sm.group(2)),
                                                         int(sm.group(3)))

    # upcoming sections: "00:36\nFootball League: England Week 36\nWAITING"
    hdr = re.compile(
        rf"(\d{{1,2}}):(\d{{2}})\n+Football League:\s*{league}\s+Week\s*(\d+)\n+(WAITING|WATCH)?")
    headers = list(hdr.finditer(text))
    for i, h in enumerate(headers):
        start = h.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        body = text[start:end]
        countdown = int(h.group(1)) * 60 + int(h.group(2))
        rows = []
        row_re = re.compile(r"(\d+)\.\n[\s\S]{0,8}?([A-Z]{2,4})\n-\n([A-Z]{2,4})\n")
        row_ms = list(row_re.finditer(body))
        for j, rm in enumerate(row_ms):
            rstart = rm.end()
            rend = row_ms[j + 1].start() if j + 1 < len(row_ms) else len(body)
            cell = body[rstart:rend]
            ou = {}
            for om in re.finditer(r"(OV|UN)\s*(\d\.\d)\n(\d+\.\d{2})", cell):
                ou[("O" if om.group(1) == "OV" else "U") + om.group(2)] = \
                    float(om.group(3))
            x12 = re.findall(r"\b1\n(\d+\.\d{2})\nX\n(\d+\.\d{2})\n2\n(\d+\.\d{2})", cell)
            row = {"pos": int(rm.group(1)), "home": rm.group(2),
                   "away": rm.group(3), "ou": ou}
            if x12:
                row["h_odds"], row["d_odds"], row["a_odds"] = map(float, x12[0])
            rows.append(row)
        if rows:
            out["sections"].append({"week": int(h.group(3)),
                                    "countdown_s": countdown,
                                    "status": h.group(4) or "",
                                    "rows": rows})
    return out


def read_page_text(frame):
    try:
        return frame.evaluate("() => document.body.innerText || ''")
    except Exception:
        return ""


MIN_COUNTDOWN_S = 40  # below this the GW is too close to lock — don't start


def page_pick_single(frame, cfg, league):
    """Pick the best O/U 2.5 single from the NEXT bettable GW, using live
    page odds + the model's call for each fixture."""
    ready = False
    for attempt in (1, 2):
        if nav_to_league_upcoming(frame, league) and select_market_tab(frame, "OU"):
            ready = True
            break
        time.sleep(6)  # games app may still be settling — one retry
    if not ready:
        return None
    parsed = parse_league_page(read_page_text(frame), league)
    if not parsed["sections"]:
        log("Page parsed but no upcoming sections found.")
        return None

    section = None
    for s in parsed["sections"]:
        if s["countdown_s"] >= MIN_COUNTDOWN_S:
            section = s
            break
    if section is None:
        log(f"Next GW starts in <{MIN_COUNTDOWN_S}s — waiting for the one after.")
        return None

    best = None
    for row in section["rows"]:
        r = api_get(f"/api/model_call?league={league}&home={row['home']}&away={row['away']}")
        if not r or "over_2_5_pct" not in r:
            continue
        p = r["over_2_5_pct"]
        conf = max(p, 100 - p)
        call = "O2.5" if p > 50 else "U2.5"
        odds = row["ou"].get(call)
        if conf < 55 or not odds:
            continue
        if not (cfg["min_odds"] <= odds <= cfg["max_odds"]):
            continue
        if best is None or conf > best["conf"]:
            best = {"league": league, "week": section["week"],
                    "countdown_s": section["countdown_s"],
                    "home": row["home"], "away": row["away"],
                    "market": "Over/Under 2.5", "call": call,
                    "odds": odds, "conf": round(conf, 1)}
    if best is None:
        log(f"GW{section['week']}: no fixture passed the filters "
            f"(conf>=55, odds {cfg['min_odds']}-{cfg['max_odds']}).")
    return best


# Odds windows per bet type — same as the predictor's banker construction:
# single sits in the banker-single window, two/three match the banker tickets.
BANKER_WINDOWS = {"single": (1.60, 2.04, 1),
                  "two": (2.05, 2.90, 4),
                  "three": (3.05, 3.90, 4)}


def page_pick_ticket(frame, cfg, league, bet_type):
    """Take the EXACT banker games the predictor picked. The LIVE page gives
    the next bettable GW (+ its fixtures, live odds, countdown); the predictor
    returns that GW's banker single/two/three (same construction as the
    Bankers page). We locate those exact fixtures on the live page and bet
    them at live odds. Returns a pick dict or None."""
    # Every cycle starts at NORMAL speed. Fast mode is only for the final
    # sprint on a nearly-locked GW; letting it persist starved the page of
    # render time and broke the market-tab switch permanently.
    set_fast_mode(False)

    ready = False
    for _ in (1, 2):
        if nav_to_league_upcoming(frame, league) and select_market_tab(frame, "OU"):
            ready = True
            break
        time.sleep(6)
    if not ready:
        return None
    parsed = parse_league_page(read_page_text(frame), league)
    section = next((s for s in parsed["sections"]
                    if s["countdown_s"] >= MIN_COUNTDOWN_S), None)
    if section is None:
        if parsed["sections"]:
            log(f"Next GW starts in <{MIN_COUNTDOWN_S}s — waiting for the one after.")
        return None
    week = section["week"]

    # single_odds_min/max narrow the SINGLE's odds window; allowed_calls
    # restricts which calls a single may use (e.g. ["O2.5"] = Overs only)
    api_type = api_bet_type(bet_type)
    q = (f"/api/banker_pick?league={urllib.parse.quote(league)}"
         f"&week={week}&type={api_type}")
    if api_type == "single":
        minsig = int(cfg.get("require_signal_agreement", 0) or 0)
        # Goal-fest (upgraded) singles are priced cheaper, so they use their
        # own lower odds floor; plain singles keep single_odds_min.
        smin = (cfg.get("upgraded_odds_min", 1.50) if minsig > 0
                else cfg.get("single_odds_min", 1.60))
        q += f"&smin={smin}&smax={cfg.get('single_odds_max', 2.04)}"
        ac = [str(c).strip() for c in (cfg.get("allowed_calls") or []) if str(c).strip()]
        if ac:
            q += "&calls=" + urllib.parse.quote(",".join(ac))
        if minsig > 0:
            q += f"&minsig={minsig}"
    tk = api_get(q)
    if not tk or not tk.get("legs"):
        log(f"GW{week}: predictor has no {bet_type} banker for this GW yet.")
        return None
    if tk.get("provisional") and not cfg.get("allow_provisional", True):
        log(f"GW{week}: banker {bet_type} is provisional and provisional is off — skipping.")
        return None

    # map each banker leg onto the live fixture row; use LIVE odds
    row_by_pair = {(r["home"], r["away"]): r for r in section["rows"]}
    legs = []
    for l in tk["legs"]:
        row = row_by_pair.get((l["home"], l["away"]))
        if not row:
            log(f"GW{week}: banker leg {l['home']}-{l['away']} not on the live "
                f"page yet — skipping this GW.")
            return None
        if l["market"] == "Over/Under 2.5":
            odds = row["ou"].get(l["call"])
        else:  # 1X2 leg — live odds live on the Match-result tab; app's odds
               # are the generator's true 1X2 odds, stable enough to target
            odds = l.get("odds")
        if not odds:
            log(f"GW{week}: no live odds for {l['home']}-{l['away']} [{l['call']}] "
                f"— skipping this GW.")
            return None
        legs.append({"home": l["home"], "away": l["away"],
                     "market": l["market"], "call": l["call"],
                     "odds": round(odds, 2)})

    # optional: skip this GW if a leg's odds is one you don't want to play
    skip = {round(float(x), 2) for x in cfg.get("skip_odds", []) if x}
    hit = [l for l in legs if round(l["odds"], 2) in skip]
    if hit:
        log(f"GW{week}: skipped — leg odds {hit[0]['odds']} is in your skip list "
            f"{sorted(skip)}.")
        return None

    total = 1.0
    for l in legs:
        total *= l["odds"]
    return {"legs": legs, "total_odds": round(total, 2),
            "week": week, "countdown_s": section["countdown_s"],
            "provisional": tk.get("provisional", False)}


def wait_page_result_legs(frame, league, week, legs, countdown_s):
    """Settle a one-GW ticket (all legs share the week): wait until the GW
    has played, read every leg's final score, return all-win / lost / None."""
    deadline = time.time() + countdown_s + 300
    scores = {}
    while time.time() < deadline:
        parsed = parse_league_page(read_page_text(frame), league)
        nw = parsed["now_week"]
        if nw == week:
            for leg in legs:
                sc = (parsed["now_scores"].get((leg["home"], leg["away"]))
                      or parsed["now_scores"].get((leg["away"], leg["home"])))
                if sc:
                    scores[(leg["home"], leg["away"])] = sc
        if nw is not None and nw != week and len(scores) == len(legs):
            break
        time.sleep(random.uniform(8, 13))
    if len(scores) < len(legs):
        return None
    all_won = True
    parts = []
    for leg in legs:
        hs, as_ = scores[(leg["home"], leg["away"])]
        won = _leg_won(leg["call"], hs, as_)
        parts.append(f"{leg['home']} {hs}:{as_} {leg['away']}={'W' if won else 'L'}")
        all_won = all_won and won
    log("Final: " + "  ".join(parts) + f"  ->  {'WON' if all_won else 'LOST'}")
    return all_won


def _leg_won(call, hs, as_):
    tot = hs + as_
    if call == "O2.5":
        return tot > 2.5
    if call == "U2.5":
        return tot <= 2.5
    if call == "1":
        return hs > as_
    if call == "2":
        return as_ > hs
    if call == "GG":
        return hs > 0 and as_ > 0
    if call == "NG":
        return not (hs > 0 and as_ > 0)
    if call == "X":
        return hs == as_
    return False


def wait_page_result(frame, league, leg, timeout_s=None):
    """Watch the live page until the bet GW has PLAYED and read the final
    score straight from the ranking block. Returns won/lost/None."""
    if timeout_s is None:
        timeout_s = leg["countdown_s"] + 300
    deadline = time.time() + timeout_s
    last_score = None
    while time.time() < deadline:
        parsed = parse_league_page(read_page_text(frame), league)
        nw = parsed["now_week"]
        sc = (parsed["now_scores"].get((leg["home"], leg["away"]))
              or parsed["now_scores"].get((leg["away"], leg["home"])))
        if nw == leg["week"] and sc:
            last_score = sc
        if nw is not None and nw != leg["week"] and last_score:
            break  # our GW finished; last seen score is the final
        if nw is not None and nw != leg["week"] and last_score is None \
                and nw > leg["week"]:
            # missed the whole GW window (page hiccup) — try app results
            r = api_get(f"/api/bet_result?league={league}&week={leg['week']}"
                        f"&home={leg['home']}&away={leg['away']}&call={leg['call']}")
            if r and r.get("status") in ("won", "lost"):
                log(f"Result (via app): {r['status'].upper()} ({r.get('score')})")
                return r["status"] == "won"
        time.sleep(random.uniform(8, 13))
    if last_score is None:
        return None
    total = last_score[0] + last_score[1]
    won = _leg_won(leg["call"], last_score[0], last_score[1])
    log(f"Final score {leg['home']} {last_score[0]}:{last_score[1]} {leg['away']}"
        f" -> total {total} [{leg['call']}] -> {'WON' if won else 'LOST'}")
    return won


def sim_cycle(frame, st, cfg):
    """Betting brain against the LIVE PAGE, no bets placed — identical pick +
    settlement logic to live (single/two/three via page_pick_ticket), just no
    placement. Returns True if an outcome was booked."""
    league = (cfg.get("leagues") or "England").split(",")[0].strip().title()
    pick = page_pick_ticket(frame, cfg, league, cfg["bet_type"])
    if not pick:
        time.sleep(15)
        return False
    legs = pick["legs"]
    total = pick["total_odds"]

    stake = next_stake(cfg, st, total)
    if cfg.get("martingale") and stake > cfg["max_stake"]:
        stop_program(f"martingale needs NGN {stake} > cap {cfg['max_stake']} "
                     f"(cum loss NGN {st.get('cum_loss', 0)})", st)

    legs_desc = " + ".join(f"{l['home']}-{l['away']}[{l['call']}]" for l in legs)
    log(f"SIM PICK ({cfg['bet_type']}): {league} GW{pick['week']} "
        f"(starts in {pick['countdown_s']}s)  {legs_desc}  @{total}  "
        f"->  stake NGN {stake}"
        + (f"  (recovering NGN {st['cum_loss']})" if st.get("cum_loss") else ""))

    won = wait_page_result_legs(frame, league, pick["week"], legs,
                                pick["countdown_s"])
    if won is None:
        log("SIM: could not read this GW's result from the page — not counting.")
        return False

    record_outcome(st, stake, total, won)
    append_ledger({"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   "sim": True, "league": league, "week": pick["week"],
                   "legs": legs_desc, "odds": total, "stake": stake, "won": won})
    if won:
        on_win(st)
        log(f"SIM WON +NGN {stake * (total - 1):,.0f}  |  "
            f"today NGN {day_profit(st):+,.0f}")
    else:
        on_loss(st, stake, cfg)
        log(f"SIM LOST -NGN {stake:,.0f}  "
            + (f"|  streak {st['mart_losses']} loss NGN {st['cum_loss']:,.0f}  "
               if cfg.get("martingale") else "")
            + f"|  today NGN {day_profit(st):+,.0f}")
    save_state(st)
    return True


def sim_cycle_app(st, cfg):
    """Older app-fed sim, still used for multi-leg (two/three) tickets."""
    btype = api_bet_type(cfg["bet_type"])
    if btype not in ("single", "two", "three"):
        btype = "single"
    lg_q = urllib.parse.quote(cfg.get("leagues", ""))
    pick = None
    for min_wave in (1, 2, 3):
        cand = api_get(f"/api/bet_pick?type={btype}&min_wave={min_wave}&leagues={lg_q}")
        if cand and cand.get("legs"):
            total = cand["total_odds"]
            if cfg["min_odds"] <= total <= cfg["max_odds"]:
                pick = cand
                break
    if pick is None:
        log("SIM: no qualifying pick right now — retrying shortly...")
        time.sleep(20)
        return False

    total = pick["total_odds"]
    stake = next_stake(cfg, st, total)
    if cfg.get("martingale") and stake > cfg["max_stake"]:
        stop_program(f"martingale needs NGN {stake} > cap {cfg['max_stake']} "
                     f"(cum loss NGN {st.get('cum_loss', 0)})", st)

    legs_desc = " + ".join(f"{l['home']}-{l['away']}[{l['call']}]" for l in pick["legs"])
    log(f"SIM PICK ({btype}, wave {pick['wave']}): {pick['league']} GW{pick['week']}  "
        f"{legs_desc}  @{total}  ->  stake NGN {stake}"
        + (f"  (recovering NGN {st['cum_loss']})" if st.get("cum_loss") else ""))

    # settle against real scraped results (all legs must resolve)
    won = wait_all_legs(pick, timeout_s=600)
    if won is None:
        log("SIM: result unknown after timeout — not counting this cycle.")
        return False

    record_outcome(st, stake, total, won)
    append_ledger({"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   "sim": True, "league": pick["league"], "week": pick["week"],
                   "legs": legs_desc, "odds": total, "stake": stake, "won": won})
    if won:
        on_win(st)
        log(f"SIM WON +NGN {stake * (total - 1):,.0f}  |  today NGN {day_profit(st):+,.0f}")
    else:
        on_loss(st, stake, cfg)
        log(f"SIM LOST -NGN {stake:,.0f}  "
            + (f"|  streak {st['mart_losses']} loss NGN {st['cum_loss']:,.0f}  "
               if cfg.get("martingale") else "")
            + f"|  today NGN {day_profit(st):+,.0f}")
    save_state(st)
    return True


def wait_all_legs(pick, timeout_s=600):
    """Real settlement for a (multi-leg) football pick via the app's result
    feed. Win only if every leg wins."""
    deadline = time.time() + timeout_s
    pending = list(pick["legs"])
    results = {}
    while time.time() < deadline and pending:
        still = []
        for leg in pending:
            q = (f"/api/bet_result?league={pick['league']}&week={pick['week']}"
                 f"&home={leg['home']}&away={leg['away']}&call={leg['call']}")
            r = api_get(q)
            if r and r.get("status") in ("won", "lost"):
                results[(leg['home'], leg['away'])] = r["status"] == "won"
                log(f"  leg {leg['home']}-{leg['away']} [{leg['call']}]: "
                    f"{r['status'].upper()} ({r.get('score')})")
            else:
                still.append(leg)
        pending = still
        if pending:
            time.sleep(12)
    if pending:
        return None
    return all(results.values())


def run_sim_loop(frame, ctx, page, st, cfg):
    log("SIMULATION MODE — reads the LIVE page (no login, no bets). "
        "Validating pick + martingale + settlement in real time.")
    chasing = cfg.get("martingale") and st.get("cum_loss", 0) > 0
    was_waiting = None
    while True:
        rails_check(cfg, st)
        inside = in_window(cfg)
        if not inside and not chasing:
            if was_waiting is not True:
                nxt = next_window_start(cfg)
                log(f"Outside betting windows — idle until {nxt or 'next window'}.")
                was_waiting = True
            time.sleep(20)
            continue
        was_waiting = False

        try:
            frame.evaluate("() => 1")
        except Exception:
            log("Browser frame lost — reconnecting...")
            frame = goto_virtuals(ctx, page)
            if frame is None:
                time.sleep(30)
                continue

        booked = sim_cycle(frame, st, cfg)   # page-driven for all bet types
        if booked:
            chasing = cfg.get("martingale") and st.get("cum_loss", 0) > 0
            rails_check(cfg, st)
            time.sleep(random.uniform(6, 15))


def main():
    cfg = load_config()
    cfg = startup_prompts(cfg)
    set_state_paths(cfg)   # real vs practice streak/ledger are kept separate
    st = load_state()
    st.setdefault("cum_loss", 0)
    st.setdefault("mart_losses", 0)
    if not cfg.get("martingale"):
        st["cum_loss"] = 0  # flat mode never carries a recovery target
        st["mart_losses"] = 0

    # ── Resume-or-start-fresh — ALWAYS asked up front, BEFORE anything is
    # settled. An unsettled bet can turn into a recovery stake once it is
    # reconciled, so you must consent to that before we go looking for it.
    cfg["_resume"] = True
    if cfg.get("martingale"):
        carried = st.get("cum_loss", 0) or 0
        pending = st.get("placing")
        mode = "practice" if (cfg.get("sim_mode") or cfg.get("dry_run")) else "REAL-MONEY"
        if carried > 0 or pending:
            print("")
            print("  " + "-" * 64)
            print(f"  A previous {mode} run left something behind:")
            if carried > 0:
                print(f"    • martingale ladder: {st.get('mart_losses', 0)} losses, "
                      f"NGN {carried:,.0f} still to recover")
            if pending:
                print(f"    • one bet from {pending.get('ts')} was never settled")
                print(f"      ({pending.get('desc', '?')}, stake NGN "
                      f"{float(pending.get('stake') or 0):,.0f})")
                print(f"      -> if it LOST, resuming makes your NEXT stake a "
                      f"recovery stake.")
            print("  " + "-" * 64)
            cfg["_resume"] = _ask_bool(
                "Resume that run? (y = settle the unfinished bet and continue "
                "the ladder / n = forget it and start fresh at base stake)", True)
            if not cfg["_resume"]:
                st.pop("placing", None)
                on_win(st)          # zero the ladder
                save_state(st)
                log("Starting FRESH — ladder reset to base stake; the unsettled "
                    "bet is left alone (it still stands in your Sportybet "
                    "account, we just won't chase it).")
            else:
                log("Resuming — the unfinished bet will be settled first, then "
                    "the ladder continues from there.")
        else:
            print("  (no carried streak or unfinished bet — starting at base stake)")

    if cfg.get("sim_mode"):
        print("=" * 62)
        print("  SPORTYBET AUTO-BETTOR   [SIMULATION — live page, no bets]")
        print(f"  bet type: {cfg['bet_type']}  |  martingale: "
              f"{'ON' if cfg.get('martingale') else 'OFF'}  |  base NGN {cfg['base_stake']}")
        print(f"  windows: {cfg['betting_windows']}")
        print("=" * 62)
        if api_get("/api/status") is None:
            stop_program("predictor app is not running at 127.0.0.1:5000 — "
                         "start it first (python app.py)")
        pw = ctx = page = None
        try:
            pw, ctx, page = launch_browser()
            set_browser_ctx(ctx)
            try:
                page.goto("https://www.sportybet.com/ng/virtual/", timeout=60000,
                          wait_until="domcontentloaded")
            except Exception:
                pass
            frame = goto_virtuals(ctx, page)
            if frame is None:
                stop_program("could not open the virtual games")
            run_sim_loop(frame, ctx, page, st, cfg)
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
        return

    print("=" * 62)
    print("  SPORTYBET AUTO-BETTOR"
          + ("   [DRY RUN — no real bets]" if cfg["dry_run"] else "   [LIVE MONEY]"))
    print(f"  bet type: {cfg['bet_type']}"
          + (f" ({cfg['racing_sport']} — {cfg['racing_venue']})"
             if cfg["bet_type"] == "racing_place"
             else f" ({BET_TYPE_PRESETS.get(cfg['bet_type'], {}).get('desc', '')})")
          + f"  |  martingale: {'ON' if cfg.get('martingale') else 'OFF (flat stakes)'}")
    print(f"  base stake NGN {cfg['base_stake']}"
          + (f"  |  max stake NGN {cfg['max_stake']}" if cfg.get("martingale") else ""))
    print(f"  daily: profit target +{cfg['daily_profit_target']}  "
          f"loss limit -{cfg['daily_loss_limit']}")
    print(f"  windows: {cfg['betting_windows']}"
          + ("   (streaks chased past window end until a WIN)"
             if cfg.get("martingale") else ""))
    print("=" * 62)

    if api_get("/api/status") is None:
        stop_program("predictor app is not running at 127.0.0.1:5000 — "
                     "start it first (python app.py)")
    rails_check(cfg, st)

    pw = ctx = page = None
    try:
        pw, ctx, page = launch_browser()
        set_browser_ctx(ctx)
        try:
            page.goto("https://www.sportybet.com/ng/", timeout=60000,
                      wait_until="domcontentloaded")
        except Exception:
            pass
        if cfg["dry_run"]:
            log("(dry run still waits for login so the flow matches live mode)")
        wait_for_login(ctx, page)
        frame = goto_virtuals(ctx, page)
        if frame is None:
            stop_program("could not open the virtual games after 5 attempts "
                         "(session rejected repeatedly) — try again in a few minutes")
        # only look for / settle an old bet if you said yes at startup
        if not cfg["dry_run"] and cfg.get("_resume", True):
            reconcile_unfinished(frame, st, cfg)
            if st.get("cum_loss", 0) > 0:
                nxt = next_stake(cfg, st, 1.65)
                log(f"After settling, the ladder stands at NGN "
                    f"{st['cum_loss']:,.0f} to recover — next stake will be "
                    f"about NGN {nxt:,.0f}.")

        chasing = cfg.get("martingale") and st.get("cum_loss", 0) > 0
        was_waiting = None
        dead_cycles = 0
        while True:
            rails_check(cfg, st)

            inside = in_window(cfg)
            if not inside and not chasing:
                if was_waiting is not True:
                    nxt = next_window_start(cfg)
                    log(f"Outside betting windows — waiting for the next window"
                        f"{' (starts ' + nxt + ')' if nxt else ''}. "
                        f"The bot is fine, just idle.")
                    was_waiting = True
                time.sleep(20)
                continue
            if was_waiting is not False:
                log("Betting window is OPEN — looking for picks.")
                was_waiting = False

            # frame health only matters when we're about to bet
            try:
                frame.evaluate("() => 1")
            except Exception:
                log("Browser frame lost — reconnecting...")
                frame = goto_virtuals(ctx, page)
                if frame is None:
                    frame = hard_reload(ctx, page)
                if frame is None:
                    time.sleep(30)
                    continue

            # logged out mid-run (a bad reload can drop the session)
            if logged_out(ctx):
                wait_for_login(ctx, page)
                frame = goto_virtuals(ctx, page) or hard_reload(ctx, page)
                if frame is None:
                    time.sleep(20)
                    continue

            if not inside and chasing:
                log("Past window end but on a losing streak — chasing until the next WIN.")

            if cfg["bet_type"] == "racing_place":
                booked = racing_cycle(frame, st, cfg)
            else:
                booked = football_cycle(frame, st, cfg)

            if booked:
                dead_cycles = 0
                chasing = cfg.get("martingale") and st.get("cum_loss", 0) > 0
                if not chasing and not in_window(cfg):
                    log("Outside betting window — waiting for the next window.")
                rails_check(cfg, st)
                time.sleep(random.uniform(6, 18))
            else:
                # Nothing bet this cycle. A couple of these is normal (no
                # qualifying pick); many in a row means the PAGE is broken —
                # that is what silently deepens the martingale, so escalate.
                dead_cycles += 1
                if dead_cycles in (3, 6, 10) or dead_cycles % 10 == 0:
                    log(f"{dead_cycles} cycles without a bet — page may be stuck; "
                        f"forcing a full refresh (this is what bleeds money when "
                        f"a winning GW gets skipped).")
                    fr = hard_reload(ctx, page)
                    if fr:
                        frame = fr
                        if dead_cycles >= 6:
                            dead_cycles = 0
                    else:
                        log("Hard refresh did not restore the games — waiting 60s.")
                        time.sleep(60)

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


if __name__ == "__main__":
    main()
