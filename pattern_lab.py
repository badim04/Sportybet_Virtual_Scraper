"""
Pattern Lab — regime-aware short-window pattern engine (separate module,
mirrors racing). Implements the framework from the ChatGPT + DeepSeek notes:

  ChatGPT:  normalize into ONE dataset · sequence mining · change-point /
            regime detection · separate discovery from validation
            (walk-forward) · track each pattern ACTIVE / WEAK / DEAD.
  DeepSeek: never look back more than ~10 matches (short rolling window) ·
            Markov streak / state-transition · CUSUM change-points ·
            ride the continuation OR fade the reversal · cycle by cycle.

Design honesty (learned the hard way in this project): a pattern is scored
CYCLE BY CYCLE and STANDALONE — a cycle is born when a short window turns hot,
lives while it stays hot, dies when it cools; each cycle's P/L is judged on its
own and never pooled with others. Every pattern reports BOTH its cycle-win%
(the "feeling") AND its net units + walk-forward test (the "ledger"), so a
"59% of cycles green / net negative" pattern is visible for what it is.

This is a research + live-read instrument, not an edge generator.
"""
import os
import json
import glob
import time
import threading
from collections import defaultdict
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
ACCURACY_DIR = os.path.join(_HERE, "data", "accuracy")

LEAGUES = ["england", "france", "germany", "italy", "spain", "turkey"]
WINDOW = 10          # DeepSeek: never analyse more than ~last 10
MIN_CYCLES = 15      # need a few cycles before a pattern is worth showing

# ── outcome + live-odds keys for each market call ──────────────────────────
#   feat = the boolean feature we streak on;  opp = the fade (reversal) call
_CALLS = {
    "O":  ("over_25_odds",  "U"),
    "U":  ("under_25_odds", "O"),
    "GG": ("gg_odds",       "NG"),
    "NG": ("ng_odds",       "GG"),
    "H":  ("home_odds",     None),
    "A":  ("away_odds",     None),
}


def _outcome(r):
    hs, aws = r["actual_home"], r["actual_away"]
    tot = hs + aws
    return {"O": tot > 2.5, "U": tot <= 2.5,
            "GG": hs > 0 and aws > 0, "NG": not (hs > 0 and aws > 0),
            "H": hs > aws, "A": aws > hs, "D": hs == aws}


_STREAM_CACHE = {"t": 0, "data": None}


def load_streams(max_age=45):
    """One normalized, chronological stream per league (ChatGPT step 7).
    Cached briefly so page loads don't re-read the logs every time."""
    now = time.time()
    if _STREAM_CACHE["data"] is not None and now - _STREAM_CACHE["t"] < max_age:
        return _STREAM_CACHE["data"]
    streams = {}
    for lg in LEAGUES:
        path = os.path.join(ACCURACY_DIR, f"eval_log_{lg}.jsonl")
        rows = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if r.get("actual_home") is None or r.get("actual_away") is None:
                        continue
                    try:
                        r["_t"] = datetime.strptime(r["predicted_at"], "%Y-%m-%d %H:%M")
                    except Exception:
                        continue
                    r["_o"] = _outcome(r)
                    rows.append(r)
        except FileNotFoundError:
            pass
        rows.sort(key=lambda r: r["_t"])
        streams[lg] = rows
    _STREAM_CACHE.update(t=now, data=streams)
    return streams


def _pattern_library():
    """Every short-window pattern we track. Both directions:
    CONTINUATION (ride the hot streak) and REVERSAL (fade it)."""
    lib = []
    for feat in ("O", "U", "GG", "NG"):
        okey_cont, opp = _CALLS[feat]
        okey_rev = _CALLS[opp][0]
        for hot in (6, 7, 8):
            lib.append({"id": f"ride_{feat}_{hot}", "kind": "ride",
                        "label": f"Ride {feat} · {hot}+/{WINDOW}",
                        "feat": feat, "call": feat, "okey": okey_cont, "hot": hot})
            lib.append({"id": f"fade_{feat}_{hot}", "kind": "fade",
                        "label": f"Fade {feat} · {hot}+/{WINDOW}",
                        "feat": feat, "call": opp, "okey": okey_rev, "hot": hot})
    return lib


