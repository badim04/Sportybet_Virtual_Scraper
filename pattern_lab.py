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


def _deepseek_decide(runs, pf, i, n):
    """DeepSeek's rule for the call at match index i, using ONLY results
    before i. `runs` = current tail streak length per feature (results[..i-1]);
    `pf` = prefix sums per feature. Returns (conf, call, reason, kind) or None."""
    best = None
    # 1) reversal: fade any 3+ streak (the doc's headline move)
    for feat in ("O", "U", "GG", "NG"):
        s = runs[feat]
        if s >= 3:
            conf = min(96, 50 + s * 8)
            cand = (conf, _OPP[feat], f"{s} {feat} in a row → fade to {_OPP[feat]}", "fade")
            if best is None or conf > best[0]:
                best = cand
    # 2) else continuation: ride a lopsided last-10 window
    if best is None and i >= WINDOW:
        for feat in ("O", "U", "GG", "NG"):
            cnt = pf[feat][i] - pf[feat][i - WINDOW]
            if cnt >= 7:
                conf = min(90, 50 + (cnt - 6) * 7)
                cand = (conf, feat, f"{cnt}/{WINDOW} {feat} → ride {feat}", "ride")
                if best is None or conf > best[0]:
                    best = cand
    return best


def _deepseek_engine(stream, pf):
    """Run DeepSeek's rule causally across history (track record) and emit the
    LIVE pick for the next match (applying the rule at the tail)."""
    n = len(stream)
    runs = {"O": 0, "U": 0, "GG": 0, "NG": 0}
    bets = []          # (won, odds, conf)
    for i in range(n):
        d = _deepseek_decide(runs, pf, i, n)
        if d:
            conf, call, _, _ = d
            o = stream[i].get(_ENGINE_OKEY[call])
            if o and o > 1:
                bets.append((stream[i]["_o"][call], o, conf))
        for f in runs:
            runs[f] = runs[f] + 1 if stream[i]["_o"][f] else 0
    live = _deepseek_decide(runs, pf, n, n)   # pick for the next (unplayed) match

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


_OVERVIEW_CACHE = {"t": 0, "data": None}
_BOARD_CACHE = {"t": 0, "data": None}
_RESULT_TTL = 55   # cycle scan is O(patterns·leagues·matches); cache the result


def compute_overview():
    """Everything the Pattern Lab tab renders: per-league scorecards
    (best patterns by honest net, with the cycle-win% shown beside it) and the
    live ride board of currently-open cycles. Result cached (see _RESULT_TTL)."""
    now = time.time()
    if _OVERVIEW_CACHE["data"] is not None and now - _OVERVIEW_CACHE["t"] < _RESULT_TTL:
        return _OVERVIEW_CACHE["data"]
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
    _OVERVIEW_CACHE.update(t=now, data=out)
    return out


def compute_live_board():
    """Just the currently-open cycles across all leagues, hottest first —
    the ride-and-switch board. Result cached (see _RESULT_TTL)."""
    now = time.time()
    if _BOARD_CACHE["data"] is not None and now - _BOARD_CACHE["t"] < _RESULT_TTL:
        return _BOARD_CACHE["data"]
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
    _BOARD_CACHE.update(t=now, data=board)
    return board
