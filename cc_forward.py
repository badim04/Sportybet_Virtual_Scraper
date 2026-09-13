"""Live forward-test of the Confident-Call modes — records what GLOBAL (highest
confidence across the next GWs) and NEXTUP (nearest GW's best) would bet, from
the moment the app starts, and grades each pick once its real result lands.

One row per distinct (mode, match) — no double-counting, no real bets. This is
the honest 'from now' record the history backtest can only approximate.
"""
import os, json
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
ACC = os.path.join(_HERE, "data", "accuracy")
PENDING = os.path.join(ACC, "cc_forward_pending.json")
LOG = os.path.join(ACC, "cc_forward_log.jsonl")
MAX_AGE = 6 * 3600   # drop unresolved picks older than a season cycle


def _won(sel, hs, aw):
    tot = hs + aw
    s = sel or ""
    if s.startswith("Over"): return tot > 2.5
    if s.startswith("Under"): return tot <= 2.5
    if s.startswith("Home win"): return hs > aw
    if s.startswith("Away win"): return aw > hs
    if s.startswith("GG"): return hs > 0 and aw > 0
    if s.startswith("NG"): return not (hs > 0 and aw > 0)
    if s.startswith("1X"): return hs >= aw
    if s.startswith("X2"): return aw >= hs
    if s.startswith("12"): return hs != aw
    return False


def _load():
    try:
        with open(PENDING, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(d):
    try:
        os.makedirs(ACC, exist_ok=True)
        with open(PENDING, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def record(picks_by_mode):
    """Add today's GLOBAL / NEXTUP picks to the pending set (deduped by
    mode+match, so the same standing pick is stored once)."""
    pend = _load()
    now = datetime.now()
    # expire stale unresolved picks
    fresh = {}
    for k, v in pend.items():
        try:
            age = (now - datetime.strptime(v["predicted_at"], "%Y-%m-%d %H:%M")).total_seconds()
        except Exception:
            continue
        if 0 <= age <= MAX_AGE:
            fresh[k] = v
    pend = fresh
    for mode, p in (picks_by_mode or {}).items():
        if not p:
            continue
        key = f"{mode}|{p['league']}|W{p.get('week')}|{p.get('home')}|{p.get('away')}"
        if key in pend:
            continue
        pend[key] = {
            "mode": mode, "league": p["league"], "week": p.get("week"),
            "home": p.get("home"), "away": p.get("away"),
            "sel": p.get("sel"), "odds": p.get("odds"), "conf": p.get("conf"),
            "market": p.get("market"), "predicted_at": now.strftime("%Y-%m-%d %H:%M"),
        }
    _save(pend)


def grade(league, week, home, away, hs, aws):
    """Grade any pending picks for a resolved fixture; append to the log."""
    pend = _load()
    graded, keep = [], {}
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    for k, v in pend.items():
        if (str(v.get("league", "")).lower() == str(league).lower()
                and v.get("week") == week and v.get("home") == home
                and v.get("away") == away):
            graded.append({**v, "actual_home": hs, "actual_away": aws,
                           "won": _won(v.get("sel"), hs, aws), "evaluated_at": now})
        else:
            keep[k] = v
    if graded:
        try:
            os.makedirs(ACC, exist_ok=True)
            with open(LOG, "a", encoding="utf-8") as f:
                for r in graded:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
        except Exception:
            pass
        _save(keep)
    return len(graded)


def summary():
    """Per-mode live record since the first graded pick."""
    rows = []
    try:
        with open(LOG, encoding="utf-8") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    out = {}
    for mode in ("global", "nextup"):
        rs = [r for r in rows if r.get("mode") == mode]
        if not rs:
            out[mode] = None
            continue
        rs.sort(key=lambda r: r.get("evaluated_at", ""))
        n = len(rs)
        w = sum(1 for r in rs if r["won"])
        net = sum((r["odds"] - 1) if r["won"] else -1 for r in rs)
        ls = mx = 0
        for r in rs:
            if r["won"]:
                ls = 0
            else:
                ls += 1
                mx = max(mx, ls)
        out[mode] = {
            "n": n, "win_pct": round(w / n * 100, 1), "net": round(net, 1),
            "roi": round(net / n * 100, 1), "max_streak": mx,
            "avg_odds": round(sum(r["odds"] for r in rs) / n, 2),
            "since": rs[0].get("evaluated_at"), "last": rs[-1].get("evaluated_at"),
        }
    out["_pending"] = len(_load())
    return out