def _prefix(stream, feat):
    """Prefix sums of a boolean feature so any window count is O(1):
    count in matches [i-WINDOW, i) == pf[i] - pf[i-WINDOW]."""
    pf = [0] * (len(stream) + 1)
    for i, r in enumerate(stream):
        pf[i + 1] = pf[i] + (1 if r["_o"][feat] else 0)
    return pf


def _cycles(stream, feat, call, okey, hot, pf=None):
    """Split a stream into STANDALONE cycles. A cycle opens when the last
    WINDOW matches hold >= hot of `feat`, collects each subsequent bet on
    `call` at its live odds, and closes when the window cools (< hot-1).
    Returns list of cycles; each cycle is a list of (won, odds, datetime).
    `pf` is a precomputed prefix-sum array for `feat` (see _prefix)."""
    if pf is None:
        pf = _prefix(stream, feat)
    cyc, cur = [], None
    n = len(stream)
    for i in range(WINDOW, n):
        rate = pf[i] - pf[i - WINDOW]
        if cur is None and rate >= hot:
            cur = []
        if cur is not None:
            r = stream[i]
            o = r.get(okey)
            if o and o > 1:
                cur.append((r["_o"][call], o, r["_t"]))
            if rate < hot - 1:
                if cur:
                    cyc.append(cur)
                cur = None
    if cur:
        cyc.append(cur)
    return cyc


def _net(bets):
    return sum((o - 1) if won else -1 for won, o, _ in bets)


def _cusum_changepoints(seq, thresh=4.0):
    """Simple two-sided CUSUM on a 0/1 sequence vs its own mean — flags the
    regime shifts both docs ask for. Returns indices where drift resets."""
    if not seq:
        return []
    mu = sum(seq) / len(seq)
    sp = sn = 0.0
    cps = []
    for i, x in enumerate(seq):
        sp = max(0, sp + (x - mu) - 0.05)
        sn = min(0, sn + (x - mu) + 0.05)
        if sp > thresh or sn < -thresh:
            cps.append(i)
            sp = sn = 0.0
    return cps


def score_pattern(stream, pat, pf=None):
    """Full standalone-cycle scorecard for one pattern on one league stream."""
    cyc = [c for c in _cycles(stream, pat["feat"], pat["call"],
                              pat["okey"], pat["hot"], pf) if c]
    if len(cyc) < MIN_CYCLES:
        return None
    all_bets = [b for c in cyc for b in c]
    n_bets = len(all_bets)
    if n_bets < 30:
        return None
    green = sum(1 for c in cyc if _net(c) > 0)
    net = _net(all_bets)
    wins = sum(1 for won, _, _ in all_bets if won)

    # split-half of the bet stream (does it persist?)
    h = n_bets // 2
    net_h1, net_h2 = _net(all_bets[:h]), _net(all_bets[h:])

    # walk-forward: is the last 30% (out of sample) still alive?
    k = int(n_bets * 0.7)
    net_oos = _net(all_bets[k:]) if n_bets - k >= 20 else None

    # ACTIVE / WEAK / DEAD from the most recent cycles (ChatGPT's monitor)
    recent = cyc[-6:]
    rnet = _net([b for c in recent for b in c])
    if rnet > 0.5:
        status = "ACTIVE"
    elif rnet > -2.0:
        status = "WEAK"
    else:
        status = "DEAD"

    return {
        "id": pat["id"], "label": pat["label"], "kind": pat["kind"],
        "cycles": len(cyc), "pct_green": round(green / len(cyc) * 100),
        "bets": n_bets, "win_pct": round(wins / n_bets * 100, 1),
        "net": round(net, 1),
        "roi": round(net / n_bets * 100, 1),
        "net_h1": round(net_h1, 1), "net_h2": round(net_h2, 1),
        "net_oos": (round(net_oos, 1) if net_oos is not None else None),
        "persists": (net_h1 > 0 and net_h2 > 0),
        "status": status,
    }


def _current_cycle(stream, pat, pf=None):
    """Is this pattern in an OPEN cycle right now (at the tail of the stream)?
    Returns the running cycle P/L, length, and whether it is cooling — the
    live 'ride board' the user wants."""
    n = len(stream)
    if n <= WINDOW:
        return None
    if pf is None:
        pf = _prefix(stream, pat["feat"])
    cur, open_since = None, None
    for i in range(WINDOW, n):
        rate = pf[i] - pf[i - WINDOW]
        if cur is None and rate >= pat["hot"]:
            cur, open_since = [], stream[i]["_t"]
        if cur is not None:
            r = stream[i]
            o = r.get(pat["okey"])
            if o and o > 1:
                cur.append((r["_o"][pat["call"]], o, r["_t"]))
            if rate < pat["hot"] - 1:
                cur, open_since = None, None
    if cur is None or not cur:
        return None
    last_rate = pf[n] - pf[n - WINDOW]
    return {
        "id": pat["id"], "label": pat["label"], "kind": pat["kind"],
        "call": pat["call"], "since": open_since.strftime("%m-%d %H:%M"),
        "len": len(cur), "running": round(_net(cur), 1),
        "window_rate": f"{last_rate}/{WINDOW}",
        "cooling": last_rate < pat["hot"],
    }


# ═══════════════════════════════════════════════════════════════════════
#  ENGINE PREDICTIONS — a concrete "call for the NEXT match" per league,
#  one from each source doc. They have different personalities on purpose:
#    DeepSeek = aggressive; always fires the strongest last-10 streak signal
#               (fade a 3+ run, else ride a 7+/10 skew), with a confidence and
#               its own STRONG filter (conf>75 AND odds>2.0).
#    ChatGPT  = disciplined; fires ONLY when a walk-forward-validated pattern
#               is currently ACTIVE (positive OOS), else says HOLD.
#  Each pick is shown beside the engine's own honest forward track record.
# ═══════════════════════════════════════════════════════════════════════

_ENGINE_OKEY = {"O": "over_25_odds", "U": "under_25_odds",
                "GG": "gg_odds", "NG": "ng_odds"}
_OPP = {"O": "U", "U": "O", "GG": "NG", "NG": "GG"}


def _deepseek_decide(runs, pf, i, n, recent_calls=(), recent_results=()):
    """DeepSeek's call at match index i, thinking like a punter, not a
    calculator. Short memory only (last {WINDOW}). Uses ONLY results before i.
      · a SMALL streak (2-3) is hot  → stay on it (ride)
      · a LONG streak (5+) is tired  → it's due to break (fade)
      · 4 is the turn — lean to the break
      · no streak but the last 10 are very one-way (8+/10) → bet the turn
      · a call that's been absent lately is "overdue" → small nudge
      · don't keep hammering the SAME call (a human gets wary)
      · if the read's gone cold (last 3 picks lost) → flip the stance
    Returns (conf, call, reason, kind) or None.
    """
    # strongest current tail streak
    feat = max(("O", "U", "GG", "NG"), key=lambda f: runs[f])
    slen = runs[feat]
    call = conf = reason = kind = None

    if slen >= 5:
        call, kind = _OPP[feat], "fade"
        conf = min(92, 60 + slen * 5)
        reason = f"{slen} {feat} on the bounce — tired, due to break → {call}"
    elif slen in (2, 3):
        call, kind = feat, "ride"
        conf = 52 + slen * 6
        reason = f"{slen} {feat} in a row — hot, stay on → {call}"
    elif slen == 4:
        call, kind = _OPP[feat], "fade"
        conf = 62
        reason = f"4 {feat} in a row — leaning the break → {call}"

    # no streak: only act if the last 10 are strongly one-way
    if call is None and i >= WINDOW:
        for f in ("O", "U", "GG", "NG"):
            cnt = pf[f][i] - pf[f][i - WINDOW]
            if cnt >= 8:
                call, kind = _OPP[f], "fade"
                conf = 56 + (cnt - 8) * 6
                reason = f"{cnt}/10 {f} lately — one-way, betting the turn → {call}"
                break
    if call is None:
        return None

    # overdue nudge: the call has barely shown up in the last 10
    if i >= WINDOW and (pf[call][i] - pf[call][i - WINDOW]) <= 2:
        conf = min(95, conf + 6)
        reason += " · overdue"

    # anti-robot: a punter won't keep backing the identical call forever
    if len(recent_calls) >= 3 and all(c == call for c in recent_calls[-3:]):
        conf = max(45, conf - 15)
        reason += " · eased (same call running)"

    # cold switch: if the last 3 actual picks all lost, this read is backwards
    if len(recent_results) >= 3 and not any(recent_results[-3:]):
        call = _OPP[call]
        kind = "fade" if kind == "ride" else "ride"
        conf = max(48, conf - 8)
        reason = f"read's been cold — flipping to → {call}"

    return (int(conf), call, reason, kind)


def _deepseek_engine(stream, pf):
    """Run DeepSeek's rule causally across history (track record) and emit the
    LIVE pick for the next match (applying the rule at the tail)."""
    n = len(stream)
    runs = {"O": 0, "U": 0, "GG": 0, "NG": 0}
    bets = []              # (won, odds, conf)
    recent_calls = []      # the engine's own last few picks (for anti-robot)
    recent_results = []    # the engine's own last few win/loss (for cold-switch)
    for i in range(n):
        d = _deepseek_decide(runs, pf, i, n, recent_calls, recent_results)
        if d:
            conf, call, _, _ = d
            o = stream[i].get(_ENGINE_OKEY[call])
            if o and o > 1:
                won = stream[i]["_o"][call]
                bets.append((won, o, conf))
                recent_calls.append(call)
                recent_results.append(won)
                recent_calls = recent_calls[-5:]
                recent_results = recent_results[-5:]
        for f in runs:
            runs[f] = runs[f] + 1 if stream[i]["_o"][f] else 0
    live = _deepseek_decide(runs, pf, n, n, recent_calls, recent_results)

    def rec(sub):
        if not sub:
            return None
        nn = len(sub)
        w = sum(1 for won, _o in sub if won)
        net = sum((_o - 1) if won else -1 for won, _o in sub)
        return {"n": nn, "win_pct": round(w / nn * 100, 1), "net": round(net, 1),
                "roi": round(net / nn * 100, 1)}

    allb = [(w, o) for w, o, _c in bets]
    # DeepSeek's STRONG filter: confidence > 75 AND odds > 2.0
    strong = [(w, o) for w, o, c in bets if c > 75 and o > 2.0]
    out = {"track_all": rec(allb), "track_strong": rec(strong)}
    if live:
        conf, call, reason, kind = live
        out["pick"] = {"call": call, "conf": conf, "reason": reason,
                       "kind": kind, "strong": conf > 75}
    else:
        out["pick"] = None
    return out


def _chatgpt_engine(scored, live):
    """ChatGPT's rule: only act on a walk-forward-validated, currently-ACTIVE
    pattern. Among patterns with an OPEN cycle right now, require status ACTIVE
    and positive out-of-sample net; pick the strongest. Else HOLD."""
    by_id = {s["id"]: s for s in scored}
    cands = []
    for c in live:
        s = by_id.get(c["id"])
        if s and s["status"] == "ACTIVE" and (s["net_oos"] or -1) > 0 and s["persists"]:
            cands.append((s["net_oos"], c, s))
    if not cands:
        # softer fallback: an active pattern with a positive OOS even if it
        # didn't clear both-half persistence — flagged as provisional
        for c in live:
            s = by_id.get(c["id"])
            if s and s["status"] == "ACTIVE" and (s["net_oos"] or -1) > 0:
                cands.append((s["net_oos"] - 100, c, s))   # rank below validated
    if not cands:
        return {"pick": None,
                "reason": "no walk-forward-validated pattern is active — HOLD"}
    cands.sort(key=lambda x: -x[0])
    _, c, s = cands[0]
    return {"pick": {"call": c["call"], "label": c["label"], "kind": c["kind"],
                     "running": c["running"], "window_rate": c["window_rate"],
                     "cooling": c["cooling"],
                     "provisional": not s["persists"]},
            "track": {"net": s["net"], "net_oos": s["net_oos"],
                      "win_pct": s["win_pct"], "status": s["status"],
                      "pct_green": s["pct_green"]},
            "reason": "validated & active" if s["persists"] else "active, not both-half validated"}


# ═══════════════════════════════════════════════════════════════════════
#  BET PICKS — the actionable layer. Ties the DeepSeek short-memory read to
#  the LIVE upcoming fixtures so every prediction is a concrete MATCH +
#  SELECTION + ODDS you can stake. Confidence is a blend (model probability +
#  the streak read + the price's value); the DeepSeek read is surfaced as the
#  human reason. Direction is whichever side the blend favours (never forced
#  against the model). One best selection per match; ranked into a headline
#  Confident Call + a global Top-N + per-league groups.
# ═══════════════════════════════════════════════════════════════════════

def _tail_runs(stream):
    """Current tail streak length per outcome (how many in a row, right now)."""
    runs = {}
    for k in ("O", "U", "GG", "NG", "H", "D", "A"):
        c = 0
        for r in reversed(stream):
            if r["_o"][k]:
                c += 1
            else:
                break
        runs[k] = c
    return runs


def _win_counts(stream):
    """Counts in the last WINDOW matches (short memory)."""
    w = stream[-WINDOW:] if len(stream) >= WINDOW else stream
    c = {k: 0 for k in ("O", "U", "GG", "NG", "H", "D", "A")}
    for r in w:
        for k in c:
            if r["_o"][k]:
                c[k] += 1
    return c


def _pair_lean(runs, cnt, a, b):
    """Human read for a complementary pair (O/U or GG/NG): ride a small streak,
    fade a long one, else lean the lopsided last-10. Returns (call, strength,
    note)."""
    for x, y in ((a, b), (b, a)):
        if runs[x] >= 5:
            return (y, min(90, 60 + runs[x] * 5),
                    f"{runs[x]} {x} on the bounce — due a turn")
    for x, y in ((a, b), (b, a)):
        if runs[x] in (2, 3):
            return (x, 52 + runs[x] * 6, f"{runs[x]} {x} in a row — hot, stay on")
    if cnt[a] >= 8:
        return (b, 56 + (cnt[a] - 8) * 6, f"{cnt[a]}/10 {a} lately — betting the turn")
    if cnt[b] >= 8:
        return (a, 56 + (cnt[b] - 8) * 6, f"{cnt[b]}/10 {b} lately — betting the turn")
    if cnt[a] - cnt[b] >= 3:
        return (a, 52, f"{cnt[a]}/10 {a} lately — leaning {a}")
    if cnt[b] - cnt[a] >= 3:
        return (b, 52, f"{cnt[b]}/10 {b} lately — leaning {b}")
    return (None, 0, "no strong run")


def _res_lean(runs):
    """Human read for match result (only rides a hot side; a long run is a
    caution, not a fade — fading a 3-way is ambiguous)."""
    for x in ("H", "A"):
        if runs[x] in (2, 3):
            return (x, 52 + runs[x] * 6, f"{runs[x]} {x} on the trot — hot")
        if runs[x] >= 4:
            return (None, 0, f"{runs[x]} {x} in a row — could turn, wary")
    return (None, 0, "no strong run")


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _league_leans(stream):
    """The three family reads for a league, from its short memory."""
    runs, cnt = _tail_runs(stream), _win_counts(stream)
    return {
        "OU": _pair_lean(runs, cnt, "O", "U"),
        "GGNG": _pair_lean(runs, cnt, "GG", "NG"),
        "RES": _res_lean(runs),
    }


# selection code -> (display, model-prob getter, live-odds key, family, opp)
def _candidates(f, leans):
    """Every bettable selection for one upcoming fixture, each scored by the
    blend. Returns list of pick dicts (best-per-fixture chosen by caller)."""
    hw = f.get("home_win_pct") or 0
    dw = f.get("draw_pct") or 0
    aw = f.get("away_win_pct") or 0
    ov = f.get("over_2_5_pct")
    bt = f.get("btts_pct")
    # spec = (display, model%, odds, family, code, opp, odd_name, tab, cells, placeable)
    # odd_name/tab/cells are exactly what the auto-bettor clicks. `placeable`
    # marks markets the bettor can reliably place on the live page today:
    # O/U 2.5 (O/U tab, 8 cells) and 1X2 Home/Away (MAIN tab, 3 cells) — both
    # proven. GG/NG + Double Chance are shown for MANUAL betting but their live
    # cells don't render reliably for automation (verified 2026-08-24), so they
    # are NOT auto-placeable yet (the bettor skips them rather than misbet).
    specs = []
    if ov is not None:
        specs.append(("Over 2.5", ov, f.get("live_over_25"), "OU", "O", "U", "OV 2.5", "OU", 8, True))
        specs.append(("Under 2.5", 100 - ov, f.get("live_under_25"), "OU", "U", "O", "UN 2.5", "OU", 8, True))
    specs.append(("Home win", hw, f.get("live_home_odds"), "RES", "H", None, "1", "1X2", 3, True))
    specs.append(("Away win", aw, f.get("live_away_odds"), "RES", "A", None, "2", "1X2", 3, True))
    # GG/NG (Others > Goal Goal/No Goal, 2 cells) and Double Chance (MAIN >
    # Double Chance, 3 cells) each have a DEDICATED live grid, so they ARE
    # auto-placeable (verified 2026-08-24).
    if bt is not None:
        specs.append(("GG (both score)", bt, f.get("live_gg_odds"), "GGNG", "GG", "NG", "GG", "GGNG", 2, True))
        specs.append(("NG (no-goal)", 100 - bt, f.get("live_ng_odds"), "GGNG", "NG", "GG", "NG", "GGNG", 2, True))
    specs.append(("1X (dbl chance)", hw + dw, f.get("live_dc_1x_odds"), "RES", "H", None, "1X", "DC", 3, True))
    specs.append(("X2 (dbl chance)", aw + dw, f.get("live_dc_x2_odds"), "RES", "A", None, "X2", "DC", 3, True))
    specs.append(("12 (dbl chance)", hw + aw, f.get("live_dc_12_odds"), "RES", None, None, "12", "DC", 3, True))

    out = []
    for disp, prob, odds, fam, code, opp, odd_name, tab, cells, placeable in specs:
        if not odds or odds <= 1 or prob is None:
            continue
        implied = 100.0 / odds
        value = prob - implied
        lcall, lstr, lnote = leans[fam]
        agree = 0.0
        agreed = None
        if code and lcall == code:
            agree = min(10.0, lstr / 9.0)
            agreed = True
        elif code and lcall is not None and lcall == opp:
            agree = -8.0
            agreed = False
        conf = _clamp(prob + agree + _clamp(value * 0.4, -8, 8), 1, 97)
        # human reason
        if agreed is True:
            streak = lnote
        elif agreed is False:
            streak = f"against the run ({lnote})"
        else:
            streak = lnote
        price = ("value" if value >= 2 else "fair price" if value >= -3 else "thin price")
        reason = f"{streak}; model {prob:.0f}%, {price} @ {odds:.2f}"
        out.append({
            "sel": disp, "market": fam, "code": code, "odds": round(odds, 2),
            "odd_name": odd_name, "tab": tab, "cells": cells,
            "placeable": placeable,
            "model_prob": round(prob), "value": round(value, 1),
            "conf": round(conf), "agree": agreed, "reason": reason,
        })
    return out


def build_bet_picks(upcoming_by_league, gw_window=3, top_n=10, min_odds=1.40,
                    placeable_only=False):
    """Turn live upcoming fixtures into concrete, stakeable match picks.
    `upcoming_by_league` = {LeagueTitle: [fixture-prediction dicts]} from the
    app's state (each fixture carries model %s + live odds). Returns the
    headline Confident Call, a global Top-N, and per-league groups.
    `min_odds` keeps the boards to stakeable prices (ultra-short doubles stay
    visible only as per-match alternatives). `placeable_only` (auto-bettor)
    restricts each fixture's chosen selection to markets the bettor can reliably
    click on the live page (O/U 2.5 + 1X2 Home/Away)."""
    streams = load_streams()
    all_picks = []
    had_fixtures = False
    for league, fixtures in (upcoming_by_league or {}).items():
        stream = streams.get(league.lower(), [])
        if len(stream) < WINDOW:
            continue
        leans = _league_leans(stream)
        # keep only the next `gw_window` distinct gameweeks (bettable soon).
        # `fixtures` is already ordered current-week-first, so the position of
        # a week in weeks_seen IS its distance from now (0 = nearest).
        weeks_seen, keep = [], []
        for f in fixtures:
            w = f.get("week")
            if w not in weeks_seen:
                if len(weeks_seen) >= gw_window:
                    continue
                weeks_seen.append(w)
            keep.append(f)
        for f in keep:
            gw_order = weeks_seen.index(f.get("week"))
            cands = _candidates(f, leans)
            if not cands:
                continue
            had_fixtures = True
            if placeable_only:
                cands = [c for c in cands if c.get("placeable")]
                if not cands:
                    continue
            # boards honour the odds floor; if nothing on this match clears it,
            # the match drops out (don't show a sub-floor pick)
            bettable = [c for c in cands if c["odds"] >= min_odds]
            if not bettable:
                continue
            best = max(bettable, key=lambda c: c["conf"])
            alts = sorted([c for c in cands if c is not best],
                          key=lambda c: -c["conf"])[:3]
            all_picks.append({
                "league": league,
                "week": f.get("week"),
                "gw_order": gw_order,
                "match": f"{f.get('home_team')} v {f.get('away_team')}",
                "home": f.get("home_team"), "away": f.get("away_team"),
                **best, "alts": alts,
            })
    # Top board = global highest confidence (unchanged). Per-league / All =
    # gameweek-first, then confidence within the GW (#4). Confident Call is
    # still the global highest-confidence pick — the nearest-GW fix for #1 is
    # held pending confirmation; `nearest_call` is computed alongside so it can
    # be swapped in trivially once approved.
    by_conf = sorted(all_picks, key=lambda p: -p["conf"])
    by_gw = sorted(all_picks, key=lambda p: (p["gw_order"], -p["conf"]))
    nearest = [p for p in all_picks if p["gw_order"] == 0]
    per_league = {}
    for p in by_gw:
        per_league.setdefault(p["league"], []).append(p)
    return {
        "confident_call": by_conf[0] if by_conf else None,
        "nearest_call": (max(nearest, key=lambda p: p["conf"]) if nearest else None),
        "top": by_conf[:top_n],
        "per_league": per_league,
        "all": by_gw,
        "n": len(all_picks),
        "window": WINDOW,
        "gw_window": gw_window,
        "min_odds": min_odds,
        "had_fixtures": had_fixtures,
    }


_OVERVIEW_CACHE = {"t": 0, "data": None}
_BOARD_CACHE = {"t": 0, "data": None}
_RESULT_TTL = 55   # cycle scan is O(patterns·leagues·matches); cache the result


def _compute_overview_impl():
    """The heavy compute (cycle scan + engines) — run OFF the request path by
    a background thread; see compute_overview()."""
    streams = load_streams()
    lib = _pattern_library()
    leagues_out = []
    total_bets = 0
    grand_best_net = None
    for lg in LEAGUES:
        stream = streams.get(lg, [])
        if len(stream) < 200:
            continue
        pfx = {f: _prefix(stream, f) for f in ("O", "U", "GG", "NG")}
        scored = [s for s in (score_pattern(stream, p, pfx[p["feat"]])
                              for p in lib) if s]
        if not scored:
            continue
        total_bets += sum(s["bets"] for s in scored)
        scored.sort(key=lambda s: -s["net"])
        best = scored[0]
        if grand_best_net is None or best["net"] > grand_best_net["net"]:
            grand_best_net = {**best, "league": lg.title()}
        live = [c for c in (_current_cycle(stream, p, pfx[p["feat"]])
                            for p in lib) if c]
        live.sort(key=lambda c: -c["running"])
        cps = _cusum_changepoints([1 if r["_o"]["O"] else 0 for r in stream])
        deepseek = _deepseek_engine(stream, pfx)
        chatgpt = _chatgpt_engine(scored, live)
        leagues_out.append({
            "league": lg.title(),
            "n": len(stream),
            "patterns": scored,
            "best": best,
            "live": live[:6],
            "regimes": len(cps) + 1,
            "any_persist": any(s["persists"] and s["net"] > 0 for s in scored),
            "deepseek": deepseek,
            "chatgpt": chatgpt,
            "last_at": stream[-1]["_t"].strftime("%m-%d %H:%M"),
        })
    out = {
        "leagues": leagues_out,
        "total_bets": total_bets,
        "window": WINDOW,
        "n_patterns": len(lib),
        "grand_best": grand_best_net,
        "any_edge": any(l["any_persist"] for l in leagues_out),
    }
    return out


def _compute_live_board_impl():
    """The currently-open cycles across all leagues, hottest first — computed
    off the request path (see compute_live_board())."""
    streams = load_streams()
    lib = _pattern_library()
    board = []
    for lg in LEAGUES:
        stream = streams.get(lg, [])
        if len(stream) < 200:
            continue
        pfx = {f: _prefix(stream, f) for f in ("O", "U", "GG", "NG")}
        for p in lib:
            c = _current_cycle(stream, p, pfx[p["feat"]])
            if c:
                c["league"] = lg.title()
                board.append(c)
    board.sort(key=lambda c: (c["cooling"], -c["running"]))
    return board


# ── background compute: keep Pattern Lab results warm so page loads never
#    block on the heavy scan (this was the /patternlab "spins forever" bug) ──
_LATEST = {"overview": None, "board": None, "at": 0}
_BG_STARTED = False
_BG_LOCK = threading.Lock()
_BG_INTERVAL = 180


def _refresh_caches():
    _LATEST["overview"] = _compute_overview_impl()
    _LATEST["board"] = _compute_live_board_impl()
    _LATEST["at"] = time.time()


def _bg_loop():
    while True:
        try:
            _refresh_caches()
        except Exception:
            pass
        time.sleep(_BG_INTERVAL)


def start_background(interval=_BG_INTERVAL):
    global _BG_STARTED, _BG_INTERVAL
    with _BG_LOCK:
        if _BG_STARTED:
            return
        _BG_STARTED = True
        _BG_INTERVAL = interval
    threading.Thread(target=_bg_loop, daemon=True).start()


def compute_overview():
    """Route-facing: return the last background-computed overview instantly
    (never blocks). Returns a 'warming' shell until the first pass finishes."""
    start_background()
    ov = _LATEST["overview"]
    if ov is None:
        return {"warming": True, "leagues": [], "total_bets": 0,
                "window": WINDOW, "n_patterns": len(_pattern_library()),
                "grand_best": None, "any_edge": False}
    return {**ov, "warming": False, "computed_at": _LATEST["at"]}


def compute_live_board():
    """Route-facing: last background-computed board (never blocks)."""
    start_background()
    return _LATEST["board"] or []
