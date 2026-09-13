"""
Sportybet Virtual Predictor — Flask Web App
Serves football and racing predictions with auto-scraping background thread.
"""

import os, sys, json, glob, math, time, threading, re

# Ensure UTF-8 output on Windows (must be before any print/import that uses Unicode)
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    import io
    try:
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
        )
        sys.stderr = io.TextIOWrapper(
            sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True
        )
    except Exception:
        pass

from collections import defaultdict, Counter
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify
import webbrowser

if getattr(sys, 'frozen', False):
    # PyInstaller creates a temp folder and stores path in _MEIPASS
    BASE_DIR = sys._MEIPASS
    PROJECT_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    PROJECT_DIR = BASE_DIR

APP_VERSION = "v13-2026.07.11"

DATA_DIR = os.path.join(PROJECT_DIR, "data")
ACCURACY_DIR = os.path.join(DATA_DIR, "accuracy")
SCRAPE_INTERVAL = 5 * 60

FOOTBALL_LEAGUES = ["England", "France", "Germany", "Italy", "Spain", "Turkey"]
RACING_SPORTS = ["Greyhound Racing", "Horse Racing", "Speedway Racing", "Motorbike Racing"]

LEAGUE_TEAM_COUNT = {
    "England": 20, "Italy": 20, "Spain": 20,
    "France": 18, "Germany": 18, "Turkey": 18,
}
LEAGUE_WEEKS = {k: (v - 1) * 2 for k, v in LEAGUE_TEAM_COUNT.items()}

LEAGUE_FLAGS = {
    "England": "gb-eng", "France": "fr", "Germany": "de",
    "Italy": "it", "Spain": "es", "Turkey": "tr",
}

app = Flask(__name__, template_folder=os.path.join(BASE_DIR, 'templates'), static_folder=os.path.join(BASE_DIR, 'static'))

state = {
    "football_models": {},
    "football_seasons": {},
    "football_tables": {},
    "football_upcoming": {},
    "football_upcoming_improved": {},
    "upcoming_odds_raw": {},
    "racing_models": {},
    "racing_predictions": {},
    "current_weeks": {},
    "current_seasons": {},
    "accuracy_stats": {},
    "last_update": None,
    "scraper_status": "Starting...",
    "scraper_running": False,
    "data_loaded": False,
}
state_lock = threading.Lock()


# ═══════════════════════════════════════════════════════════
#  INLINED PREDICTION ENGINE
#  (avoids importing modules with problematic module-level
#   stdout wrapping and signal handlers)
# ═══════════════════════════════════════════════════════════

def load_football(data_dir, num_seasons=4):
    files = sorted(glob.glob(os.path.join(data_dir, "sportybet_results_*.json")))
    records, seen = [], set()
    for f in files:
        fts = re.search(r'_(\d{8}_\d{6})\.json$', os.path.basename(f))
        file_ts = fts.group(1) if fts else ""
        try:
            with open(f, encoding="utf-8") as fp:
                data = json.load(fp)
            if not isinstance(data, list):
                continue
            for r in data:
                mid = r.get("match_id")
                league = r.get("league", "")
                key = (mid, league) if mid else id(r)
                if key not in seen:
                    seen.add(key)
                    # Timestamp of the earliest file containing this result —
                    # used to pair results with predictions made before them
                    r["_file_ts"] = file_ts
                    records.append(r)
        except Exception:
            pass

    by_league = defaultdict(list)
    for r in records:
        hs, as_ = r.get("home_score"), r.get("away_score")
        if hs is None or as_ is None:
            continue
        try:
            r["home_score"] = int(hs)
            r["away_score"] = int(as_)
        except (ValueError, TypeError):
            continue
        by_league[r.get("league", "Unknown")].append(r)

    filtered = {}
    for league, matches in by_league.items():
        seasons = sorted(set(m.get("season", 0) for m in matches))
        complete = [s for s in seasons if s > 0]
        max_w = LEAGUE_WEEKS.get(league, 38)
        truly_complete = [s for s in complete
                         if len([m for m in matches if m.get("season") == s]) >= max_w * 0.9]
        use_seasons = (truly_complete or complete or seasons)[:num_seasons]
        selected = [m for m in matches if m.get("season") in use_seasons]
        filtered[league] = selected
        print(f"  [Data] {league}: {len(selected)} matches from seasons {use_seasons}", flush=True)

    return filtered


def load_racing(data_dir):
    files = sorted(glob.glob(os.path.join(data_dir, "sportybet_racing_*.json")))
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
        except Exception:
            pass

    by_sport = defaultdict(list)
    for r in records:
        by_sport[r.get("sport", "Unknown")].append(r)

    for sport, races in by_sport.items():
        venues = set(r.get("race_name") for r in races)
        print(f"  [Data] {sport}: {len(races)} races, venues: {', '.join(sorted(venues))}", flush=True)

    return by_sport


def build_team_models(league_data):
    models = {}
    for league, matches in league_data.items():
        if len(matches) < 50:
            continue
        n = len(matches)
        total_home = sum(m["home_score"] for m in matches)
        total_away = sum(m["away_score"] for m in matches)
        avg_home = total_home / n
        avg_away = total_away / n

        team_stats = defaultdict(lambda: {
            "home_goals_for": 0, "home_goals_against": 0, "home_matches": 0,
            "away_goals_for": 0, "away_goals_against": 0, "away_matches": 0,
            "home_wins": 0, "home_draws": 0, "home_losses": 0,
            "away_wins": 0, "away_draws": 0, "away_losses": 0,
        })

        for m in matches:
            ht, at = m["home_team"], m["away_team"]
            hs, as_ = m["home_score"], m["away_score"]
            team_stats[ht]["home_goals_for"] += hs
            team_stats[ht]["home_goals_against"] += as_
            team_stats[ht]["home_matches"] += 1
            team_stats[at]["away_goals_for"] += as_
            team_stats[at]["away_goals_against"] += hs
            team_stats[at]["away_matches"] += 1
            if hs > as_:
                team_stats[ht]["home_wins"] += 1
                team_stats[at]["away_losses"] += 1
            elif hs == as_:
                team_stats[ht]["home_draws"] += 1
                team_stats[at]["away_draws"] += 1
            else:
                team_stats[ht]["home_losses"] += 1
                team_stats[at]["away_wins"] += 1

        teams = {}
        for team, s in team_stats.items():
            hm = max(s["home_matches"], 1)
            am = max(s["away_matches"], 1)
            teams[team] = {
                "home_attack": (s["home_goals_for"] / hm) / max(avg_home, 0.01),
                "home_defense": (s["home_goals_against"] / hm) / max(avg_away, 0.01),
                "away_attack": (s["away_goals_for"] / am) / max(avg_away, 0.01),
                "away_defense": (s["away_goals_against"] / am) / max(avg_home, 0.01),
                "home_win_rate": s["home_wins"] / hm,
                "home_draw_rate": s["home_draws"] / hm,
                "away_win_rate": s["away_wins"] / am,
                "away_draw_rate": s["away_draws"] / am,
                "home_gpg": s["home_goals_for"] / hm,
                "away_gpg": s["away_goals_for"] / am,
                "home_conceded_pg": s["home_goals_against"] / hm,
                "away_conceded_pg": s["away_goals_against"] / am,
                "total_matches": hm + am,
            }

        score_freq = defaultdict(int)
        for m in matches:
            score_freq[f"{m['home_score']}-{m['away_score']}"] += 1

        models[league] = {
            "avg_home": avg_home,
            "avg_away": avg_away,
            "n": n,
            "teams": teams,
            "score_freq": dict(score_freq),
        }
    return models


def poisson_pmf(k, lam):
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def predict_match(model, home_team, away_team, max_goals=7):
    teams = model["teams"]
    if home_team not in teams or away_team not in teams:
        return None

    ht, at = teams[home_team], teams[away_team]
    lambda_home = max(0.1, min(model["avg_home"] * ht["home_attack"] * at["away_defense"], 5.0))
    lambda_away = max(0.1, min(model["avg_away"] * at["away_attack"] * ht["home_defense"], 5.0))

    prob_matrix = {}
    for i in range(max_goals + 1):
        for j in range(max_goals + 1):
            prob_matrix[(i, j)] = poisson_pmf(i, lambda_home) * poisson_pmf(j, lambda_away)

    home_win = sum(p for (h, a), p in prob_matrix.items() if h > a)
    draw = sum(p for (h, a), p in prob_matrix.items() if h == a)
    away_win = sum(p for (h, a), p in prob_matrix.items() if h < a)

    total_goals_probs = defaultdict(float)
    for (h, a), p in prob_matrix.items():
        total_goals_probs[h + a] += p

    over_15 = sum(p for g, p in total_goals_probs.items() if g > 1.5)
    over_25 = sum(p for g, p in total_goals_probs.items() if g > 2.5)
    over_35 = sum(p for g, p in total_goals_probs.items() if g > 3.5)
    btts = sum(p for (h, a), p in prob_matrix.items() if h > 0 and a > 0)
    top_scores = sorted(prob_matrix.items(), key=lambda x: -x[1])[:5]

    best_outcome = max(home_win, draw, away_win)
    margin_over_draw = best_outcome - draw
    if margin_over_draw < 0.12 and draw > 0.22:
        prediction = "Draw"
        outcome_scores = sorted(
            [(s, p) for s, p in prob_matrix.items() if s[0] == s[1]],
            key=lambda x: -x[1]
        )
    elif home_win > draw and home_win > away_win:
        prediction = "Home Win"
        outcome_scores = sorted(
            [(s, p) for s, p in prob_matrix.items() if s[0] > s[1]],
            key=lambda x: -x[1]
        )
    elif away_win > draw and away_win > home_win:
        prediction = "Away Win"
        outcome_scores = sorted(
            [(s, p) for s, p in prob_matrix.items() if s[1] > s[0]],
            key=lambda x: -x[1]
        )
    else:
        prediction = "Draw"
        outcome_scores = sorted(
            [(s, p) for s, p in prob_matrix.items() if s[0] == s[1]],
            key=lambda x: -x[1]
        )

    best_score = outcome_scores[0][0] if outcome_scores else top_scores[0][0]
    predicted_score = f"{best_score[0]}-{best_score[1]}"

    return {
        "home_team": home_team,
        "away_team": away_team,
        "lambda_home": round(lambda_home, 2),
        "lambda_away": round(lambda_away, 2),
        "predicted_score": predicted_score,
        "home_win_pct": round(home_win * 100, 1),
        "draw_pct": round(draw * 100, 1),
        "away_win_pct": round(away_win * 100, 1),
        "over_1_5_pct": round(over_15 * 100, 1),
        "over_2_5_pct": round(over_25 * 100, 1),
        "over_3_5_pct": round(over_35 * 100, 1),
        "btts_pct": round(btts * 100, 1),
        "ou25_call": "OVER" if over_25 > 0.5 else "UNDER",
        "btts_call": "YES" if btts > 0.5 else "NO",
        "prediction": prediction,
        "confidence": round(max(home_win, draw, away_win) * 100, 1),
        "top_scores": [(f"{h}-{a}", round(p * 100, 1)) for (h, a), p in top_scores],
    }


def generate_round_robin(teams_list):
    teams = list(teams_list)
    n = len(teams)
    if n % 2 == 1:
        teams.append("BYE")
        n += 1
    schedule = []
    rotation = list(teams)
    for _ in range(n - 1):
        round_matches = []
        for i in range(n // 2):
            home, away = rotation[i], rotation[n - 1 - i]
            if home != "BYE" and away != "BYE":
                round_matches.append((home, away))
        schedule.append(round_matches)
        rotation = [rotation[0]] + [rotation[-1]] + rotation[1:-1]
    return schedule + [[(a, h) for h, a in rm] for rm in schedule]


def predict_season(model, league):
    teams = sorted(model["teams"].keys())
    fixtures = generate_round_robin(teams)
    max_weeks = LEAGUE_WEEKS.get(league, len(fixtures))
    fixtures = fixtures[:max_weeks]

    season_predictions = []
    standings = {t: {"P": 0, "W": 0, "D": 0, "L": 0, "GF": 0, "GA": 0, "Pts": 0}
                 for t in teams}

    for week_idx, round_matches in enumerate(fixtures):
        week_num = week_idx + 1
        week_preds = []
        for home, away in round_matches:
            pred = predict_match(model, home, away)
            if pred is None:
                continue
            pred["week"] = week_num
            hs, as_ = map(int, pred["predicted_score"].split("-"))
            standings[home]["P"] += 1
            standings[away]["P"] += 1
            standings[home]["GF"] += hs
            standings[home]["GA"] += as_
            standings[away]["GF"] += as_
            standings[away]["GA"] += hs
            if hs > as_:
                standings[home]["W"] += 1
                standings[home]["Pts"] += 3
                standings[away]["L"] += 1
            elif hs == as_:
                standings[home]["D"] += 1
                standings[home]["Pts"] += 1
                standings[away]["D"] += 1
                standings[away]["Pts"] += 1
            else:
                standings[home]["L"] += 1
                standings[away]["W"] += 1
                standings[away]["Pts"] += 3
            week_preds.append(pred)
        season_predictions.append({"week": week_num, "matches": week_preds})

    table = sorted(standings.items(),
                   key=lambda x: (-x[1]["Pts"], -(x[1]["GF"] - x[1]["GA"]), -x[1]["GF"]))
    return season_predictions, table


def build_racing_models(racing_data):
    models = {}
    for sport, races in racing_data.items():
        by_venue = defaultdict(list)
        for r in races:
            by_venue[r.get("race_name", "Unknown")].append(r)

        for venue, venue_races in by_venue.items():
            # Sort races chronologically (newest first)
            venue_races.sort(
                key=lambda r: (r.get("race_date", ""), r.get("race_time", "")),
                reverse=True
            )
            n = len(venue_races)
            key = f"{sport} \u2014 {venue}"

            eo_results, ou_results = [], []
            runner_wins = Counter()
            runner_appearances = Counter()
            win_odds_list = []

            for r in venue_races:
                mkts = r.get("markets", {})
                eo = mkts.get("even_odd", {})
                if eo.get("result"):
                    eo_results.append(eo["result"])
                ou = mkts.get("over_under", {})
                if ou.get("result"):
                    ou_results.append(ou["result"])
                if r.get("winner"):
                    runner_wins[r["winner"]] += 1
                for rn in r.get("runners", []):
                    runner_appearances[rn.get("name")] += 1
                win_mkt = mkts.get("win", {})
                if win_mkt.get("odds") and win_mkt["odds"] > 1:
                    win_odds_list.append(win_mkt["odds"])

            even_count = eo_results.count("E")
            over_count = ou_results.count("O")

            # Build recent race history (last 30 races with full details)
            recent_races = []
            for i, r in enumerate(venue_races[:30]):
                mkts = r.get("markets", {})
                runners = r.get("runners", [])
                recent_races.append({
                    "race_num": n - i,
                    "race_id": r.get("race_id", ""),
                    "date": r.get("race_date", ""),
                    "time": r.get("race_time", ""),
                    "winner": r.get("winner", ""),
                    "runners": [rn.get("name", "") for rn in runners],
                    "positions": {rn.get("name", ""): rn.get("finish_pos", 0) for rn in runners},
                    "eo_result": mkts.get("even_odd", {}).get("result", ""),
                    "eo_odds": mkts.get("even_odd", {}).get("odds"),
                    "ou_result": mkts.get("over_under", {}).get("result", ""),
                    "ou_odds": mkts.get("over_under", {}).get("odds"),
                    "win_odds": mkts.get("win", {}).get("odds"),
                })

            models[key] = {
                "sport": sport, "venue": venue, "n": n,
                "even_rate": even_count / len(eo_results) if eo_results else None,
                "odd_rate": (len(eo_results) - even_count) / len(eo_results) if eo_results else None,
                "eo_n": len(eo_results),
                "over_rate": over_count / len(ou_results) if ou_results else None,
                "under_rate": (len(ou_results) - over_count) / len(ou_results) if ou_results else None,
                "ou_n": len(ou_results),
                "avg_win_odds": sum(win_odds_list) / len(win_odds_list) if win_odds_list else None,
                "top_winners": runner_wins.most_common(10),
                "total_unique_runners": len(runner_appearances),
                "recent_races": recent_races,
                "eo_sequence": eo_results[:20],
                "ou_sequence": ou_results[:20],
                "runner_wins": dict(runner_wins),
                "runner_appearances": dict(runner_appearances),
            }
    return models


def _streak_prediction(sequence, label_a="E", label_b="O"):
    """Predict next outcome based on streak analysis and mean reversion."""
    if not sequence:
        return label_a, 50.0

    # Recent streak
    streak_val = sequence[0]
    streak_len = 0
    for v in sequence:
        if v == streak_val:
            streak_len += 1
        else:
            break

    # Overall rates (recent 20)
    recent = sequence[:20]
    a_count = recent.count(label_a)
    a_rate = a_count / len(recent) if recent else 0.5

    # Blend: base rate + streak reversion
    # Longer streaks = higher reversion probability
    reversion_boost = min(streak_len * 0.05, 0.20)

    if streak_val == label_a:
        # Streak of A, predict B with increasing confidence
        pred_a_prob = max(0.25, a_rate - reversion_boost)
    else:
        # Streak of B, predict A with increasing confidence
        pred_a_prob = min(0.75, a_rate + reversion_boost)

    if pred_a_prob >= 0.5:
        return label_a, round(pred_a_prob * 100, 1)
    else:
        return label_b, round((1 - pred_a_prob) * 100, 1)


def _harville_combos(runners, probs, order_len, top_n=3, ordered=True):
    """Most likely finish combinations via the Harville model: given win
    probabilities, P(A then B) = pA * pB / (1 - pA), etc."""
    import itertools
    n = len(runners)
    if n < order_len:
        return []
    combos = []
    idx = range(n)
    for perm in itertools.permutations(idx, order_len):
        p = 1.0
        used = 0.0
        for i in perm:
            denom = 1.0 - used
            if denom <= 0:
                p = 0.0
                break
            p *= probs[i] / denom
            used += probs[i]
        combos.append((perm, p))
    if not ordered:
        # Collapse orderings of the same set (quinella)
        agg = {}
        for perm, p in combos:
            key = tuple(sorted(perm))
            agg[key] = agg.get(key, 0.0) + p
        combos = [(k, p) for k, p in agg.items()]
    combos.sort(key=lambda t: -t[1])
    out = []
    for perm, p in combos[:top_n]:
        out.append({
            "runners": [{"name": runners[i].get("name", ""),
                         "badge": runners[i].get("badge")} for i in perm],
            "prob": round(p * 100, 1),
        })
    return out


def _racing_model_stub(races):
    """Minimal model dict for venues that have upcoming races but no
    results history yet (newly discovered tracks)."""
    r0 = races[0] if races else {}
    return {
        "sport": r0.get("sport", ""), "venue": r0.get("venue", ""), "n": 0,
        "even_rate": None, "odd_rate": None, "over_rate": None,
        "under_rate": None, "eo_n": 0, "ou_n": 0, "avg_win_odds": None,
        "top_winners": [], "total_unique_runners": 0, "recent_races": [],
        "eo_sequence": [], "ou_sequence": [], "runner_wins": {},
        "runner_appearances": {},
    }


def predict_upcoming_races(models, racing_upcoming):
    """Predict every scraped upcoming race: winner probabilities per runner
    (smoothed venue history blended with live win-odds), E/O + O/U picks
    derived from the winner distribution over badge numbers, and
    exacta/quinella/trifecta combos via the Harville model."""
    predictions = {}
    for key, races in racing_upcoming.items():
        m = models.get(key, {})
        wins = m.get("runner_wins", {})
        apps = m.get("runner_appearances", {})
        eo_seq = list(m.get("eo_sequence", []))
        ou_seq = list(m.get("ou_sequence", []))
        rank_rates = load_racing_rank_rates(key)

        race_preds = []
        for race in races:
            runners = [r for r in race.get("runners", []) if r.get("name")]
            if len(runners) < 2:
                continue
            n = len(runners)

            # Historical win rate with Laplace smoothing
            model_p = []
            for rn in runners:
                w = wins.get(rn["name"], 0)
                a = apps.get(rn["name"], 0)
                model_p.append((w + 1.0) / (a + n))
            s = sum(model_p)
            model_p = [p / s for p in model_p]

            # Market-implied probabilities from live win odds
            market_p = None
            if all((rn.get("win_odds") or 0) > 1 for rn in runners):
                inv = [1.0 / rn["win_odds"] for rn in runners]
                si = sum(inv)
                market_p = [v / si for v in inv]

            if market_p:
                probs = [0.55 * mp + 0.45 * hp
                         for mp, hp in zip(market_p, model_p)]
            else:
                probs = model_p

            for rn, p in zip(runners, probs):
                rn["win_prob"] = round(p * 100, 1)
            ranked = sorted(zip(runners, probs), key=lambda t: -t[1])

            # Even/Odd + Over/Under of the WINNING badge number, from the
            # winner distribution (falls back to streak analysis)
            badges_ok = all(str(rn.get("badge") or "").isdigit() for rn in runners)
            if badges_ok:
                p_even = sum(p for rn, p in zip(runners, probs)
                             if int(rn["badge"]) % 2 == 0)
                eo_pred = "E" if p_even >= 0.5 else "O"
                eo_conf = round(max(p_even, 1 - p_even) * 100, 1)
                p_over = sum(p for rn, p in zip(runners, probs)
                             if int(rn["badge"]) > n / 2)
                ou_pred = "O" if p_over >= 0.5 else "U"
                ou_conf = round(max(p_over, 1 - p_over) * 100, 1)
            else:
                eo_pred, eo_conf = _streak_prediction(eo_seq, "E", "O")
                ou_pred, ou_conf = _streak_prediction(ou_seq, "O", "U")
            # Cascade streaks so consecutive races don't all get one answer
            eo_seq = [eo_pred] + eo_seq
            ou_seq = [ou_pred] + ou_seq

            # Improved picks: re-weight by the venue's LIVE-learned pattern
            # of which predicted rank actually wins (at some venues our
            # 2nd/3rd pick wins more often than the 1st)
            imp_fields = {}
            if rank_rates:
                runners_by_rank = [rn for rn, _ in ranked]
                weights = [max(rank_rates.get(k, 0.02), 0.02)
                           for k in range(1, len(runners_by_rank) + 1)]
                sw = sum(weights) or 1.0
                imp_probs = [w / sw for w in weights]
                order = sorted(range(len(runners_by_rank)),
                               key=lambda i: -imp_probs[i])
                imp_runners = []
                for i in order:
                    rn2 = dict(runners_by_rank[i])
                    rn2["win_prob"] = round(imp_probs[i] * 100, 1)
                    imp_runners.append(rn2)
                base = [runners_by_rank[i] for i in order]
                ps = [imp_probs[i] for i in order]
                imp_fields = {
                    "imp_winner_pick": imp_runners[0],
                    "imp_top3": imp_runners[:3],
                    "imp_runners": imp_runners,
                    "imp_exacta": _harville_combos(base, ps, 2, top_n=3, ordered=True),
                    "imp_quinella": _harville_combos(base, ps, 2, top_n=3, ordered=False),
                    "imp_trifecta": _harville_combos(base, ps, 3, top_n=3, ordered=True),
                }

            rp = {
                "race_id": race.get("race_id", ""),
                "countdown": race.get("countdown", ""),
                "status": race.get("status", ""),
                "sport": race.get("sport", ""),
                "venue": race.get("venue", ""),
                "runners": [rn for rn, _ in ranked],
                "winner_pick": ranked[0][0],
                "top3": [rn for rn, _ in ranked[:3]],
                "imp_ready": bool(imp_fields),
                **imp_fields,
                "eo_prediction": eo_pred,
                "eo_confidence": eo_conf,
                "eo_odds_even": race.get("eo_odds_even"),
                "eo_odds_odd": race.get("eo_odds_odd"),
                "ou_prediction": "OVER" if ou_pred == "O" else "UNDER",
                "ou_confidence": ou_conf,
                "ou_odds_over": race.get("ou_odds_over"),
                "ou_odds_under": race.get("ou_odds_under"),
                "exacta": _harville_combos(runners, probs, 2, top_n=3, ordered=True),
                "quinella": _harville_combos(runners, probs, 2, top_n=3, ordered=False),
                "trifecta": _harville_combos(runners, probs, 3, top_n=3, ordered=True),
                "eo_streak": m.get("eo_sequence", [])[:5],
                "ou_streak": m.get("ou_sequence", [])[:5],
            }
            race_preds.append(rp)

        if race_preds:
            predictions[key] = race_preds
    return predictions


def predict_upcoming_with_model(model, upcoming_matches, league):
    predictions = []
    for match in upcoming_matches:
        home, away = match.get("home_team", ""), match.get("away_team", "")
        if not home or not away:
            continue
        pred = predict_match(model, home, away)
        if pred is None:
            continue

        raw_week = match.get("week", "")
        wm = re.search(r'(\d+)', str(raw_week))
        pred["week"] = int(wm.group(1)) if wm else None
        pred["week_label"] = raw_week
        pred["position"] = match.get("position")
        pred["live_home_odds"] = match.get("home_odds")
        pred["live_draw_odds"] = match.get("draw_odds")
        pred["live_away_odds"] = match.get("away_odds")
        pred["live_over_25"] = match.get("over_2.5")
        pred["live_under_25"] = match.get("under_2.5")
        pred["live_gg_odds"] = match.get("gg_odds")
        pred["live_ng_odds"] = match.get("ng_odds")
        # Every other goal line + Double Chance. These are scraped but were
        # never persisted, so odds-pattern studies could only ever see the
        # 2.5 line — carry them through to the snapshots/eval logs.
        for _ln in ("1.5", "3.5", "4.5"):
            pred[f"live_over_{_ln}"] = match.get(f"over_{_ln}")
            pred[f"live_under_{_ln}"] = match.get(f"under_{_ln}")
        for _dc in ("dc_1x_odds", "dc_12_odds", "dc_x2_odds"):
            pred[f"live_{_dc}"] = match.get(_dc)
        if pred.get("live_gg_odds") and pred["live_gg_odds"] > 1:
            pred["gg_edge"] = round(pred.get("btts_pct", 0) - (1 / pred["live_gg_odds"] * 100), 1)
        if pred.get("live_ng_odds") and pred["live_ng_odds"] > 1:
            pred["ng_edge"] = round((100 - pred.get("btts_pct", 0)) - (1 / pred["live_ng_odds"] * 100), 1)

        for key_name, model_key in [("25", "over_2_5_pct")]:
            live_o = pred.get(f"live_over_{key_name}")
            live_u = pred.get(f"live_under_{key_name}")
            hist_o = pred.get(model_key, 0)
            if live_o and live_o > 1:
                pred[f"over_{key_name}_edge"] = round(hist_o - (1 / live_o * 100), 1)
            if live_u and live_u > 1:
                pred[f"under_{key_name}_edge"] = round((100 - hist_o) - (1 / live_u * 100), 1)

        if pred.get("live_home_odds") and pred["live_home_odds"] > 1:
            pred["hw_edge"] = round(pred["home_win_pct"] - (1 / pred["live_home_odds"] * 100), 1)
        if pred.get("live_draw_odds") and pred["live_draw_odds"] > 1:
            pred["d_edge"] = round(pred["draw_pct"] - (1 / pred["live_draw_odds"] * 100), 1)
        if pred.get("live_away_odds") and pred["live_away_odds"] > 1:
            pred["aw_edge"] = round(pred["away_win_pct"] - (1 / pred["live_away_odds"] * 100), 1)

        predictions.append(pred)
    return predictions


# ═══════════════════════════════════════════════════════════
#  DATA LOADING & MODEL BUILDING
# ═══════════════════════════════════════════════════════════

def detect_current_week(league_data, league):
    matches = league_data.get(league, [])
    if not matches:
        return 1
    # Use the latest (highest) season to determine current week
    max_season = max(m.get("season", 1) for m in matches)
    latest = [m for m in matches if m.get("season") == max_season]
    weeks = [m.get("week", 0) for m in latest if m.get("week")]
    return max(weeks) if weeks else 1


def detect_current_season(league_data, league):
    matches = league_data.get(league, [])
    seasons = set(m.get("season", 0) for m in matches if m.get("season"))
    return len(seasons) if seasons else 1


ODDS_MERGE_WINDOW = 15 * 60  # merge odds files up to this much older than newest


def _week_num(match):
    wm = re.search(r'(\d+)', str(match.get("week", "")))
    return int(wm.group(1)) if wm else None


def _dedup_matches(matches):
    """Collapse duplicate (week, home, away) rows, preferring the copy with
    the most odds fields (scroll snapshots overlap, so dups are normal)."""
    def richness(m):
        return sum(1 for k in ("home_odds", "draw_odds", "away_odds",
                               "over_2.5", "under_2.5",
                               "gg_odds", "ng_odds") if m.get(k) is not None)
    seen = {}
    for m in matches:
        key = (m.get("week"), m.get("home_team"), m.get("away_team"))
        if key not in seen or richness(m) > richness(seen[key]):
            seen[key] = m
    return list(seen.values())


def _split_oversized_weeks(league, matches):
    """A real gameweek has exactly teams/2 matches with each team appearing
    once. If a scrape lumped several weeks' fixtures under one label (missed
    week headers), walk in page order and start a new week whenever a team
    repeats or the week is full — robust even when some rows were missed."""
    mpw = LEAGUE_TEAM_COUNT.get(league, 20) // 2
    total_w = LEAGUE_WEEKS.get(league, 38)
    by_label = defaultdict(list)
    for m in matches:
        by_label[m.get("week")].append(m)  # preserves page order per label
    if not any(len(grp) > mpw for grp in by_label.values()):
        return matches
    rebuilt = []
    for label, grp in by_label.items():
        cw = re.search(r'Week\s*(\d+)', str(label))
        if len(grp) <= mpw or not cw:
            rebuilt.extend(grp)
            continue
        wk = int(cw.group(1))
        teams_seen, count = set(), 0
        for m in grp:
            ht, at = m.get("home_team"), m.get("away_team")
            if count >= mpw or ht in teams_seen or at in teams_seen:
                wk = wk % total_w + 1
                teams_seen, count = set(), 0
            count += 1
            teams_seen.update([ht, at])
            m["week"] = f"Week {wk} - {league}"
            m["position"] = count
            rebuilt.append(m)
    return rebuilt


def load_upcoming_odds():
    """Merge upcoming odds across recent scrape files per league.

    The newest file defines the current week; slightly older files fill in
    future weeks that a partial scrape may have missed (so one bad parse
    doesn't collapse the 6-week view down to a single GW). Weeks that have
    already been played fall out of the forward window. Matches are returned
    ordered current-week-first, handling season wrap (38 -> 1).
    """
    odds = {}
    by_league_files = defaultdict(list)
    for f in glob.glob(os.path.join(DATA_DIR, "sportybet_odds_*.json")):
        m = re.search(r'sportybet_odds_([a-z]+)_(\d{8}_\d{6})\.json$', os.path.basename(f))
        if m:
            by_league_files[m.group(1)].append((m.group(2), f))

    for _lg_key, flist in by_league_files.items():
        flist.sort()
        newest_ts = flist[-1][0]
        try:
            newest_dt = datetime.strptime(newest_ts, "%Y%m%d_%H%M%S")
        except ValueError:
            continue

        recent = []
        for ts, f in flist:
            try:
                age = (newest_dt - datetime.strptime(ts, "%Y%m%d_%H%M%S")).total_seconds()
            except ValueError:
                continue
            if 0 <= age <= ODDS_MERGE_WINDOW:
                recent.append(f)

        # Newest scrape is authoritative; older fragments (the page
        # virtualizes rows, so scrapes are often partial) may only FILL
        # gaps — never contradict. Invariants enforced: a fixture pair
        # appears once across the window, each team once per week, and
        # a week holds at most teams/2 matches.
        league = None
        odds_keys = ("home_odds", "draw_odds", "away_odds",
                     "over_1.5", "under_1.5", "over_2.5", "under_2.5",
                     "over_3.5", "under_3.5", "over_4.5", "under_4.5",
                     "gg_odds", "ng_odds",
                     "dc_1x_odds", "dc_12_odds", "dc_x2_odds")
        base_pairs = {}                 # (home, away) -> match dict
        week_teams = defaultdict(set)   # week -> teams already placed
        week_counts = defaultdict(int)  # week -> matches placed
        cur_week = None
        for f in reversed(recent):  # newest first
            try:
                with open(f, encoding="utf-8") as fp:
                    data = json.load(fp)
            except Exception:
                continue
            if not isinstance(data, list) or not data:
                continue
            league = data[0].get("league") or league
            if league:
                # Dedup before split — files written before the dedup-order
                # fix contain doubled rows that would otherwise be sliced
                # into fake shifted weeks
                data = _split_oversized_weeks(league, _dedup_matches(data))
            mpw = LEAGUE_TEAM_COUNT.get(league, 20) // 2
            if cur_week is None:
                cur_week = _week_num(data[0])
            for mt in data:
                w = _week_num(mt)
                if w is None:
                    continue
                pair = (mt.get("home_team"), mt.get("away_team"))
                if pair in base_pairs:
                    # Same fixture already placed by a newer scrape — just
                    # backfill any odds that copy was missing
                    tgt = base_pairs[pair]
                    for k in odds_keys:
                        if tgt.get(k) is None and mt.get(k) is not None:
                            tgt[k] = mt[k]
                    continue
                ht, at = pair
                if (week_counts[w] >= mpw
                        or ht in week_teams[w] or at in week_teams[w]):
                    continue  # contradicts newer data — stale/mislabeled
                base_pairs[pair] = dict(mt)
                week_teams[w].update([ht, at])
                week_counts[w] += 1

        if not base_pairs or not league:
            continue
        if cur_week is None:
            cur_week = min(_week_num(mt) for mt in base_pairs.values())
        total_w = LEAGUE_WEEKS.get(league, 38)

        def wdist(w):
            return (w - cur_week) % total_w

        kept = [(wdist(_week_num(mt)), mt.get("position") or 0, mt)
                for mt in base_pairs.values() if wdist(_week_num(mt)) < 10]
        kept.sort(key=lambda t: (t[0], t[1]))
        if kept:
            odds[league] = [t[2] for t in kept]
    return odds


# ═══════════════════════════════════════════════════════════
#  ACCURACY TRACKING & SELF-IMPROVING MODEL
# ═══════════════════════════════════════════════════════════

SNAPSHOT_MAX_AGE = 6 * 3600  # virtual seasons recycle in hours — drop stale snapshots


def _snapshot_key(week, home, away):
    return f"W{week}_{home}_{away}"


def save_prediction_snapshots(league, predictions):
    os.makedirs(ACCURACY_DIR, exist_ok=True)
    fpath = os.path.join(ACCURACY_DIR, f"snapshots_{league.lower()}.json")
    existing = {}
    if os.path.exists(fpath):
        try:
            with open(fpath, encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = {}

    # Expire stale snapshots: fixtures recycle every virtual season, so an
    # old unconsumed snapshot would swallow the NEXT season's result
    now = datetime.now()
    pruned = {}
    for key, snap in existing.items():
        if not key.startswith("W"):
            continue  # legacy season-keyed snapshot
        try:
            age = (now - datetime.strptime(
                snap.get("predicted_at", ""), "%Y-%m-%d %H:%M")).total_seconds()
        except (ValueError, TypeError):
            continue
        if 0 <= age <= SNAPSHOT_MAX_AGE:
            pruned[key] = snap
    existing = pruned

    for p in predictions:
        wk = p.get("week", 0)
        key = _snapshot_key(wk, p["home_team"], p["away_team"])
        if key not in existing:
            existing[key] = {
                "week": wk,
                "home_team": p["home_team"], "away_team": p["away_team"],
                "predicted_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "prediction": p.get("prediction"),
                "home_win_pct": p.get("home_win_pct"),
                "draw_pct": p.get("draw_pct"),
                "away_win_pct": p.get("away_win_pct"),
                "over_2_5_pct": p.get("over_2_5_pct"),
                "btts_pct": p.get("btts_pct"),
                "predicted_score": p.get("predicted_score"),
                "lambda_home": p.get("lambda_home"),
                "lambda_away": p.get("lambda_away"),
                # Live odds at prediction time — feeds odds-pattern learning
                "home_odds": p.get("live_home_odds"),
                "draw_odds": p.get("live_draw_odds"),
                "away_odds": p.get("live_away_odds"),
                "over_25_odds": p.get("live_over_25"),
                "under_25_odds": p.get("live_under_25"),
                "gg_odds": p.get("live_gg_odds"),
                "ng_odds": p.get("live_ng_odds"),
                # Other goal lines + Double Chance (added 2026-08-10) so the
                # 1.5/3.5/4.5 and DC markets become testable from eval logs
                "over_15_odds": p.get("live_over_1.5"),
                "under_15_odds": p.get("live_under_1.5"),
                "over_35_odds": p.get("live_over_3.5"),
                "under_35_odds": p.get("live_under_3.5"),
                "over_45_odds": p.get("live_over_4.5"),
                "under_45_odds": p.get("live_under_4.5"),
                "dc_1x_odds": p.get("live_dc_1x_odds"),
                "dc_12_odds": p.get("live_dc_12_odds"),
                "dc_x2_odds": p.get("live_dc_x2_odds"),
            }

    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)


def _load_accuracy_totals(league):
    fpath = os.path.join(ACCURACY_DIR, f"totals_{league.lower()}.json")
    if os.path.exists(fpath):
        try:
            with open(fpath, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"total_evaluated": 0, "correct_1x2": 0, "correct_ou25": 0,
            "correct_btts": 0, "sum_score_error": 0.0}


def _save_accuracy_totals(league, totals):
    os.makedirs(ACCURACY_DIR, exist_ok=True)
    fpath = os.path.join(ACCURACY_DIR, f"totals_{league.lower()}.json")
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(totals, f, indent=2)


def evaluate_accuracy(league, league_data):
    """Match saved prediction snapshots against results scraped AFTER the
    prediction was made. Each snapshot is consumed once evaluated, and stats
    accumulate in a running totals file — so every finished GW feeds the
    self-improvement loop exactly once."""
    fpath = os.path.join(ACCURACY_DIR, f"snapshots_{league.lower()}.json")
    if not os.path.exists(fpath):
        return {}

    try:
        with open(fpath, encoding="utf-8") as f:
            snapshots = json.load(f)
    except Exception:
        return {}

    # Index results by (week, home, away); keep every sighting with the
    # timestamp of the file it first appeared in
    results = defaultdict(list)
    for m in league_data.get(league, []):
        key = _snapshot_key(
            m.get("week", 0), m.get("home_team", ""), m.get("away_team", "")
        )
        try:
            file_dt = datetime.strptime(m.get("_file_ts", ""), "%Y%m%d_%H%M%S")
        except ValueError:
            continue
        results[key].append((file_dt, m))

    correct_1x2 = 0
    correct_ou25 = 0
    correct_btts = 0
    total = 0
    score_errors = []
    team_errors = defaultdict(list)
    consumed = []
    eval_records = []

    for key, snap in snapshots.items():
        try:
            pred_dt = datetime.strptime(
                snap.get("predicted_at", ""), "%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            consumed.append(key)  # unparseable — drop it
            continue

        actual = None
        for file_dt, m in results.get(key, []):
            # Result must surface after the prediction, within one season cycle
            gap = (file_dt - pred_dt).total_seconds()
            if -120 <= gap <= SNAPSHOT_MAX_AGE:
                actual = m
                break
        if actual is None:
            continue
        hs = actual.get("home_score")
        aws = actual.get("away_score")
        if hs is None or aws is None:
            continue

        consumed.append(key)
        total += 1
        hs, aws = int(hs), int(aws)

        # Live forward-test: grade any pending Confident-Call picks for this
        # now-resolved fixture (records both GLOBAL and NEXTUP modes).
        try:
            import cc_forward
            cc_forward.grade(league, snap.get("week"), snap.get("home_team"),
                             snap.get("away_team"), hs, aws)
        except Exception:
            pass

        # 1X2 accuracy
        if hs > aws:
            actual_outcome = "Home Win"
        elif hs == aws:
            actual_outcome = "Draw"
        else:
            actual_outcome = "Away Win"
        if snap.get("prediction") == actual_outcome:
            correct_1x2 += 1

        # O/U 2.5
        actual_total = hs + aws
        pred_ou = snap.get("over_2_5_pct", 50)
        if (actual_total > 2.5 and pred_ou > 50) or (actual_total <= 2.5 and pred_ou <= 50):
            correct_ou25 += 1

        # BTTS
        actual_btts = hs > 0 and aws > 0
        pred_btts = snap.get("btts_pct", 50)
        if (actual_btts and pred_btts > 50) or (not actual_btts and pred_btts <= 50):
            correct_btts += 1

        # Score error for lambda adjustment
        lh = snap.get("lambda_home", 1.0)
        la = snap.get("lambda_away", 1.0)
        score_errors.append(abs(hs - round(lh)) + abs(aws - round(la)))

        # Per-team error tracking for adjustments
        home_t = snap.get("home_team", "")
        away_t = snap.get("away_team", "")
        if home_t and lh > 0:
            team_errors[home_t].append({
                "role": "home", "actual_goals": hs,
                "predicted_lambda": lh, "conceded": aws,
                "predicted_concede": la,
            })
        if away_t and la > 0:
            team_errors[away_t].append({
                "role": "away", "actual_goals": aws,
                "predicted_lambda": la, "conceded": hs,
                "predicted_concede": lh,
            })

        # Persist the full labeled record (prediction + odds + outcome) —
        # this growing dataset is what odds-pattern learning runs on
        eval_records.append({**snap, "actual_home": hs, "actual_away": aws,
                             "actual_outcome": actual_outcome,
                             "evaluated_at": datetime.now().strftime("%Y-%m-%d %H:%M")})

    if eval_records:
        try:
            log_path = os.path.join(ACCURACY_DIR, f"eval_log_{league.lower()}.jsonl")
            with open(log_path, "a", encoding="utf-8") as f:
                for rec in eval_records:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # Remove consumed snapshots so each result is only counted once
    if consumed:
        for key in consumed:
            snapshots.pop(key, None)
        try:
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump(snapshots, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    # Accumulate into running totals
    totals = _load_accuracy_totals(league)
    if total:
        totals["total_evaluated"] += total
        totals["correct_1x2"] += correct_1x2
        totals["correct_ou25"] += correct_ou25
        totals["correct_btts"] += correct_btts
        totals["sum_score_error"] += sum(score_errors)
        _save_accuracy_totals(league, totals)

    cum_total = totals["total_evaluated"]
    if not cum_total:
        return {}
    stats = {
        "total_evaluated": cum_total,
        "new_evaluated": total,
        "correct_1x2": totals["correct_1x2"],
        "correct_ou25": totals["correct_ou25"],
        "correct_btts": totals["correct_btts"],
        "accuracy_1x2": round(totals["correct_1x2"] / cum_total * 100, 1),
        "accuracy_ou25": round(totals["correct_ou25"] / cum_total * 100, 1),
        "accuracy_btts": round(totals["correct_btts"] / cum_total * 100, 1),
        "avg_score_error": round(totals["sum_score_error"] / cum_total, 2),
    }
    return stats, team_errors


def compute_adjustments(league, team_errors, alpha=0.15):
    adj_path = os.path.join(ACCURACY_DIR, f"adjustments_{league.lower()}.json")
    existing = {}
    if os.path.exists(adj_path):
        try:
            with open(adj_path, encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = {}

    for team, errors in team_errors.items():
        if team not in existing:
            existing[team] = {"attack_adj": 1.0, "defense_adj": 1.0, "samples": 0}

        attack_ratios = []
        defense_ratios = []
        for e in errors:
            pred_goals = max(e["predicted_lambda"], 0.1)
            actual_goals = e["actual_goals"]
            # Clamp single-match ratios: one 4-0 result against a 0.8 lambda
            # would otherwise drag the EWMA by a 5x outlier
            attack_ratios.append(max(0.5, min(2.0, actual_goals / pred_goals)))

            pred_concede = max(e["predicted_concede"], 0.1)
            actual_concede = e["conceded"]
            defense_ratios.append(max(0.5, min(2.0, actual_concede / pred_concede)))

        if attack_ratios:
            avg_attack_ratio = sum(attack_ratios) / len(attack_ratios)
            old_adj = existing[team]["attack_adj"]
            existing[team]["attack_adj"] = round(
                alpha * avg_attack_ratio + (1 - alpha) * old_adj, 4
            )

        if defense_ratios:
            avg_defense_ratio = sum(defense_ratios) / len(defense_ratios)
            old_adj = existing[team]["defense_adj"]
            existing[team]["defense_adj"] = round(
                alpha * avg_defense_ratio + (1 - alpha) * old_adj, 4
            )

        existing[team]["samples"] = existing[team].get("samples", 0) + len(errors)

    with open(adj_path, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)

    return existing


def load_adjustments(league):
    adj_path = os.path.join(ACCURACY_DIR, f"adjustments_{league.lower()}.json")
    if os.path.exists(adj_path):
        try:
            with open(adj_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


# ── Racing accuracy: snapshots keyed by race_id (globally unique) ──────────

def _racing_slug(key):
    return re.sub(r'[^a-z0-9]+', '_', key.lower()).strip('_')


def save_racing_snapshots(key, race_preds):
    os.makedirs(ACCURACY_DIR, exist_ok=True)
    fpath = os.path.join(ACCURACY_DIR, f"racing_snapshots_{_racing_slug(key)}.json")
    existing = {}
    if os.path.exists(fpath):
        try:
            with open(fpath, encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = {}

    now = datetime.now()
    pruned = {}
    for rid, snap in existing.items():
        try:
            age = (now - datetime.strptime(
                snap.get("predicted_at", ""), "%Y-%m-%d %H:%M")).total_seconds()
        except (ValueError, TypeError):
            continue
        if 0 <= age <= SNAPSHOT_MAX_AGE:
            pruned[rid] = snap
    existing = pruned

    for rp in race_preds:
        rid = str(rp.get("race_id") or "")
        if not rid or rid in existing:
            continue
        existing[rid] = {
            "race_id": rid,
            "predicted_at": now.strftime("%Y-%m-%d %H:%M"),
            "winner": rp["winner_pick"].get("name", ""),
            "top3": [r.get("name", "") for r in rp.get("top3", [])],
            # Full predicted order + win probs: rank-pattern learning data
            "ranked": [r.get("name", "") for r in rp.get("runners", [])],
            "win_probs": [r.get("win_prob") for r in rp.get("runners", [])],
            "imp_winner": (rp.get("imp_winner_pick") or {}).get("name", ""),
            "imp_top3": [r.get("name", "") for r in rp.get("imp_top3", [])],
            "eo": rp.get("eo_prediction", ""),
            "ou": (rp.get("ou_prediction") or "")[:1],
            "exacta": [r["name"] for r in rp["exacta"][0]["runners"]] if rp.get("exacta") else [],
            "quinella": [r["name"] for r in rp["quinella"][0]["runners"]] if rp.get("quinella") else [],
            "trifecta": [r["name"] for r in rp["trifecta"][0]["runners"]] if rp.get("trifecta") else [],
        }

    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)


def evaluate_racing_accuracy(key, races):
    """Consume racing snapshots whose race_id now appears in results;
    accumulate correctness per market into a running totals file."""
    slug = _racing_slug(key)
    spath = os.path.join(ACCURACY_DIR, f"racing_snapshots_{slug}.json")
    tpath = os.path.join(ACCURACY_DIR, f"racing_totals_{slug}.json")

    totals = defaultdict(int)
    if os.path.exists(tpath):
        try:
            with open(tpath, encoding="utf-8") as f:
                totals.update(json.load(f))
        except Exception:
            pass

    snapshots = {}
    if os.path.exists(spath):
        try:
            with open(spath, encoding="utf-8") as f:
                snapshots = json.load(f)
        except Exception:
            snapshots = {}

    results = {str(r.get("race_id")): r for r in races if r.get("race_id")}
    consumed = []
    eval_records = []
    for rid, snap in snapshots.items():
        r = results.get(rid)
        if not r:
            continue
        consumed.append(rid)
        finish = sorted(r.get("runners", []),
                        key=lambda x: x.get("finish_pos") or 99)
        names = [x.get("name", "") for x in finish]
        winner = r.get("winner") or (names[0] if names else "")

        if winner:
            totals["winner_eval"] += 1
            totals["winner"] += int(snap.get("winner") == winner)
            totals["top3"] += int(winner in (snap.get("top3") or []))
            # Which predicted rank actually won — the venue's rank pattern
            ranked = snap.get("ranked") or []
            if winner in ranked:
                totals["rankwin_eval"] += 1
                totals[f"rankwin_{ranked.index(winner) + 1}"] += 1
            if snap.get("imp_winner"):
                totals["imp_eval"] += 1
                totals["imp_winner"] += int(snap["imp_winner"] == winner)
                totals["imp_top3"] += int(winner in (snap.get("imp_top3") or []))
        eval_records.append({**snap, "actual_podium": names[:3],
                             "actual_winner": winner,
                             "evaluated_at": datetime.now().strftime("%Y-%m-%d %H:%M")})
        eo_res = r.get("markets", {}).get("even_odd", {}).get("result")
        if eo_res and snap.get("eo"):
            totals["eo_eval"] += 1
            totals["eo"] += int(eo_res == snap["eo"])
        ou_res = r.get("markets", {}).get("over_under", {}).get("result")
        if ou_res and snap.get("ou"):
            totals["ou_eval"] += 1
            totals["ou"] += int(ou_res == snap["ou"])
        if len(names) >= 2 and snap.get("exacta"):
            totals["exacta_eval"] += 1
            totals["exacta"] += int(names[:2] == snap["exacta"][:2])
            q = snap.get("quinella") or snap["exacta"][:2]
            totals["quinella"] += int(set(names[:2]) == set(q))
        if len(names) >= 3 and snap.get("trifecta"):
            totals["trifecta_eval"] += 1
            totals["trifecta"] += int(names[:3] == snap["trifecta"][:3])

    if consumed:
        for rid in consumed:
            snapshots.pop(rid, None)
        try:
            with open(spath, "w", encoding="utf-8") as f:
                json.dump(snapshots, f, indent=2, ensure_ascii=False)
            with open(tpath, "w", encoding="utf-8") as f:
                json.dump(dict(totals), f, indent=2)
        except Exception:
            pass
        try:
            lpath = os.path.join(ACCURACY_DIR, f"racing_eval_log_{slug}.jsonl")
            with open(lpath, "a", encoding="utf-8") as f:
                for rec in eval_records:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

    if not totals.get("winner_eval") and not totals.get("eo_eval"):
        return {}

    def pct(c, n):
        return round(totals[c] / totals[n] * 100, 1) if totals.get(n) else None

    return {
        "evaluated": totals.get("winner_eval", 0),
        "new_evaluated": len(consumed),
        "winner_pct": pct("winner", "winner_eval"),
        "top3_pct": pct("top3", "winner_eval"),
        "eo_pct": pct("eo", "eo_eval"),
        "eo_eval": totals.get("eo_eval", 0),
        "ou_pct": pct("ou", "ou_eval"),
        "ou_eval": totals.get("ou_eval", 0),
        "exacta_pct": pct("exacta", "exacta_eval"),
        "quinella_pct": pct("quinella", "exacta_eval"),
        "exacta_eval": totals.get("exacta_eval", 0),
        "trifecta_pct": pct("trifecta", "trifecta_eval"),
        "trifecta_eval": totals.get("trifecta_eval", 0),
        "rankwin_eval": totals.get("rankwin_eval", 0),
        "rank_rates": {k: pct(f"rankwin_{k}", "rankwin_eval")
                       for k in range(1, 7) if totals.get(f"rankwin_{k}")},
        "imp_eval": totals.get("imp_eval", 0),
        "imp_winner_pct": pct("imp_winner", "imp_eval"),
        "imp_top3_pct": pct("imp_top3", "imp_eval"),
    }


def load_racing_rank_rates(key, min_samples=30):
    """Live-learned P(actual winner | our predicted rank k) for a venue,
    from the running totals. Returns {rank: prob} once enough races have
    been evaluated, else None."""
    tpath = os.path.join(ACCURACY_DIR, f"racing_totals_{_racing_slug(key)}.json")
    try:
        with open(tpath, encoding="utf-8") as f:
            totals = json.load(f)
    except Exception:
        return None
    n = totals.get("rankwin_eval", 0)
    if n < min_samples:
        return None
    return {k: totals.get(f"rankwin_{k}", 0) / n for k in range(1, 9)}


def predict_match_improved(model, home_team, away_team, adjustments, max_goals=7):
    teams = model["teams"]
    if home_team not in teams or away_team not in teams:
        return None

    ht, at = teams[home_team], teams[away_team]
    h_adj = adjustments.get(home_team, {})
    a_adj = adjustments.get(away_team, {})

    def _shrunk(adj_dict, field):
        # Raw EWMA adjustments are noisy (goal ratios from few matches) and
        # backtests show they HURT accuracy at full strength in every league.
        # Shrink 75% toward 1.0 and clamp so only persistent bias survives.
        raw = adj_dict.get(field, 1.0)
        return max(0.93, min(1.07, 1.0 + (raw - 1.0) * 0.25))

    lambda_home = max(0.1, min(
        model["avg_home"] * ht["home_attack"] * at["away_defense"]
        * _shrunk(h_adj, "attack_adj") * _shrunk(a_adj, "defense_adj"),
        5.0
    ))
    lambda_away = max(0.1, min(
        model["avg_away"] * at["away_attack"] * ht["home_defense"]
        * _shrunk(a_adj, "attack_adj") * _shrunk(h_adj, "defense_adj"),
        5.0
    ))

    prob_matrix = {}
    for i in range(max_goals + 1):
        for j in range(max_goals + 1):
            prob_matrix[(i, j)] = poisson_pmf(i, lambda_home) * poisson_pmf(j, lambda_away)

    home_win = sum(p for (h, a), p in prob_matrix.items() if h > a)
    draw = sum(p for (h, a), p in prob_matrix.items() if h == a)
    away_win = sum(p for (h, a), p in prob_matrix.items() if h < a)

    total_goals_probs = defaultdict(float)
    for (h, a), p in prob_matrix.items():
        total_goals_probs[h + a] += p

    over_15 = sum(p for g, p in total_goals_probs.items() if g > 1.5)
    over_25 = sum(p for g, p in total_goals_probs.items() if g > 2.5)
    over_35 = sum(p for g, p in total_goals_probs.items() if g > 3.5)
    btts = sum(p for (h, a), p in prob_matrix.items() if h > 0 and a > 0)

    best_outcome = max(home_win, draw, away_win)
    margin_over_draw = best_outcome - draw
    if margin_over_draw < 0.12 and draw > 0.22:
        prediction = "Draw"
        outcome_scores = sorted(
            [(s, p) for s, p in prob_matrix.items() if s[0] == s[1]],
            key=lambda x: -x[1]
        )
    elif home_win > draw and home_win > away_win:
        prediction = "Home Win"
        outcome_scores = sorted(
            [(s, p) for s, p in prob_matrix.items() if s[0] > s[1]],
            key=lambda x: -x[1]
        )
    elif away_win > draw and away_win > home_win:
        prediction = "Away Win"
        outcome_scores = sorted(
            [(s, p) for s, p in prob_matrix.items() if s[1] > s[0]],
            key=lambda x: -x[1]
        )
    else:
        prediction = "Draw"
        outcome_scores = sorted(
            [(s, p) for s, p in prob_matrix.items() if s[0] == s[1]],
            key=lambda x: -x[1]
        )

    top_scores = sorted(prob_matrix.items(), key=lambda x: -x[1])[:5]
    best_score = outcome_scores[0][0] if outcome_scores else top_scores[0][0]
    predicted_score = f"{best_score[0]}-{best_score[1]}"

    return {
        "home_team": home_team,
        "away_team": away_team,
        "lambda_home": round(lambda_home, 2),
        "lambda_away": round(lambda_away, 2),
        "predicted_score": predicted_score,
        "home_win_pct": round(home_win * 100, 1),
        "draw_pct": round(draw * 100, 1),
        "away_win_pct": round(away_win * 100, 1),
        "over_1_5_pct": round(over_15 * 100, 1),
        "over_2_5_pct": round(over_25 * 100, 1),
        "over_3_5_pct": round(over_35 * 100, 1),
        "btts_pct": round(btts * 100, 1),
        "ou25_call": "OVER" if over_25 > 0.5 else "UNDER",
        "btts_call": "YES" if btts > 0.5 else "NO",
        "prediction": prediction,
        "confidence": round(max(home_win, draw, away_win) * 100, 1),
        "home_win_prob": home_win,
        "draw_prob": draw,
        "away_win_prob": away_win,
    }


def improve_prediction(model, base_pred, adjustments, calibration=None):
    """Full improved prediction for one fixture:
    1. team attack/defense error adjustments (EWMA-learned),
    2. blend with market-implied probabilities when live odds exist —
       virtual outcomes are generated from the odds, so the market is the
       strongest available signal,
    3. calibrated no-draw call: 250k+ historical matches show a 'Draw' call
       hits only ~28% in every league while the stronger side hits ~45-52%.
    Base predictions elsewhere are untouched."""
    imp = predict_match_improved(model, base_pred["home_team"],
                                 base_pred["away_team"], adjustments)
    if imp is None:
        return None
    imp["week"] = base_pred.get("week")

    ho = base_pred.get("live_home_odds")
    do = base_pred.get("live_draw_odds")
    ao = base_pred.get("live_away_odds")
    if ho and do and ao and ho > 1 and do > 1 and ao > 1:
        inv = [1.0 / ho, 1.0 / do, 1.0 / ao]
        s = sum(inv)
        mkt = [v / s for v in inv]
        mdl = [imp.get("home_win_prob", imp["home_win_pct"] / 100),
               imp.get("draw_prob", imp["draw_pct"] / 100),
               imp.get("away_win_prob", imp["away_win_pct"] / 100)]
        sm = sum(mdl) or 1.0
        mdl = [v / sm for v in mdl]
        blend = [0.65 * mk + 0.35 * mo for mk, mo in zip(mkt, mdl)]
        sb = sum(blend) or 1.0
        imp["home_win_pct"] = round(blend[0] / sb * 100, 1)
        imp["draw_pct"] = round(blend[1] / sb * 100, 1)
        imp["away_win_pct"] = round(blend[2] / sb * 100, 1)
        imp["market_blend"] = True

    if imp["home_win_pct"] >= imp["away_win_pct"]:
        imp["prediction"] = "Home Win"
        imp["confidence"] = imp["home_win_pct"]
    else:
        imp["prediction"] = "Away Win"
        imp["confidence"] = imp["away_win_pct"]

    # Honest confidence: the real historical hit rate of this call type
    if calibration:
        row = calibration.get("calls", {}).get(imp["prediction"], {})
        if row.get("n", 0) >= 200:
            imp["calibrated_hit_pct"] = row["hit_pct"]

    imp.pop("home_win_prob", None)
    imp.pop("draw_prob", None)
    imp.pop("away_win_prob", None)
    return imp


def _calibration_path(league):
    return os.path.join(ACCURACY_DIR, f"calibration_{league.lower()}.json")


def load_calibration(league):
    try:
        with open(_calibration_path(league), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def evaluate_accuracy_improved(league, league_data, adjustments, model):
    """Backtest the improved model against actual results, and save a
    calibration file with the real historical hit rate of each call type
    (used to show honest confidence and to pick the calibrated call)."""
    if not model:
        return None
    adjustments = adjustments or {}
    matches = league_data.get(league, [])
    correct_1x2 = 0
    correct_ou25 = 0
    correct_btts = 0
    correct_nodraw = 0
    call_stats = {"Home Win": [0, 0], "Away Win": [0, 0]}  # [hits, n]
    total = 0
    pred_cache = {}  # only ~380 unique pairings per league

    for m in matches:
        hs, aws = m.get("home_score"), m.get("away_score")
        ht, at = m.get("home_team", ""), m.get("away_team", "")
        if hs is None or aws is None or not ht or not at:
            continue
        key = (ht, at)
        if key not in pred_cache:
            pred_cache[key] = predict_match_improved(model, ht, at, adjustments)
        pred = pred_cache[key]
        if not pred:
            continue
        total += 1
        hs, aws = int(hs), int(aws)

        if hs > aws:
            actual_outcome = "Home Win"
        elif hs == aws:
            actual_outcome = "Draw"
        else:
            actual_outcome = "Away Win"
        if pred["prediction"] == actual_outcome:
            correct_1x2 += 1

        # Calibrated no-draw call: across all leagues a "Draw" call hits only
        # ~28% historically while the stronger side hits ~45-52%
        nd_call = ("Home Win" if pred["home_win_pct"] >= pred["away_win_pct"]
                   else "Away Win")
        call_stats[nd_call][1] += 1
        if nd_call == actual_outcome:
            call_stats[nd_call][0] += 1
            correct_nodraw += 1

        if (hs + aws > 2.5 and pred["over_2_5_pct"] > 50) or (hs + aws <= 2.5 and pred["over_2_5_pct"] <= 50):
            correct_ou25 += 1
        actual_btts = hs > 0 and aws > 0
        if (actual_btts and pred["btts_pct"] > 50) or (not actual_btts and pred["btts_pct"] <= 50):
            correct_btts += 1

    if total == 0:
        return None

    calibration = {
        "computed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "n": total,
        "calls": {
            c: {"n": s[1], "hit_pct": round(s[0] / s[1] * 100, 1) if s[1] else 0}
            for c, s in call_stats.items()
        },
        "nodraw_1x2": round(correct_nodraw / total * 100, 1),
        "ou25": round(correct_ou25 / total * 100, 1),
        "btts": round(correct_btts / total * 100, 1),
    }
    try:
        os.makedirs(ACCURACY_DIR, exist_ok=True)
        with open(_calibration_path(league), "w", encoding="utf-8") as f:
            json.dump(calibration, f, indent=2)
    except Exception:
        pass

    return {
        "accuracy_1x2": round(correct_1x2 / total * 100, 1),
        "accuracy_nodraw": round(correct_nodraw / total * 100, 1),
        "accuracy_ou25": round(correct_ou25 / total * 100, 1),
        "accuracy_btts": round(correct_btts / total * 100, 1),
        "total": total,
    }


def rebuild_models():
    print("  [App] Loading football data...", flush=True)
    league_data = load_football(DATA_DIR, num_seasons=4)

    football_models = {}
    football_tables = {}
    football_upcoming = {}
    football_upcoming_improved = {}
    accuracy_stats = {}

    if league_data:
        print("  [App] Building Poisson models...", flush=True)
        football_models = build_team_models(league_data)

        for league in FOOTBALL_LEAGUES:
            if league not in football_models:
                continue
            _, table = predict_season(football_models[league], league)
            football_tables[league] = table

    # Use ONLY real fixtures from odds data (not model round-robin)
    upcoming_odds = load_upcoming_odds()
    odds_weeks = {}
    if upcoming_odds:
        for league, odds_list in upcoming_odds.items():
            if odds_list:
                # Lists are ordered current-week-first (handles 38 -> 1 wrap,
                # where min() would wrongly pick week 1)
                w = _week_num(odds_list[0])
                if w is not None:
                    odds_weeks[league] = w

    for league in FOOTBALL_LEAGUES:
        if league not in football_models:
            continue
        all_preds = []
        cur_week = odds_weeks.get(league, detect_current_week(league_data, league) if league in league_data else 1)

        # Current GW: use real fixtures from odds (with live odds)
        if league in upcoming_odds:
            preds = predict_upcoming_with_model(
                football_models[league], upcoming_odds[league], league
            )
            if preds:
                all_preds.extend(preds)

        # All upcoming GW fixtures come from real scraped odds data
        # No round-robin forecasts needed — Sportybet shows ~6 upcoming weeks

        if all_preds:
            football_upcoming[league] = all_preds
            try:
                save_prediction_snapshots(league, all_preds)
            except Exception as e:
                print(f"  [Accuracy] Error saving snapshots for {league}: {e}", flush=True)

    # Evaluate accuracy and build improved predictions
    print("  [App] Evaluating accuracy & building improved models...", flush=True)
    for league in FOOTBALL_LEAGUES:
        if league not in football_models or league not in league_data:
            continue

        # Resolve any pending banker tickets against fresh results
        try:
            evaluate_pending_bankers(league, league_data)
        except Exception as e:
            print(f"  [Bankers] Evaluation error for {league}: {e}", flush=True)

        # Evaluate how past predictions performed
        try:
            result = evaluate_accuracy(league, league_data)
            if result:
                stats, team_errors = result
                if stats.get("total_evaluated", 0) >= 5:
                    accuracy_stats[league] = stats
                    adjustments = compute_adjustments(league, team_errors)
                    print(f"  [Accuracy] {league}: {stats['total_evaluated']} evaluated, "
                          f"1X2={stats['accuracy_1x2']}%, O2.5={stats['accuracy_ou25']}%, "
                          f"BTTS={stats['accuracy_btts']}%", flush=True)
                else:
                    adjustments = load_adjustments(league)
            else:
                adjustments = load_adjustments(league)
        except Exception as e:
            print(f"  [Accuracy] Error evaluating {league}: {e}", flush=True)
            adjustments = {}

        # Generate improved predictions (adjustments + market blend +
        # calibrated no-draw call — valuable even with empty adjustments)
        if league in football_upcoming:
            calibration = load_calibration(league)
            improved_list = []
            for pred in football_upcoming[league]:
                imp = improve_prediction(
                    football_models[league], pred, adjustments, calibration
                )
                if imp:
                    improved_list.append(imp)
            if improved_list:
                football_upcoming_improved[league] = improved_list

                # Track improved model accuracy too
                try:
                    imp_snap_result = evaluate_accuracy_improved(league, league_data, adjustments, football_models.get(league))
                    if imp_snap_result and league in accuracy_stats:
                        # Improved tab uses the calibrated no-draw call
                        accuracy_stats[league]["improved_1x2"] = imp_snap_result.get(
                            "accuracy_nodraw", imp_snap_result.get("accuracy_1x2", 0))
                        accuracy_stats[league]["improved_ou25"] = imp_snap_result.get("accuracy_ou25", 0)
                        accuracy_stats[league]["improved_btts"] = imp_snap_result.get("accuracy_btts", 0)
                except Exception:
                    pass

    print("  [App] Loading racing data...", flush=True)
    racing_data = load_racing(DATA_DIR)
    racing_models = {}
    racing_predictions = {}
    racing_accuracy = {}

    if racing_data:
        print("  [App] Building racing models...", flush=True)
        racing_models = build_racing_models(racing_data)

        # Evaluate past race predictions against newly scraped results
        for key, m in racing_models.items():
            races = [r for r in racing_data.get(m["sport"], [])
                     if r.get("race_name") == m["venue"]]
            try:
                stats = evaluate_racing_accuracy(key, races)
                if stats:
                    racing_accuracy[key] = stats
                    if stats.get("new_evaluated"):
                        print(f"  [RaceAccuracy] {key}: +{stats['new_evaluated']} evaluated, "
                              f"winner={stats.get('winner_pct')}%, E/O={stats.get('eo_pct')}%",
                              flush=True)
            except Exception as e:
                print(f"  [RaceAccuracy] Error for {key}: {e}", flush=True)

        # Re-predict currently known upcoming races with fresh models
        with state_lock:
            racing_upcoming_raw = dict(state.get("racing_upcoming_raw", {}))
        upcoming_preds = predict_upcoming_races(racing_models, racing_upcoming_raw)
        racing_predictions = {}
        for key in set(racing_models) | set(upcoming_preds):
            m = racing_models.get(key) or _racing_model_stub(
                racing_upcoming_raw.get(key, []))
            racing_predictions[key] = {"model": m,
                                       "races": upcoming_preds.get(key, [])}

    cur_weeks, cur_seasons = {}, {}
    for league in FOOTBALL_LEAGUES:
        if league in league_data:
            cur_weeks[league] = odds_weeks.get(league, detect_current_week(league_data, league))
            cur_seasons[league] = detect_current_season(league_data, league)

    # Small recent-results cache for the auto-bettor's result cross-check
    recent_results = {}
    for lg, ms in league_data.items():
        recent_results[lg] = [
            {"week": m.get("week"), "home": m.get("home_team"),
             "away": m.get("away_team"), "hs": m.get("home_score"),
             "as": m.get("away_score"), "_file_ts": m.get("_file_ts", "")}
            for m in ms[-150:]
        ]

    with state_lock:
        state["recent_results"] = recent_results
        state["history_counts"] = {lg: len(ms) for lg, ms in league_data.items()}
        state["football_models"] = football_models
        state["football_tables"] = football_tables
        state["football_upcoming"] = football_upcoming
        state["football_upcoming_improved"] = football_upcoming_improved
        state["upcoming_odds_raw"] = upcoming_odds
        state["accuracy_stats"] = accuracy_stats
        state["racing_models"] = racing_models
        state["racing_predictions"] = racing_predictions
        state["racing_accuracy"] = racing_accuracy
        state["current_weeks"] = cur_weeks
        state["current_seasons"] = cur_seasons
        state["last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state["data_loaded"] = True

    upcoming_count = sum(len(v) for v in football_upcoming.values())
    improved_count = sum(len(v) for v in football_upcoming_improved.values())
    print(f"  [App] Models ready: {len(football_models)} football leagues, "
          f"{len(racing_models)} racing venues, {upcoming_count} predictions, "
          f"{improved_count} improved predictions", flush=True)


# ═══════════════════════════════════════════════════════════
#  BACKGROUND SCRAPER
# ═══════════════════════════════════════════════════════════

def _scrape_league_odds_direct(frame, league_name, fast=False):
    """Scrape upcoming odds for a league using direct sidebar navigation."""
    import random

    def _pause(lo=500, hi=1500):
        if fast:
            lo, hi = max(400, lo * 2 // 3), max(600, hi * 2 // 3)
        time.sleep(random.randint(lo, hi) / 1000)

    print(f"  [Odds] Navigating to {league_name}", flush=True)

    def _pw_click(selectors, label, tm=6000):
        for sel in selectors:
            try:
                loc = frame.locator(sel).first
                loc.wait_for(state="visible", timeout=tm)
                loc.scroll_into_view_if_needed()
                loc.click()
                print(f"    Clicked '{label}' via: {sel}", flush=True)
                return True
            except Exception:
                continue
        print(f"    Could not click '{label}'", flush=True)
        return False

    tm = 4000 if fast else 8000

    # Step 1: Expand Football League sidebar using exact CSS selectors
    # Check if already expanded to avoid collapsing it on subsequent leagues
    needs_expand = frame.evaluate(r"""() => {
        const links = Array.from(document.querySelectorAll('a'));
        const fl = links.find(a => a.textContent.trim() === 'Football League');
        if (!fl) return true; // If not found, try clicking anyway via Playwright
        return fl.classList.contains('collapsed');
    }""")

    if needs_expand:
        fl_selectors = [
            "a.toggler.gr-icon-league",
            "a[class*='toggler'][class*='gr-icon-league']",
            "a[class*='gr-icon-league']",
            "a.toggler:has-text('Football League')",
            "a:has-text('Football League')",
            "li:has-text('Football League') > a",
        ]
        fl_clicked = _pw_click(fl_selectors, "Football League", tm)
        if not fl_clicked:
            print(f"    Could not click Football League toggler", flush=True)
            return []
        _pause(800, 1500)
    else:
        print(f"    Football League toggler already expanded", flush=True)

    # Verify submenu expanded (wait for Upcoming or Results History to appear)
    submenu_ready = False
    for attempt in range(12):
        try:
            loc = frame.locator("a[class*='text-overflow']:has-text('Upcoming')").first
            if loc.is_visible(timeout=500):
                submenu_ready = True
                break
        except Exception:
            pass
        try:
            loc = frame.locator("a[class*='text-overflow']:has-text('Results History')").first
            if loc.is_visible(timeout=500):
                submenu_ready = True
                break
        except Exception:
            pass
        # Re-click toggler at attempt 4 (sometimes needs double tap)
        if attempt == 4 and needs_expand:
            try:
                frame.locator("a[class*='gr-icon-league']").first.click()
                print(f"    Re-clicked Football League toggler", flush=True)
            except Exception:
                pass
        time.sleep(0.5)

    if submenu_ready:
        print(f"    Football League submenu expanded", flush=True)
    else:
        print(f"    Football League submenu did NOT expand", flush=True)

    # Step 2: Click Upcoming INSIDE Football League's <li> container (JS-scoped)
    # This prevents clicking the wrong "Upcoming" (e.g. Greyhound Racing's)
    up_clicked = frame.evaluate(r"""() => {
        const flLinks = Array.from(document.querySelectorAll('a'));
        const flLink = flLinks.find(a => a.textContent.trim() === 'Football League');
        if (!flLink) return 'no_fl_link';
        const flLi = flLink.closest('li');
        if (!flLi) return 'no_fl_li';
        const subLinks = flLi.querySelectorAll('a');
        for (const a of subLinks) {
            if (a.textContent.trim() === 'Upcoming' && a.offsetParent !== null) {
                a.click();
                return 'clicked';
            }
        }
        return 'not_found';
    }""")
    print(f"    Upcoming click (JS-scoped): {up_clicked}", flush=True)

    if up_clicked != 'clicked':
        # Playwright fallback — try all visible Upcoming links
        up_fallback = _pw_click([
            "a:has-text('Upcoming')",
            "text=Upcoming",
        ], "Upcoming (fallback)", tm)
        if not up_fallback:
            print(f"    Could not click Upcoming at all", flush=True)
            return []
    _pause(1500, 2500)

    # Step 3: Click league tab using div.item (confirmed DOM structure from results scraper)
    tab_clicked = frame.evaluate(f"""(league) => {{
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

    if tab_clicked:
        print(f"    Clicked league tab '{league_name}' via {tab_clicked}", flush=True)
    else:
        # Playwright fallback
        _pw_click([
            f"text={league_name}",
            f"a:has-text('{league_name}')",
            f"[class*='tab']:has-text('{league_name}')",
        ], league_name, tm)
    _pause(1500, 2500)

    extra_matches = []

    # Step 3: Wait for content to load (retry until LOADING disappears)
    body_text = ""
    for attempt in range(6):
        try:
            body_text = frame.evaluate("() => document.body.innerText")
        except Exception as e:
            print(f"    Failed to get body text: {e}", flush=True)
            return []
        if body_text and "LOADING" not in body_text:
            break
        _pause(1500, 2500)

    if not body_text:
        return extra_matches

    # The upcoming list is virtualized — only rendered rows appear in
    # innerText, so one grab often captures 1-3 of the ~6 GWs shown.
    # Scroll through the panel and concatenate snapshots (sentinel line
    # resets parser week context between them; dedup removes overlap).
    texts = [body_text]
    for _ in range(5):
        try:
            moved = frame.evaluate("""() => {
                let target = null;
                for (const el of document.querySelectorAll('div, main, section')) {
                    if (el.scrollHeight > el.clientHeight + 80 && el.clientHeight > 150) {
                        target = el;
                    }
                }
                if (target) {
                    const before = target.scrollTop;
                    target.scrollTop = before + target.clientHeight * 0.85;
                    return target.scrollTop > before;
                }
                const by = window.scrollY;
                window.scrollBy(0, window.innerHeight * 0.85);
                return window.scrollY > by;
            }""")
        except Exception:
            break
        _pause(500, 900)
        try:
            t = frame.evaluate("() => document.body.innerText")
        except Exception:
            break
        if t:
            texts.append(t)
        if not moved:
            break
    body_text = "\n===SNAPSHOT===\n".join(texts)
    # Scroll back up so the market-filter tabs are clickable afterwards
    try:
        frame.evaluate("""() => {
            for (const el of document.querySelectorAll('div, main, section')) {
                if (el.scrollHeight > el.clientHeight + 80) el.scrollTop = 0;
            }
            window.scrollTo(0, 0);
        }""")
    except Exception:
        pass

    # Debug dump to analyze page format
    if league_name == "England":
        try:
            with open("debug_upcoming_page.txt", "w", encoding="utf-8") as df:
                df.write(body_text)
            print(f"    DEBUG: page dump ({len(body_text)} chars)", flush=True)
        except Exception:
            pass

    lines = body_text.split('\n')
    week_info = ""
    matches = []

    # Scan page text for week headers and match patterns
    current_week_label = ""
    pos = 1
    stop_words = {'Betslip', 'My', 'Place', 'Login', 'Bet', 'HOME', 'DRAW', 'AWAY',
                  'Match', 'Over', 'Correct', 'MAIN', 'Others', 'Go', 'WATCH', 'WAITING',
                  'Ranking', 'NOW', 'Tournaments', 'Greyhound', 'Horse', 'Speedway',
                  'Motorbike', 'Results', 'Football', 'MENU', 'WIN', 'SHOW', 'Upcoming'}
    all_lines = [l.strip() for l in lines if l.strip()]

    i = 0
    while i < len(all_lines):
        line = all_lines[i]

        # Snapshot boundary from scroll capture — the next rows may belong
        # to any week, so wait for their own header before attributing
        if line == '===SNAPSHOT===':
            current_week_label = ""
            pos = 1
            i += 1
            continue

        # Check for week header: "Week 38 - England" or "England Week 38"
        wm = re.search(r'Week\s*(\d+)\s*[-\u2013]\s*(\w+)', line)
        if not wm:
            wm2 = re.search(r'(?:Football League[:\s]*)?(\w+)\s+Week\s*(\d+)', line)
            if wm2 and wm2.group(1).lower() == league_name.lower():
                current_week_label = f"Week {wm2.group(2)} - {wm2.group(1)}"
                pos = 1
                i += 1
                continue
        if wm and wm.group(2).lower() == league_name.lower():
            current_week_label = f"Week {wm.group(1)} - {wm.group(2)}"
            pos = 1
            i += 1
            continue

        if not current_week_label:
            i += 1
            continue

        # Odds-view row numbers ("1.", "2.", ...) restart at "1." for each
        # week section — catches week boundaries whose header line the page
        # didn't render (otherwise 6 weeks of fixtures collapse into one).
        rn = re.match(r'^(\d+)\.$', line)
        if rn:
            if int(rn.group(1)) == 1 and pos > 1:
                cw = re.search(r'Week\s*(\d+)', current_week_label)
                if cw:
                    total_w = LEAGUE_WEEKS.get(league_name, 38)
                    next_week = int(cw.group(1)) % total_w + 1
                    current_week_label = f"Week {next_week} - {league_name}"
                    pos = 1
            i += 1
            continue

        # Pattern 1: HOME score AWAY (live/completed matches with ":")
        if (re.match(r'^[A-Z]{2,4}$', line)
                and line not in stop_words
                and i + 2 < len(all_lines)):
            next1 = all_lines[i + 1]
            next2 = all_lines[i + 2]

            # 1a: HOME / "0 : 0" or "1 : 2" / AWAY (Ranking view with scores)
            if ':' in next1 and re.match(r'^[A-Z]{2,4}$', next2) and next2 not in stop_words:
                match = {
                    "league": league_name,
                    "week": current_week_label,
                    "position": pos,
                    "home_team": line,
                    "away_team": next2,
                    "scraped_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                matches.append(match)
                pos += 1
                i += 3
                if i < len(all_lines) and re.match(r'^\d+$', all_lines[i]):
                    i += 1
                continue

            # 1b: HOME / "-" / AWAY (odds view: "FOR\n-\nNEW" then odds follow)
            if next1 == '-' and re.match(r'^[A-Z]{2,4}$', next2) and next2 not in stop_words:
                match = {
                    "league": league_name,
                    "week": current_week_label,
                    "position": pos,
                    "home_team": line,
                    "away_team": next2,
                    "scraped_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                # Try to extract 1X2 odds from following lines
                # Expected: "1", home_odds, "X", draw_odds, "2", away_odds
                j = i + 3
                if j + 5 < len(all_lines) and all_lines[j] == '1':
                    try:
                        ho = float(all_lines[j + 1])
                        if all_lines[j + 2] == 'X':
                            do = float(all_lines[j + 3])
                            if all_lines[j + 4] == '2':
                                ao = float(all_lines[j + 5])
                                match["home_odds"] = ho
                                match["draw_odds"] = do
                                match["away_odds"] = ao
                    except (ValueError, IndexError):
                        pass
                matches.append(match)
                pos += 1
                i += 3
                continue

            # 1c: HOME / AWAY (two consecutive team codes, no separator)
            if re.match(r'^[A-Z]{2,4}$', next1) and next1 not in stop_words:
                match = {
                    "league": league_name,
                    "week": current_week_label,
                    "position": pos,
                    "home_team": line,
                    "away_team": next1,
                    "scraped_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                matches.append(match)
                pos += 1
                i += 2
                continue

        # Pattern 2: "HOME - AWAY" inline format (with possible trailing space)
        inline = re.match(r'^([A-Z]{2,4})\s*-\s*([A-Z]{2,4})\s*$', line)
        if inline and inline.group(1) not in stop_words and inline.group(2) not in stop_words:
            match = {
                "league": league_name,
                "week": current_week_label,
                "position": pos,
                "home_team": inline.group(1),
                "away_team": inline.group(2),
                "scraped_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            matches.append(match)
            pos += 1
            i += 1
            continue

        i += 1

    # Deduplicate FIRST: overlapping scroll snapshots capture the same row
    # twice under the same week label. Dedup must happen before the
    # oversized-week split, or 10 fixtures x 2 copies get sliced into two
    # fake consecutive weeks.
    matches = _dedup_matches(matches)

    # Safety net: split any week label holding several weeks' worth of
    # DISTINCT matches (missed headers) into consecutive weeks in page order
    n_before = len(set(m.get("week") for m in matches))
    matches = _split_oversized_weeks(league_name, matches)
    n_after = len(set(m.get("week") for m in matches))
    if n_after != n_before:
        print(f"    Split oversized week groups: {n_before} -> {n_after} weeks", flush=True)

    if not matches and body_text:
        snippet = body_text[:800].replace('\n', ' | ')
        print(f"  [Odds] DEBUG page text: {snippet}", flush=True)

    match_weeks = {}
    for m in matches:
        w = m.get("week", "?")
        match_weeks[w] = match_weeks.get(w, 0) + 1
    print(f"  [Odds] {league_name}: {len(matches)} matches across {len(match_weeks)} weeks: {match_weeks}", flush=True)

    # Step 4: Try to click "Match result" tab to get 1X2 odds
    if matches:
        try:
            clicked = frame.evaluate("""() => {
                const allEls = document.querySelectorAll('a, li, span, div, button, [class*="tab"], [class*="filter"]');
                for (const el of allEls) {
                    const text = el.textContent.trim();
                    if ((text === 'Match result' || text === '1X2')
                        && el.offsetParent !== null && el.offsetWidth > 0) {
                        el.click();
                        return 'clicked_mr';
                    }
                }
                return 'not_found';
            }""")
            if clicked == 'clicked_mr':
                print(f"    Match result tab: {clicked}", flush=True)
                _pause(2000, 3000)
                mr_text = frame.evaluate("() => document.body.innerText")
                if mr_text:
                    # Look for odds (decimal numbers like 3.08) associated with each match
                    mr_lines = [l.strip() for l in mr_text.split('\n') if l.strip()]
                    # Collect all decimal odds from the page
                    all_odds = []
                    for line in mr_lines:
                        nums = re.findall(r'\b(\d+\.\d{2})\b', line)
                        all_odds.extend([float(n) for n in nums])
                    # Assign odds in groups of 3 (home, draw, away) to matches
                    if len(all_odds) >= len(matches) * 3:
                        for idx, m in enumerate(matches):
                            base = idx * 3
                            m["home_odds"] = all_odds[base]
                            m["draw_odds"] = all_odds[base + 1]
                            m["away_odds"] = all_odds[base + 2]
                        print(f"    Assigned 1X2 odds to {len(matches)} matches", flush=True)
                    elif all_odds:
                        print(f"    Found {len(all_odds)} odds numbers but need {len(matches)*3}", flush=True)
        except Exception as e:
            print(f"  [Odds] Match result tab error: {e}", flush=True)

        # Step 5: Over/Under tab — scroll-capture then per-row parse
        # (the old whole-page approach required exactly 8 numbers per match
        # across the entire virtualized list and silently failed)
        try:
            clicked = frame.evaluate("""() => {
                const allEls = document.querySelectorAll('a, li, span, div, button, [class*="tab"], [class*="filter"]');
                for (const el of allEls) {
                    const text = el.textContent.trim();
                    if ((text === 'Over/Under' || text === 'Over / Under')
                        && el.offsetParent !== null && el.offsetWidth > 0) {
                        el.click();
                        return 'clicked_ou';
                    }
                }
                return 'not_found';
            }""")
            if clicked == 'clicked_ou':
                print(f"    Over/Under tab: {clicked}", flush=True)
                _pause(2000, 3000)
                ou_texts = []
                for _ in range(14):
                    try:
                        t = frame.evaluate("() => document.body.innerText")
                    except Exception:
                        break
                    if t:
                        ou_texts.append(t)
                    try:
                        moved = frame.evaluate("""() => {
                            let scrolled = false;
                            for (const el of document.querySelectorAll('div, main, section')) {
                                if (el.scrollHeight > el.clientHeight + 80) {
                                    const b = el.scrollTop;
                                    el.scrollTop += el.clientHeight * 0.85;
                                    if (el.scrollTop > b) scrolled = true;
                                }
                            }
                            const by = window.scrollY;
                            window.scrollBy(0, window.innerHeight * 0.85);
                            return scrolled || window.scrollY > by;
                        }""")
                    except Exception:
                        break
                    _pause(500, 900)
                    if not moved:
                        break
                ou_text = "\n===SNAPSHOT===\n".join(ou_texts)
                if league_name == "England":
                    try:
                        with open("debug_ou_page.txt", "w", encoding="utf-8") as df:
                            df.write(ou_text)
                    except Exception:
                        pass
                assigned = _assign_ou_odds_per_row(league_name, matches, ou_text)
                if assigned:
                    print(f"    Assigned O/U odds to {assigned} matches (per-row)", flush=True)
                else:
                    print(f"    O/U per-row parse matched 0 rows", flush=True)
        except Exception as e:
            print(f"  [Odds] O/U tab error: {e}", flush=True)

        # Step 6: MAIN > "1X2 + Double Chance + GG/NG" sub-tab — GG/NG odds
        # (BTTS market; layout verified live 2026-07-26). Same virtualized
        # scroll-capture as O/U; parse is label-driven so a no-op tab click
        # just assigns 0 rows instead of garbage.
        try:
            # Scroll back up so the market-filter bar is at hand
            try:
                frame.evaluate("""() => {
                    for (const el of document.querySelectorAll('div, main, section')) {
                        if (el.scrollHeight > el.clientHeight + 80) el.scrollTop = 0;
                    }
                    window.scrollTo(0, 0);
                }""")
            except Exception:
                pass
            _pause(500, 900)
            frame.evaluate("""() => {
                for (const el of document.querySelectorAll('a, li, span, div, button')) {
                    const t = el.textContent.trim();
                    if (t === 'MAIN' && el.offsetParent !== null && el.offsetWidth > 0) {
                        el.click(); return 'clicked_main';
                    }
                }
                return 'not_found';
            }""")
            _pause(800, 1400)
            clicked = frame.evaluate("""() => {
                for (const el of document.querySelectorAll('div.item, div[class*="item"], a, li, span, button')) {
                    const t = el.textContent.trim();
                    if (t === '1X2 + Double Chance + GG/NG' && el.offsetParent !== null) {
                        el.click(); return 'clicked_ggng';
                    }
                }
                return 'not_found';
            }""")
            if clicked == 'clicked_ggng':
                print(f"    GG/NG sub-tab: {clicked}", flush=True)
                _pause(2000, 3000)
                gg_texts = []
                for _ in range(14):
                    try:
                        t = frame.evaluate("() => document.body.innerText")
                    except Exception:
                        break
                    if t:
                        gg_texts.append(t)
                    try:
                        moved = frame.evaluate("""() => {
                            let scrolled = false;
                            for (const el of document.querySelectorAll('div, main, section')) {
                                if (el.scrollHeight > el.clientHeight + 80) {
                                    const b = el.scrollTop;
                                    el.scrollTop += el.clientHeight * 0.85;
                                    if (el.scrollTop > b) scrolled = true;
                                }
                            }
                            const by = window.scrollY;
                            window.scrollBy(0, window.innerHeight * 0.85);
                            return scrolled || window.scrollY > by;
                        }""")
                    except Exception:
                        break
                    _pause(500, 900)
                    if not moved:
                        break
                gg_text = "\n===SNAPSHOT===\n".join(gg_texts)
                if league_name == "England":
                    try:
                        with open("debug_ggng_page.txt", "w", encoding="utf-8") as df:
                            df.write(gg_text)
                    except Exception:
                        pass
                assigned = _assign_ggng_odds_per_row(league_name, matches, gg_text)
                if assigned:
                    print(f"    Assigned GG/NG odds to {assigned} matches (per-row)", flush=True)
                else:
                    print(f"    GG/NG per-row parse matched 0 rows", flush=True)
            else:
                print(f"    GG/NG sub-tab not found", flush=True)
        except Exception as e:
            print(f"  [Odds] GG/NG tab error: {e}", flush=True)

    # Merge multi-week fixtures from pre-click page (without odds)
    # with current week matches (with odds)
    if extra_matches:
        current_weeks_in_matches = set(m.get("week") for m in matches)
        for em in extra_matches:
            if em["week"] not in current_weeks_in_matches:
                matches.append(em)

    return matches


def _frame_login_error(frame):
    """The virtustec games provider sometimes rejects a session: the iframe
    loads but shows only 'Login error ... CODE: CORE-1002'. A page reload
    usually gets a fresh, working session."""
    try:
        txt = frame.evaluate("() => (document.body.innerText || '').slice(0, 2000)")
        return ("Login error" in txt) or ("CODE: CORE" in txt)
    except Exception:
        return False


def _health(component, ok=True, error=None):
    """Track per-component health so logs and /api/status say plainly what
    works and what doesn't."""
    now = datetime.now().strftime("%m-%d %H:%M:%S")
    with state_lock:
        h = state.setdefault("health", {})
        entry = h.setdefault(component, {"ok_at": None, "error": None})
        if ok:
            entry["ok_at"] = now
            entry["error"] = None
        else:
            entry["error"] = f"{now}  {error}"


def _print_health():
    with state_lock:
        h = {k: dict(v) for k, v in state.get("health", {}).items()}
    if not h:
        return
    parts = []
    for comp, info in sorted(h.items()):
        if info.get("error"):
            parts.append(f"{comp}: !! {info['error']}")
        else:
            parts.append(f"{comp}: ok {info.get('ok_at')}")
    print("  [Health] " + "  |  ".join(parts), flush=True)


def _fresh_event_loop():
    """After the watchdog kills a wedged Playwright driver, the greenlet
    unwind leaves this thread's asyncio *running-loop marker* set — and
    Playwright's sync API checks exactly that marker (get_running_loop), so
    every reconnect fails with 'Sync API inside the asyncio loop'. Clear the
    zombie marker and install a fresh loop."""
    import asyncio
    try:
        running = asyncio.events._get_running_loop()
    except Exception:
        running = None
    if running is not None:
        try:
            asyncio.events._set_running_loop(None)
            print("  [Health] Cleared zombie running-loop marker "
                  "(recovering from killed browser)", flush=True)
        except Exception:
            pass
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("closed")
    except Exception:
        try:
            asyncio.set_event_loop(asyncio.new_event_loop())
        except Exception:
            pass


def _get_browser_and_frame(results_mod):
    """Launch browser and find the virtual iframe. Returns (pw, browser, ctx, page, frame) or raises."""
    _fresh_event_loop()
    pw, browser, ctx, page = results_mod.create_stealth_browser(
        headless=True, proxy_server=None, stealth_mode="advanced"
    )
    _register_driver("scraper", pw)  # protect the setup phase too
    frame = None
    for attempt in range(5):
        _beat("scraper")
        try:
            page.goto(results_mod.SITE_URL, timeout=60000, wait_until="domcontentloaded")
        except Exception:
            pass
        time.sleep(15)
        frame = results_mod.wait_and_find_virtual_frame(page, timeout=45000)
        is_real = (frame is not page and hasattr(frame, 'url') and frame.url
                   and "sportybet.com/ng/virtual" not in frame.url
                   and frame.url != "about:blank")
        if is_real:
            if _frame_login_error(frame):
                print("  [Health] Virtual session rejected (CORE-1002 login error) — "
                      "reloading for a fresh session", flush=True)
                _health("virtual_session", ok=False,
                        error="CORE-1002 login error — reloading")
                time.sleep(8)
                continue
            _health("virtual_session")
            return pw, browser, ctx, page, frame
        print(f"  [Scraper] Iframe attempt {attempt+1}/3 failed, retrying...", flush=True)
        time.sleep(5)
    # Clean up on failure
    _unregister_driver("scraper")
    _safe_cleanup(pw, browser)
    return None


def _safe_cleanup(pw, browser):
    """Safely close browser and stop playwright without deadlocking the calling thread."""
    def _do_clean():
        try:
            if browser: browser.close()
        except Exception: pass
        try:
            if pw: pw.stop()
        except Exception: pass
    
    t = threading.Thread(target=_do_clean)
    t.start()
    t.join(timeout=3.0)  # If it hangs, we abandon the cleanup thread and proceed


def _import_scraper_modules():
    """Import scraper modules with signal handler monkey-patching."""
    import importlib, signal
    if PROJECT_DIR not in sys.path:
        sys.path.insert(0, PROJECT_DIR)
    _orig_signal = signal.signal
    signal.signal = lambda *a, **kw: None
    try:
        results_mod = importlib.import_module("sportybet_virtual_scraper_v1")
        odds_mod = importlib.import_module("sportybet_virtual_odds_v1")
        racing_mod = importlib.import_module("sportybet_virtual_racing_scraper_v2")
    finally:
        signal.signal = _orig_signal
    # Feed the watchdog from inside long per-league scrape loops
    results_mod.HEARTBEAT = lambda: _beat("scraper")
    return results_mod, odds_mod, racing_mod


def _assign_ou_odds_per_row(league_name, matches, body_text):
    """Parse the Over/Under market view per row and attach O/U odds to the
    already-parsed matches, keyed by (week, home, away). Row anchor is the
    HOME / - / AWAY team-code triple; the decimals that follow are the
    over/under prices for goal lines 1.5, 2.5, 3.5, 4.5."""
    index = {}
    for m in matches:
        index[(m.get("week"), m.get("home_team"), m.get("away_team"))] = m

    lines = [l.strip() for l in body_text.split('\n') if l.strip()]
    total_w = LEAGUE_WEEKS.get(league_name, 38)
    team_re = re.compile(r'^[A-Z]{2,4}$')
    week_label = ""
    pos = 1
    assigned = set()
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if line == '===SNAPSHOT===':
            week_label = ""
            pos = 1
            i += 1
            continue
        wm = re.search(r'Week\s*(\d+)\s*[-–]\s*(\w+)', line)
        if wm and wm.group(2).lower() == league_name.lower():
            week_label = f"Week {wm.group(1)} - {wm.group(2)}"
            pos = 1
            i += 1
            continue
        wm2 = re.search(r'(?:Football League[:\s]*)?(\w+)\s+Week\s*(\d+)', line)
        if wm2 and wm2.group(1).lower() == league_name.lower():
            week_label = f"Week {wm2.group(2)} - {wm2.group(1)}"
            pos = 1
            i += 1
            continue
        rn = re.match(r'^(\d+)\.$', line)
        if rn:
            if int(rn.group(1)) == 1 and pos > 1 and week_label:
                cw = re.search(r'Week\s*(\d+)', week_label)
                if cw:
                    week_label = f"Week {int(cw.group(1)) % total_w + 1} - {league_name}"
                    pos = 1
            i += 1
            continue
        # Row anchor: HOME / - / AWAY
        if (week_label and team_re.match(line) and i + 2 < n
                and lines[i + 1] == '-' and team_re.match(lines[i + 2])):
            home, away = line, lines[i + 2]
            nums = []
            j = i + 3
            while j < n and len(nums) < 8:
                l2 = lines[j]
                if (l2 == '===SNAPSHOT===' or re.match(r'^\d+\.$', l2)
                        or 'Week' in l2
                        or (team_re.match(l2) and j + 2 < n and lines[j + 1] == '-')):
                    break
                nums.extend(float(x) for x in re.findall(r'\b(\d{1,2}\.\d{2})\b', l2))
                j += 1
            key3 = (week_label, home, away)
            m = index.get(key3)
            if m is not None and len(nums) >= 4 and key3 not in assigned:
                # Sanity: an over/under pair's implied probabilities must sum
                # to ~1 plus margin — otherwise the layout isn't what we
                # assume and assigning would be worse than skipping
                pairs = list(zip(nums[0::2], nums[1::2]))
                ok_pairs = [p for p in pairs
                            if 1.0 <= (1 / p[0] + 1 / p[1]) <= 1.45]
                if len(ok_pairs) >= len(pairs) - 1 and len(pairs) >= 2:
                    fields = [("over_1.5", "under_1.5"), ("over_2.5", "under_2.5"),
                              ("over_3.5", "under_3.5"), ("over_4.5", "under_4.5")]
                    for (fo, fu), (vo, vu) in zip(fields, pairs):
                        m[fo] = vo
                        m[fu] = vu
                    assigned.add(key3)
            pos += 1
            i = j
            continue
        i += 1
    return len(assigned)


# Labels carried by the "1X2 + Double Chance + GG/NG" view, in page order.
# 1X/12/X2/GG/NG all match the team-code regex, so the row scanner must not
# mistake them for the next fixture's HOME code.
_GGNG_LABELS = ("1", "X", "2", "1X", "12", "X2", "GG", "NG")
_GGNG_FIELDS = {"1": "mr_home_odds", "X": "mr_draw_odds", "2": "mr_away_odds",
                "1X": "dc_1x_odds", "12": "dc_12_odds", "X2": "dc_x2_odds",
                "GG": "gg_odds", "NG": "ng_odds"}


def _assign_ggng_odds_per_row(league_name, matches, body_text):
    """Parse the MAIN > '1X2 + Double Chance + GG/NG' market view per row and
    attach Double Chance (1X/12/X2) + GG/NG odds to the already-parsed
    matches, keyed by (week, home, away). Row anchor is the HOME / - / AWAY
    triple; the block that follows is labeled pairs (verified live 2026-07-26):
    1 x.xx X x.xx 2 x.xx 1X x.xx 12 x.xx X2 x.xx GG x.xx NG x.xx"""
    index = {}
    for m in matches:
        index[(m.get("week"), m.get("home_team"), m.get("away_team"))] = m

    lines = [l.strip() for l in body_text.split('\n') if l.strip()]
    total_w = LEAGUE_WEEKS.get(league_name, 38)
    team_re = re.compile(r'^[A-Z]{2,4}$')
    val_re = re.compile(r'^(\d{1,2}\.\d{2})$')
    week_label = ""
    pos = 1
    assigned = set()
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if line == '===SNAPSHOT===':
            week_label = ""
            pos = 1
            i += 1
            continue
        wm = re.search(r'Week\s*(\d+)\s*[-–]\s*(\w+)', line)
        if wm and wm.group(2).lower() == league_name.lower():
            week_label = f"Week {wm.group(1)} - {wm.group(2)}"
            pos = 1
            i += 1
            continue
        wm2 = re.search(r'(?:Football League[:\s]*)?(\w+)\s+Week\s*(\d+)', line)
        if wm2 and wm2.group(1).lower() == league_name.lower():
            week_label = f"Week {wm2.group(2)} - {wm2.group(1)}"
            pos = 1
            i += 1
            continue
        rn = re.match(r'^(\d+)\.$', line)
        if rn:
            if int(rn.group(1)) == 1 and pos > 1 and week_label:
                cw = re.search(r'Week\s*(\d+)', week_label)
                if cw:
                    week_label = f"Week {int(cw.group(1)) % total_w + 1} - {league_name}"
                    pos = 1
            i += 1
            continue
        # Row anchor: HOME / - / AWAY
        if (week_label and team_re.match(line) and i + 2 < n
                and lines[i + 1] == '-' and team_re.match(lines[i + 2])
                and line not in _GGNG_LABELS):
            home, away = line, lines[i + 2]
            vals = {}
            j = i + 3
            while j < n:
                l2 = lines[j]
                if (l2 == '===SNAPSHOT===' or re.match(r'^\d+\.$', l2)
                        or 'Week' in l2
                        or (team_re.match(l2) and j + 2 < n and lines[j + 1] == '-'
                            and l2 not in _GGNG_LABELS)):
                    break
                if l2 in _GGNG_LABELS and j + 1 < n:
                    vm = val_re.match(lines[j + 1])
                    if vm:
                        vals.setdefault(l2, float(vm.group(1)))
                        j += 2
                        continue
                j += 1
            key3 = (week_label, home, away)
            m = index.get(key3)
            gg, ng = vals.get("GG"), vals.get("NG")
            # Sanity: GG/NG implied probabilities must sum to ~1 plus margin,
            # or the layout isn't what we assume and we skip the whole row.
            # Double Chance is checked the same way against its complement
            # (1X pairs with 2, 12 with X, X2 with 1).
            if (m is not None and gg and ng and key3 not in assigned
                    and 1.0 <= (1 / gg + 1 / ng) <= 1.45):
                for lbl, field in _GGNG_FIELDS.items():
                    v = vals.get(lbl)
                    if v and 1.0 < v < 100:
                        m[field] = v
                for dc, comp in (("1X", "2"), ("12", "X"), ("X2", "1")):
                    a, b = vals.get(dc), vals.get(comp)
                    if not (a and b and 1.0 <= (1 / a + 1 / b) <= 1.45):
                        m.pop(_GGNG_FIELDS[dc], None)   # implausible — drop it
                assigned.add(key3)
            pos += 1
            i = j
            continue
        i += 1
    return len(assigned)


def _scrape_odds(frame, ts, fast=False, owner="scraper"):
    """Scrape upcoming odds for all leagues from the current frame."""
    saved = 0
    delay = 1 if fast else 8
    for li, league in enumerate(FOOTBALL_LEAGUES):
        _beat(owner)
        try:
            odds = _scrape_league_odds_direct(frame, league, fast=fast)
            if odds:
                fname = os.path.join(DATA_DIR, f"sportybet_odds_{league.lower()}_{ts}.json")
                with open(fname, "w", encoding="utf-8") as f:
                    json.dump(odds, f, indent=2, ensure_ascii=False)
                print(f"  [Scraper] Saved {len(odds)} {league} odds", flush=True)
                saved += 1
        except Exception as e:
            print(f"  [Scraper] Error scraping {league} odds: {e}", flush=True)
        if li < len(FOOTBALL_LEAGUES) - 1:
            time.sleep(delay)
    if saved:
        _health("odds_scrape")
    else:
        _health("odds_scrape", ok=False, error="0 leagues scraped this sweep")
    return saved


# ═══════════════════════════════════════════════════════════
#  RACING SCRAPING (upcoming races + incremental results)
# ═══════════════════════════════════════════════════════════

_RACE_SKIP_WORDS = {
    'Last Results', 'Performance', 'Rating', 'WIN', 'PLACE', 'SHOW',
    'Guide Price', 'EXACTA', 'QUINELLA', 'TRIFECTA', 'EVENODD', 'EVEN/ODD',
    'OVER/UNDER', 'OVERUNDER', 'Boxed', 'Any', 'Clear', 'Add to Betslip',
    'Go to all markets', 'Market filter', 'Win/Place/Show', 'Even/Odd',
    'Over/Under', 'Exacta/Quinella', 'Trifecta', 'EVEN', 'ODD', 'OVER',
    'UNDER', 'E', 'O', 'U',
}
_RACE_STATUS_WORDS = ('WATCH', 'WAITING', 'EXPIRED', 'NOW')


def _grab_race_blocks(frame):
    """Split page innerText into per-race blocks. Upcoming race headers look
    like '00:27 Greyhound Racing: Santa Monica #3264547' (countdown may be a
    separate line)."""
    return frame.evaluate(r"""() => {
        const lines = (document.body.innerText || '').split('\n').map(l => l.trim());
        const out = [];
        let cur = null;
        let lastTime = '';
        const HDR = /^(?:(\d{1,2}:\d{2})\s+)?([A-Za-z ]+Racing):\s*(.+?)\s*[-–]?\s*#(\d{5,})/;
        for (const line of lines) {
            if (!line) continue;
            if (/^\d{1,2}:\d{2}$/.test(line)) { lastTime = line; continue; }
            const hm = line.match(HDR);
            if (hm) {
                if (cur) out.push(cur);
                cur = { countdown: hm[1] || lastTime, sport: hm[2].trim(),
                        venue: hm[3].trim(), race_id: hm[4], lines: [] };
                lastTime = '';
                continue;
            }
            if (cur) cur.lines.push(line);
        }
        if (cur) out.push(cur);
        return out;
    }""")


def _parse_upcoming_runners(lines):
    """Parse a race block from the Win/Place/Show view into runners.
    Expected per runner: badge number, name, form tokens (digits/X),
    'NN%', 'N/5', then up to 3 odds floats (win, place, show)."""
    runners = []
    cur = None
    pending_badge = None
    for raw in lines:
        s = raw.strip()
        if not s or s in _RACE_SKIP_WORDS or s in _RACE_STATUS_WORDS:
            continue
        if re.fullmatch(r'\d{1,2}', s):
            v = int(s)
            if cur and cur.get("name") and not cur["odds"] and v <= 9:
                cur["form"].append(s)  # form token between name and odds
            elif v <= 20:
                if cur:
                    runners.append(cur)
                    cur = None
                pending_badge = str(v)
            continue
        if s == 'BETS CLOSED':
            continue
        if re.fullmatch(r'[Xx]', s):
            if cur:
                cur["form"].append('X')
            continue
        # Concatenated form run like 'XX3XX' or 'X1312' (must contain an X
        # so real names are never caught)
        if re.fullmatch(r'[Xx0-9]{2,8}', s) and re.search(r'[Xx]', s):
            if cur:
                cur["form"].append(s)
            continue
        if re.fullmatch(r'\d{1,3}%', s):
            if cur:
                cur["performance"] = s
            continue
        if re.fullmatch(r'\d/5', s):
            if cur:
                cur["rating"] = s
            continue
        if re.fullmatch(r'\d+\.\d+', s):
            if cur:
                cur["odds"].append(float(s))
            continue
        if re.match(r'^[A-Za-z]', s) and len(s) <= 40:
            if cur:
                runners.append(cur)
            cur = {"badge": pending_badge, "name": s, "form": [], "odds": []}
            pending_badge = None
    if cur:
        runners.append(cur)

    out = []
    for i, r in enumerate(runners):
        odds = r.pop("odds", [])
        r["win_odds"] = odds[0] if len(odds) >= 1 else None
        r["place_odds"] = odds[1] if len(odds) >= 2 else None
        r["show_odds"] = odds[2] if len(odds) >= 3 else None
        r["form"] = ' '.join(r.get("form", [])[:5])
        if not r.get("badge"):
            r["badge"] = str(i + 1)  # UI lists runners in badge order
        out.append(r)
    return out


def _extract_pair_odds(lines, label_a, label_b):
    """Find two race-level market odds, e.g. EVEN 1.62 / ODD 1.64 or
    'E: 1.62'-style."""
    txt = '\n'.join(lines)

    def find(label):
        m = re.search(rf'\b{label}\b\D{{0,4}}(\d+\.\d+)', txt, re.I)
        if m:
            return float(m.group(1))
        m = re.search(rf'\b{label[0]}[\s:.]+(\d+\.\d+)', txt)
        return float(m.group(1)) if m else None

    return find(label_a), find(label_b)


def _click_sport_submenu_link(frame, sport, link_text):
    """Click a link inside a racing sport's expanded sidebar submenu."""
    try:
        return frame.evaluate(r"""(args) => {
            const [sport, text] = args;
            const toggle = Array.from(document.querySelectorAll('a.toggler, a[class*="toggler"]'))
                             .find(a => a.textContent.trim() === sport);
            if (!toggle) return false;
            const li = toggle.closest('li');
            if (!li) return false;
            for (const a of li.querySelectorAll('a')) {
                if (a.textContent.trim() === text) {
                    const r = a.getBoundingClientRect();
                    if (r.height > 0 && r.width > 0) { a.click(); return true; }
                }
            }
            return false;
        }""", [sport, link_text])
    except Exception:
        return False


def _click_market_tab(frame, label):
    try:
        return frame.evaluate("""(label) => {
            for (const el of document.querySelectorAll('a, li, span, div, button')) {
                if (el.textContent.trim() === label
                        && el.offsetParent !== null && el.offsetWidth > 0) {
                    el.click();
                    return true;
                }
            }
            return false;
        }""", label) is True
    except Exception:
        return False


def _scrape_venue_upcoming(frame, sport, venue, fast=False, debug_dump=False):
    """Scrape upcoming races for one venue: runners with win/place/show
    odds plus race-level Even/Odd and Over/Under odds."""
    import random

    def _pause(lo=1000, hi=1800):
        if fast:
            lo, hi = int(lo * 0.6), int(hi * 0.6)
        time.sleep(random.randint(lo, hi) / 1000)

    if not _click_sport_submenu_link(frame, sport, venue):
        print(f"  [RaceOdds] Could not open {sport} / {venue}", flush=True)
        return []

    def _matching_blocks():
        all_blocks = _grab_race_blocks(frame) or []
        return [b for b in all_blocks
                if (b.get("venue") or "").strip().lower() == venue.strip().lower()]

    # Venue pages switch slowly — wait until race headers actually name
    # THIS venue (otherwise we'd parse the previous page's races)
    blocks = []
    for _attempt in range(10):
        _pause(900, 1400)
        blocks = _matching_blocks()
        if blocks:
            break
    if not blocks:
        print(f"  [RaceOdds] {sport}/{venue}: page did not load this venue, skipping", flush=True)
        return []

    # Ensure the default runner-odds view, then re-grab
    if _click_market_tab(frame, "Win/Place/Show"):
        _pause(800, 1400)
        blocks = _matching_blocks() or blocks

    if debug_dump:
        try:
            with open("debug_racing_upcoming.txt", "w", encoding="utf-8") as df:
                df.write(frame.evaluate("() => document.body.innerText") or "")
        except Exception:
            pass

    races = {}
    order = []
    for b in blocks:
        rid = b.get("race_id")
        if not rid or rid in races:
            continue
        status = next((w for w in _RACE_STATUS_WORDS
                       if any(w == l for l in b.get("lines", []))), "")
        runners = _parse_upcoming_runners(b.get("lines", []))
        if len(runners) < 2:
            continue
        races[rid] = {
            "race_id": rid,
            "sport": sport,
            "venue": venue,
            "countdown": b.get("countdown", ""),
            "status": status,
            "runners": runners,
            "scraped_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        order.append(rid)

    if not races:
        print(f"  [RaceOdds] {sport}/{venue}: no upcoming races parsed", flush=True)
        return []

    # Race-level markets from the other filter tabs
    for tab, la, lb, ka, kb in (
        ("Even/Odd", "EVEN", "ODD", "eo_odds_even", "eo_odds_odd"),
        ("Over/Under", "OVER", "UNDER", "ou_odds_over", "ou_odds_under"),
    ):
        if not _click_market_tab(frame, tab):
            continue
        _pause(800, 1400)
        for b in (_grab_race_blocks(frame) or []):
            rid = b.get("race_id")
            if rid in races:
                a, bb = _extract_pair_odds(b.get("lines", []), la, lb)
                if a is not None:
                    races[rid][ka] = a
                if bb is not None:
                    races[rid][kb] = bb

    _click_market_tab(frame, "Win/Place/Show")
    print(f"  [RaceOdds] {sport}/{venue}: {len(order)} upcoming races", flush=True)
    return [races[r] for r in order]


def _scrape_racing_upcoming(frame, racing_mod, fast=False):
    """Scrape upcoming races for every venue of every racing sport."""
    upcoming = {}
    first = True
    for sport in RACING_SPORTS:
        try:
            if not racing_mod.expand_racing_sport(frame, sport):
                continue
        except Exception as e:
            print(f"  [RaceOdds] expand {sport} failed: {e}", flush=True)
            continue
        try:
            venues = frame.evaluate(r"""(sport) => {
                const toggle = Array.from(document.querySelectorAll('a.toggler, a[class*="toggler"]'))
                                 .find(a => a.textContent.trim() === sport);
                if (!toggle) return [];
                const li = toggle.closest('li');
                if (!li) return [];
                const names = [];
                for (const a of li.querySelectorAll('a')) {
                    const t = a.textContent.trim();
                    if (t && t !== sport && t !== 'Results History' && t !== 'Upcoming') {
                        names.push(t);
                    }
                }
                return names;
            }""", sport) or []
        except Exception:
            venues = []
        for venue in venues:
            _beat("poller")
            try:
                races = _scrape_venue_upcoming(frame, sport, venue,
                                               fast=fast, debug_dump=first)
                first = False
                if races:
                    key = f"{sport} — {venue}"
                    upcoming[key] = races
                    # Push this venue live immediately — don't make the UI
                    # wait for the rest of the ~13-venue sweep
                    try:
                        refresh_racing_predictions({key: races})
                    except Exception as e:
                        print(f"  [RaceOdds] refresh {key} error: {e}", flush=True)
            except Exception as e:
                print(f"  [RaceOdds] {sport}/{venue} error: {e}", flush=True)
    return upcoming


def _scrape_racing_results_light(frame, racing_mod, ts):
    """Incremental racing results: visible page + a couple of Load More
    clicks per sport — enough to capture races since the last cycle."""
    racing_saved = 0
    for sport in RACING_SPORTS:
        _beat("scraper")
        try:
            if not racing_mod.navigate_to_racing_results_history(frame, sport):
                continue
            races = racing_mod.scrape_racing_page(frame, sport)
            known = {r.get("race_id") for r in races}
            for _ in range(2):
                if not racing_mod.click_load_more(frame):
                    break
                for r in racing_mod.scrape_racing_page(frame, sport):
                    if r.get("race_id") not in known:
                        known.add(r.get("race_id"))
                        races.append(r)
            if races:
                slug = sport.lower().replace(" ", "_")
                fname = os.path.join(DATA_DIR, f"sportybet_racing_{slug}_{ts}.json")
                with open(fname, "w", encoding="utf-8") as f:
                    json.dump(races, f, indent=2, ensure_ascii=False)
                print(f"  [Scraper] Saved {len(races)} {sport} results", flush=True)
                racing_saved += 1
        except Exception as e:
            print(f"  [Scraper] {sport} results error: {e}", flush=True)
    if racing_saved:
        _health("racing_results")
    else:
        _health("racing_results", ok=False, error="0 sports returned racing results")


def _try_full_scrape():
    """Full scrape: 4 seasons of results per league + racing history + odds.
    Runs when history is missing."""
    try:
        results_mod, odds_mod, racing_mod = _import_scraper_modules()
        os.makedirs(DATA_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        result = _get_browser_and_frame(results_mod)
        if not result:
            print("  [Scraper] Virtual iframe not found after 3 attempts", flush=True)
            return

        pw, browser, ctx, page, frame = result
        _register_driver("scraper", pw)
        try:
            print("  [Scraper] Full scrape: 4 seasons per league...", flush=True)
            done = set()
            for li, league in enumerate(FOOTBALL_LEAGUES):
                _beat("scraper")
                try:
                    matches, _ = results_mod.scrape_league_results(
                        frame, league, target_seasons=4,
                        first_league=(li == 0), detail_seasons=0,
                    )
                    if matches:
                        fname = os.path.join(DATA_DIR, f"sportybet_results_{league.lower()}_{ts}.json")
                        with open(fname, "w", encoding="utf-8") as f:
                            json.dump(matches, f, indent=2, ensure_ascii=False)
                        print(f"  [Scraper] Saved {len(matches)} {league} matches", flush=True)
                        done.add(league)
                except Exception as e:
                    print(f"  [Scraper] Error scraping {league}: {e}", flush=True)
                if li < len(FOOTBALL_LEAGUES) - 1:
                    time.sleep(10)

            # Second pass: leagues that got nothing (the first league often
            # runs while the page is still settling, so e.g. England can
            # come up empty on a fresh install)
            for league in [lg for lg in FOOTBALL_LEAGUES if lg not in done]:
                _beat("scraper")
                print(f"  [Scraper] Retrying {league} (0 matches on first pass)", flush=True)
                try:
                    matches, _ = results_mod.scrape_league_results(
                        frame, league, target_seasons=4,
                        first_league=True, detail_seasons=0,
                    )
                    if matches:
                        fname = os.path.join(DATA_DIR, f"sportybet_results_{league.lower()}_{ts}.json")
                        with open(fname, "w", encoding="utf-8") as f:
                            json.dump(matches, f, indent=2, ensure_ascii=False)
                        print(f"  [Scraper] Saved {len(matches)} {league} matches (retry)", flush=True)
                except Exception as e:
                    print(f"  [Scraper] Retry error for {league}: {e}", flush=True)

            # Racing history: one full day per sport (Load More until exhausted)
            for sport in RACING_SPORTS:
                _beat("scraper")
                try:
                    races = racing_mod.scrape_sport_results(frame, sport, target_days=1)
                    if races:
                        slug = sport.lower().replace(" ", "_")
                        fname = os.path.join(DATA_DIR, f"sportybet_racing_{slug}_{ts}.json")
                        with open(fname, "w", encoding="utf-8") as f:
                            json.dump(races, f, indent=2, ensure_ascii=False)
                        print(f"  [Scraper] Saved {len(races)} {sport} results", flush=True)
                except Exception as e:
                    print(f"  [Scraper] Error scraping {sport}: {e}", flush=True)

            # Reload page for odds
            print("  [Scraper] Reloading page for odds scraping...", flush=True)
            try:
                page.goto(results_mod.SITE_URL, timeout=90000, wait_until="domcontentloaded")
            except Exception as nav_err:
                print(f"  [Scraper] Page reload slow, continuing: {nav_err}", flush=True)
            time.sleep(15)
            try:
                frame = results_mod.wait_and_find_virtual_frame(page, timeout=60000)
            except Exception:
                frame = page
            is_real = (frame is not page and hasattr(frame, 'url') and frame.url
                       and "sportybet.com/ng/virtual" not in frame.url
                       and frame.url != "about:blank")
            if is_real and _frame_login_error(frame):
                _health("virtual_session", ok=False,
                        error="CORE-1002 login error (odds phase) — skipped, next cycle retries")
                is_real = False
            if is_real:
                _scrape_odds(frame, ts)
            else:
                print("  [Scraper] Virtual iframe not found for odds phase", flush=True)

        finally:
            _unregister_driver("scraper")
            _safe_cleanup(pw, browser)

    except Exception as e:
        print(f"  [Scraper] Full scrape failed: {e}", flush=True)


def _try_quick_scrape():
    """Incremental scrape: only the latest ~3 gameweeks of results per league
    + odds. The initial full scrape already covers deep history; these light
    passes just append the newly finished GWs (deduped on load) so results,
    accuracy and predictions stay current. Runs every 5 mins."""
    try:
        results_mod, odds_mod, racing_mod = _import_scraper_modules()
        os.makedirs(DATA_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        result = _get_browser_and_frame(results_mod)
        if not result:
            print("  [Scraper] Virtual iframe not found after 3 attempts", flush=True)
            return

        pw, browser, ctx, page, frame = result
        _register_driver("scraper", pw)
        try:
            print("  [Scraper] Quick scrape: latest GWs per league + odds...", flush=True)
            res_saved = 0
            missing = []
            for li, league in enumerate(FOOTBALL_LEAGUES):
                _beat("scraper")
                try:
                    matches, _ = results_mod.scrape_league_results(
                        frame, league, target_seasons=1,
                        first_league=(li == 0), detail_seasons=0,
                        target_matches=LEAGUE_TEAM_COUNT.get(league, 20) // 2 * 3,
                    )
                    if matches:
                        fname = os.path.join(DATA_DIR, f"sportybet_results_{league.lower()}_{ts}.json")
                        with open(fname, "w", encoding="utf-8") as f:
                            json.dump(matches, f, indent=2, ensure_ascii=False)
                        print(f"  [Scraper] Saved {len(matches)} {league} matches (quick)", flush=True)
                        res_saved += 1
                    else:
                        missing.append(league)
                except Exception as e:
                    print(f"  [Scraper] Error scraping {league}: {e}", flush=True)
                    missing.append(league)
                if li < len(FOOTBALL_LEAGUES) - 1:
                    time.sleep(5)

            # One retry pass for leagues that came up empty
            for league in missing:
                _beat("scraper")
                print(f"  [Scraper] Retrying {league} (0 matches on first pass)", flush=True)
                try:
                    matches, _ = results_mod.scrape_league_results(
                        frame, league, target_seasons=1,
                        first_league=True, detail_seasons=0,
                        target_matches=LEAGUE_TEAM_COUNT.get(league, 20) // 2 * 3,
                    )
                    if matches:
                        fname = os.path.join(DATA_DIR, f"sportybet_results_{league.lower()}_{ts}.json")
                        with open(fname, "w", encoding="utf-8") as f:
                            json.dump(matches, f, indent=2, ensure_ascii=False)
                        print(f"  [Scraper] Saved {len(matches)} {league} matches (retry)", flush=True)
                        res_saved += 1
                except Exception as e:
                    print(f"  [Scraper] Retry error for {league}: {e}", flush=True)
            if res_saved:
                _health("results_scrape")
            else:
                _health("results_scrape", ok=False,
                        error="0 leagues returned results this cycle "
                              "(site navigation failing — see debug dumps)")

            # Latest racing results (visible + 2 Load More per sport)
            _scrape_racing_results_light(frame, racing_mod, ts)

            # Reload for odds
            print("  [Scraper] Reloading page for odds scraping...", flush=True)
            try:
                page.goto(results_mod.SITE_URL, timeout=90000, wait_until="domcontentloaded")
            except Exception:
                pass
            time.sleep(15)
            try:
                frame = results_mod.wait_and_find_virtual_frame(page, timeout=60000)
            except Exception:
                frame = page
            is_real = (frame is not page and hasattr(frame, 'url') and frame.url
                       and "sportybet.com/ng/virtual" not in frame.url
                       and frame.url != "about:blank")
            if is_real and _frame_login_error(frame):
                _health("virtual_session", ok=False,
                        error="CORE-1002 login error (odds phase) — skipped, next cycle retries")
                is_real = False
            if is_real:
                _scrape_odds(frame, ts)
            else:
                print("  [Scraper] Virtual iframe not found for odds phase", flush=True)

        finally:
            _unregister_driver("scraper")
            _safe_cleanup(pw, browser)

    except Exception as e:
        print(f"  [Scraper] Quick scrape failed: {e}", flush=True)


ODDS_POLL_INTERVAL = 20


def refresh_odds_and_predictions():
    """Lightweight refresh: reload odds files, update current GW + predictions.
    Does NOT rebuild Poisson models - just overlays new odds on existing models."""
    upcoming_odds = load_upcoming_odds()
    if not upcoming_odds:
        return

    with state_lock:
        football_models = state.get("football_models", {})
        football_upcoming = dict(state.get("football_upcoming", {}))
        football_upcoming_improved = dict(state.get("football_upcoming_improved", {}))
        cur_weeks = dict(state.get("current_weeks", {}))

    changed = False

    for league, odds_list in upcoming_odds.items():
        if not odds_list or league not in football_models:
            continue
        # Lists are ordered current-week-first by load_upcoming_odds
        new_week = _week_num(odds_list[0])
        if new_week is not None and new_week != cur_weeks.get(league):
            print(f"  [OddsPoll] {league}: GW changed {cur_weeks.get(league)} -> {new_week}", flush=True)
            cur_weeks[league] = new_week
            changed = True

        preds = predict_upcoming_with_model(
            football_models[league], odds_list, league
        )
        if preds:
            # odds_list is already merged across recent scrapes, so replace
            # wholesale — keeping old state here would resurrect played weeks
            football_upcoming[league] = preds
            try:
                save_prediction_snapshots(league, preds)
            except Exception as e:
                print(f"  [Accuracy] Error saving snapshots for {league}: {e}", flush=True)
            # Record this GW's banker tickets so their outcome is tracked
            try:
                cur_wk = preds[0].get("week")
                cur_matches = [p for p in preds if p.get("week") == cur_wk]
                single, two, three = _gw_bankers(cur_matches)
                snapshot_gw_bankers(league, cur_wk, single, two, three)
            except Exception as e:
                print(f"  [Bankers] Snapshot error for {league}: {e}", flush=True)

            adjustments = load_adjustments(league)
            calibration = load_calibration(league)
            improved_list = []
            for pred in preds:
                imp = improve_prediction(
                    football_models[league], pred, adjustments, calibration
                )
                if imp:
                    improved_list.append(imp)
            if improved_list:
                football_upcoming_improved[league] = improved_list

    with state_lock:
        state["football_upcoming"] = football_upcoming
        state["football_upcoming_improved"] = football_upcoming_improved
        state["upcoming_odds_raw"] = upcoming_odds
        state["current_weeks"] = cur_weeks
        state["last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if changed:
        upcoming_count = sum(len(v) for v in football_upcoming.values())
        print(f"  [OddsPoll] Predictions refreshed: {upcoming_count} total", flush=True)

    # Live forward-test: snapshot what GLOBAL and NEXTUP would bet right now, so
    # the record accumulates from the moment the app runs (deduped by match).
    try:
        import pattern_lab, cc_forward
        d = pattern_lab.build_bet_picks(football_upcoming, gw_window=3, top_n=1)
        cc_forward.record({"global": d.get("confident_call"),
                           "nextup": d.get("nearest_call")})
    except Exception:
        pass


def refresh_racing_predictions(racing_upcoming):
    """Recompute racing predictions from freshly scraped upcoming races and
    snapshot them for accuracy tracking."""
    with state_lock:
        racing_models = state.get("racing_models", {})
        racing_predictions = dict(state.get("racing_predictions", {}))

    preds = predict_upcoming_races(racing_models, racing_upcoming)
    for key, race_preds in preds.items():
        try:
            save_racing_snapshots(key, race_preds)
        except Exception as e:
            print(f"  [RaceAccuracy] Snapshot error for {key}: {e}", flush=True)
        if key in racing_predictions:
            racing_predictions[key] = dict(racing_predictions[key])
            racing_predictions[key]["races"] = race_preds
        else:
            model = racing_models.get(key) or _racing_model_stub(
                racing_upcoming.get(key, []))
            racing_predictions[key] = {"model": model, "races": race_preds}

    with state_lock:
        # Merge — this may be a single-venue incremental update, and
        # rebuild_models re-predicts from the accumulated raw dict
        merged_raw = dict(state.get("racing_upcoming_raw", {}))
        merged_raw.update(racing_upcoming)
        state["racing_upcoming_raw"] = merged_raw
        state["racing_predictions"] = racing_predictions
        state["last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ═══════════════════════════════════════════════════════════
#  WATCHDOG — self-healing for hung Playwright calls
# ═══════════════════════════════════════════════════════════
# frame.evaluate() has no timeout: if the virtual-games page wedges (or the
# laptop sleeps mid-call), the call never returns and its thread freezes
# forever. Each scraping thread registers its Playwright driver PID and
# heartbeats as it progresses; when a heartbeat goes stale the watchdog kills
# that driver's process tree, which makes the pending call raise so the
# thread's own reconnect logic takes over.

WATCHDOG_TIMEOUTS = {"poller": 8 * 60, "scraper": 12 * 60}
_watchdog = {name: {"beat": time.time(), "pid": None} for name in WATCHDOG_TIMEOUTS}
_watchdog_lock = threading.Lock()


def _beat(owner):
    with _watchdog_lock:
        if owner in _watchdog:
            _watchdog[owner]["beat"] = time.time()


def _driver_pid(pw):
    """Resolve the node driver subprocess PID of a sync Playwright instance."""
    for path in ("_impl_obj._connection._transport._proc.pid",
                 "_connection._transport._proc.pid"):
        obj = pw
        try:
            for attr in path.split('.'):
                obj = getattr(obj, attr)
            return int(obj)
        except Exception:
            continue
    return None


def _register_driver(owner, pw):
    pid = _driver_pid(pw)
    with _watchdog_lock:
        _watchdog[owner]["pid"] = pid
        _watchdog[owner]["beat"] = time.time()
    if pid is None:
        print(f"  [Watchdog] Could not resolve driver PID for {owner} — "
              f"hang protection inactive for this browser session", flush=True)


def _unregister_driver(owner):
    with _watchdog_lock:
        _watchdog[owner]["pid"] = None
        _watchdog[owner]["beat"] = time.time()


def watchdog_thread():
    import subprocess
    while True:
        time.sleep(30)
        now = time.time()
        for owner, timeout in WATCHDOG_TIMEOUTS.items():
            with _watchdog_lock:
                pid = _watchdog[owner]["pid"]
                stalled = now - _watchdog[owner]["beat"]
            if pid is None or stalled < timeout:
                continue
            print(f"  [Watchdog] {owner} made no progress for {stalled/60:.1f} min — "
                  f"killing its browser (driver pid {pid}) to force recovery", flush=True)
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=30)
            except Exception as e:
                print(f"  [Watchdog] taskkill failed: {e}", flush=True)
            _unregister_driver(owner)


_RESPAWN = {"n": 0}


def background_odds_poller():
    """Separate thread: keeps browser open, polls football odds and racing
    upcoming races continuously. On asyncio-poisoning after a killed browser it
    respawns itself in a fresh thread (see the create_stealth_browser guard)."""
    time.sleep(10)
    print("  [OddsPoll] Odds poller thread started", flush=True)

    while True:
        pw = browser = ctx = page = frame = None
        try:
            results_mod, _, racing_mod = _import_scraper_modules()
            os.makedirs(DATA_DIR, exist_ok=True)

            _fresh_event_loop()
            try:
                pw, browser, ctx, page = results_mod.create_stealth_browser(
                    headless=True, proxy_server=None, stealth_mode="advanced"
                )
            except Exception as ce:
                if "asyncio loop" in str(ce) or "Sync API" in str(ce):
                    # This thread's asyncio state was poisoned by a watchdog-
                    # killed driver and _fresh_event_loop couldn't clear it —
                    # the exact bug that freezes the poller at a stale gameweek.
                    # A BRAND-NEW thread has clean asyncio state, so hand off to
                    # one and let this poisoned thread die.
                    print("  [OddsPoll] asyncio-poisoned thread after a killed "
                          "browser — respawning poller in a fresh thread", flush=True)
                    _safe_cleanup(pw, browser)
                    _RESPAWN["n"] += 1
                    delay = 60 if _RESPAWN["n"] % 6 == 0 else 8
                    threading.Thread(
                        target=lambda: (time.sleep(delay), background_odds_poller()),
                        daemon=True).start()
                    return
                raise
            _register_driver("poller", pw)
            try:
                page.goto(results_mod.SITE_URL, timeout=60000, wait_until="domcontentloaded")
            except Exception:
                pass
            time.sleep(12)
            frame = results_mod.wait_and_find_virtual_frame(page, timeout=45000)
            is_real = (frame is not page and hasattr(frame, 'url') and frame.url
                       and "sportybet.com/ng/virtual" not in frame.url
                       and frame.url != "about:blank")
            if is_real and _frame_login_error(frame):
                print("  [Health] Poller session rejected (CORE-1002 login error) — "
                      "fresh browser in 30s", flush=True)
                _health("virtual_session", ok=False,
                        error="CORE-1002 login error (poller) — reconnecting")
                is_real = False
            if not is_real:
                print(f"  [OddsPoll] Virtual iframe not found, retrying in 30s", flush=True)
                _unregister_driver("poller")
                _safe_cleanup(pw, browser)
                time.sleep(30)
                continue
            _health("virtual_session")

            poll_count = 0
            empty_polls = 0
            while True:
                poll_count += 1
                _beat("poller")
                try:
                    frame.evaluate("() => 1")
                except Exception:
                    print(f"  [OddsPoll] Browser connection lost after {poll_count} polls, reconnecting...", flush=True)
                    break

                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                _prune_old_odds_files()   # self-throttled to every ~10 min

                # Alternate which half goes first so neither football nor
                # racing is always a full sweep (~several minutes) stale
                phases = ("racing", "football") if poll_count % 2 == 1 \
                    else ("football", "racing")
                got_anything = False
                for phase in phases:
                    if phase == "football":
                        saved = _scrape_odds(frame, ts, fast=True, owner="poller")
                        if saved > 0:
                            got_anything = True
                            print(f"  [OddsPoll] Poll #{poll_count}: got odds for {saved} leagues", flush=True)
                            refresh_odds_and_predictions()
                        else:
                            print(f"  [OddsPoll] Poll #{poll_count}: no odds scraped", flush=True)
                    else:
                        # Racing upcoming races — predictions are refreshed
                        # per venue inside the sweep
                        try:
                            racing_up = _scrape_racing_upcoming(frame, racing_mod, fast=True)
                            if racing_up:
                                got_anything = True
                                n_races = sum(len(v) for v in racing_up.values())
                                print(f"  [RacePoll] {n_races} upcoming races across "
                                      f"{len(racing_up)} venues", flush=True)
                                _health("racing_upcoming")
                            else:
                                _health("racing_upcoming", ok=False,
                                        error="0 venues scraped this sweep")
                        except Exception as e:
                            print(f"  [RacePoll] Failed: {e}", flush=True)
                            _health("racing_upcoming", ok=False, error=str(e)[:120])

                # The virtual iframe can silently break (it navigates itself
                # mid-sweep and stops responding to clicks) while still
                # answering evaluate() — so an all-empty poll counts as a
                # failure. Escalate: reload the page after 2 in a row, tear
                # the whole browser down after 4.
                if got_anything:
                    empty_polls = 0
                else:
                    empty_polls += 1
                    if empty_polls >= 4:
                        print(f"  [OddsPoll] {empty_polls} empty polls — page reload didn't help, "
                              f"restarting with a fresh browser", flush=True)
                        break
                    if empty_polls >= 2:
                        print(f"  [OddsPoll] {empty_polls} empty polls — reloading virtual page", flush=True)
                        try:
                            page.goto(results_mod.SITE_URL, timeout=60000,
                                      wait_until="domcontentloaded")
                            time.sleep(12)
                            frame = results_mod.wait_and_find_virtual_frame(page, timeout=45000)
                            is_real = (frame is not page and hasattr(frame, 'url') and frame.url
                                       and "sportybet.com/ng/virtual" not in frame.url
                                       and frame.url != "about:blank")
                            if not is_real:
                                print("  [OddsPoll] Iframe missing after reload — fresh browser", flush=True)
                                break
                        except Exception as e:
                            print(f"  [OddsPoll] Page reload failed ({e}) — fresh browser", flush=True)
                            break

                time.sleep(ODDS_POLL_INTERVAL)

        except Exception as e:
            print(f"  [OddsPoll] Failed: {e}", flush=True)
            _health("odds_scrape", ok=False, error=str(e)[:120])
        finally:
            _unregister_driver("poller")
            _safe_cleanup(pw, browser)

        time.sleep(5)


_PRUNE = {"t": 0}


def _prune_old_odds_files(max_age_hours=2, min_interval=600):
    """Delete odds snapshot files older than max_age_hours. Only the last ~15
    minutes are ever read, so a 2h window is ample; letting these pile up (826
    were found once) slows every page load that globs them. Self-throttles so
    it can be called cheaply from the poll loop."""
    now = time.time()
    if now - _PRUNE["t"] < min_interval:
        return
    _PRUNE["t"] = now
    cutoff = datetime.now() - timedelta(hours=max_age_hours)
    removed = 0
    for f in glob.glob(os.path.join(DATA_DIR, "sportybet_odds_*.json")):
        m = re.search(r'_(\d{8}_\d{6})\.json$', os.path.basename(f))
        if not m:
            continue
        try:
            if datetime.strptime(m.group(1), "%Y%m%d_%H%M%S") < cutoff:
                os.remove(f)
                removed += 1
        except (ValueError, OSError):
            pass
    if removed:
        print(f"  [Scraper] Pruned {removed} odds files older than {max_age_hours}h", flush=True)


def _history_insufficient():
    """True if any league lacks ~1 season of results — only then is the
    heavy multi-season scrape worth its ~20 min runtime."""
    with state_lock:
        counts = dict(state.get("history_counts", {}))
    return any(counts.get(lg, 0) < 300 for lg in FOOTBALL_LEAGUES)


def background_scraper():
    """Separate thread: heavy scrape only if history is missing, then light
    incremental scrapes (latest GWs only) every 5 min — the initial heavy
    scrape covers the rest."""
    time.sleep(2)

    # Load existing data first so the app is usable immediately
    rebuild_models()

    if _history_insufficient():
        with state_lock:
            state["scraper_status"] = "Full scrape (initial history)..."
            state["scraper_running"] = True
        print(f"\n  [Scraper] Initial FULL scrape at {datetime.now()}", flush=True)
        _try_full_scrape()
        print("  [Scraper] Rebuilding models with fresh data...", flush=True)
        rebuild_models()
    else:
        print("  [Scraper] History sufficient — skipping heavy scrape, "
              "going straight to incremental updates", flush=True)
        with state_lock:
            state["scraper_status"] = "Quick scrape (update)..."
            state["scraper_running"] = True
        print(f"\n  [Scraper] QUICK scrape at {datetime.now()}", flush=True)
        _try_quick_scrape()
        rebuild_models()

    while True:
        with state_lock:
            state["scraper_status"] = "Waiting..."
            state["scraper_running"] = False

        time.sleep(SCRAPE_INTERVAL)

        _prune_old_odds_files()

        if _history_insufficient():
            with state_lock:
                state["scraper_status"] = "Full scrape (history refill)..."
                state["scraper_running"] = True
            print(f"\n  [Scraper] FULL scrape (history refill) at {datetime.now()}", flush=True)
            _try_full_scrape()
        else:
            with state_lock:
                state["scraper_status"] = "Quick scrape (update)..."
                state["scraper_running"] = True
            print(f"\n  [Scraper] QUICK scrape at {datetime.now()}", flush=True)
            _try_quick_scrape()

        print("  [Scraper] Rebuilding models...", flush=True)
        rebuild_models()
        _health("models_rebuild")
        _print_health()


# ═══════════════════════════════════════════════════════════
#  BANKERS — verified high-hit-rate plays from the eval logs
# ═══════════════════════════════════════════════════════════

_banker_cache = {"ts": 0.0, "rates": None}


def _banker_verified_rates():
    """Live-verified hit rates of the banker play types, recomputed from the
    eval logs at most every 30 minutes."""
    if _banker_cache["rates"] and time.time() - _banker_cache["ts"] < 1800:
        return _banker_cache["rates"]
    stats = {"ou60": [0, 0], "ou55": [0, 0], "fav145": [0, 0], "btts55": [0, 0]}
    for f in glob.glob(os.path.join(ACCURACY_DIR, "eval_log_*.jsonl")):
        try:
            with open(f, encoding="utf-8") as fp:
                for line in fp:
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    hs, aws = r.get("actual_home"), r.get("actual_away")
                    if hs is None or aws is None:
                        continue
                    # O/U 2.5 call by confidence
                    p = r.get("over_2_5_pct")
                    if p is not None:
                        conf = max(p, 100 - p)
                        hit = (p > 50) == ((hs + aws) > 2.5)
                        if conf >= 60:
                            stats["ou60"][0] += hit
                            stats["ou60"][1] += 1
                        elif conf >= 55:
                            stats["ou55"][0] += hit
                            stats["ou55"][1] += 1
                    # BTTS call by confidence
                    b = r.get("btts_pct")
                    if b is not None and max(b, 100 - b) >= 55:
                        hit = (b > 50) == ((hs > 0) and (aws > 0))
                        stats["btts55"][0] += hit
                        stats["btts55"][1] += 1
                    # Strong favorite: called side's odds <= 1.45
                    o = {"Home Win": r.get("home_odds"),
                         "Draw": r.get("draw_odds"),
                         "Away Win": r.get("away_odds")}.get(r.get("prediction"))
                    if o and 1.0 < o <= 1.45:
                        stats["fav145"][0] += (r.get("prediction") == r.get("actual_outcome"))
                        stats["fav145"][1] += 1
        except Exception:
            pass
    rates = {k: {"hit": round(h / n * 100, 1) if n else None, "n": n}
             for k, (h, n) in stats.items()}
    _banker_cache["rates"] = rates
    _banker_cache["ts"] = time.time()
    return rates


def _collect_bankers():
    """Scan current upcoming predictions for the verified play types and
    build ~2-odds singles and ~3-odds two-leg tickets."""
    rates = _banker_verified_rates()
    singles = []
    cur_weeks = state.get("current_weeks", {})
    for lg in FOOTBALL_LEAGUES:
        cur = cur_weeks.get(lg, 1)
        total_w = LEAGUE_WEEKS.get(lg, 38)
        for p in state.get("football_upcoming", {}).get(lg, []):
            wk = p.get("week")
            wdist = ((wk - cur) % total_w) if wk else 99
            base = {"league": lg, "week": wk, "week_dist": wdist,
                    "home": p.get("home_team"), "away": p.get("away_team")}

            ou_p = p.get("over_2_5_pct", 50)
            conf = max(ou_p, 100 - ou_p)
            call_over = ou_p > 50
            odds = p.get("live_over_25") if call_over else p.get("live_under_25")
            tier = "ou60" if conf >= 60 else ("ou55" if conf >= 55 else None)
            if tier and rates[tier]["n"] >= 50:
                singles.append({**base, "market": "Over/Under 2.5",
                                "call": "OVER 2.5" if call_over else "UNDER 2.5",
                                "conf": conf, "odds": odds,
                                "verified": rates[tier]["hit"],
                                "verified_n": rates[tier]["n"]})

            co = {"Home Win": p.get("live_home_odds"),
                  "Away Win": p.get("live_away_odds")}.get(p.get("prediction"))
            if co and 1.0 < co <= 1.45 and rates["fav145"]["n"] >= 50:
                singles.append({**base, "market": "1X2 strong favorite",
                                "call": p["prediction"],
                                "conf": p.get("confidence"), "odds": co,
                                "verified": rates["fav145"]["hit"],
                                "verified_n": rates["fav145"]["n"]})

            bt = p.get("btts_pct", 50)
            if max(bt, 100 - bt) >= 55 and rates["btts55"]["n"] >= 50:
                singles.append({**base, "market": "BTTS",
                                "call": "BTTS YES" if bt > 50 else "BTTS NO",
                                "conf": max(bt, 100 - bt), "odds": None,
                                "verified": rates["btts55"]["hit"],
                                "verified_n": rates["btts55"]["n"]})

    singles.sort(key=lambda s: (-(s["verified"] or 0), s["week_dist"], -(s["conf"] or 0)))

    # ~3-odds tickets: pair two odds-carrying singles from different matches
    with_odds = [s for s in singles if s.get("odds") and s["odds"] > 1][:14]
    tickets = []
    for i in range(len(with_odds)):
        for j in range(i + 1, len(with_odds)):
            a, b = with_odds[i], with_odds[j]
            if (a["league"], a["home"], a["week"]) == (b["league"], b["home"], b["week"]):
                continue
            total = round(a["odds"] * b["odds"], 2)
            if 2.4 <= total <= 3.8:
                joint = round((a["verified"] / 100) * (b["verified"] / 100) * 100, 1)
                tickets.append({"legs": [a, b], "total_odds": total,
                                "joint_pct": joint})
    tickets.sort(key=lambda t: -t["joint_pct"])
    return rates, singles, tickets[:5]


def _leg_p(tier_hit_pct, odds):
    """Honest per-leg probability: never above the market-implied probability
    (margin-adjusted). Backtest showed tier rates alone overstate tickets."""
    implied = (1.0 / odds) / 1.05 if odds and odds > 1 else 0.5
    if tier_hit_pct is None:
        return round(implied, 4)
    return round(min(tier_hit_pct / 100.0, implied), 4)


def _over_signal_count(m):
    """How many secondary signals agree that this match is a GOAL-FEST, for
    an Over 2.5 pick: BTTS-yes, expected goals >=3.0, expected goals >=3.3.
    0-3. Backtested (100k picks): a 2+ Over pick wins ~61% vs ~55% baseline —
    a real WIN-RATE lift (shorter losing streaks), NOT a price edge."""
    c = 0
    if (m.get("btts_pct") or 0) > 50:
        c += 1
    lam = (m.get("lambda_home") or 0) + (m.get("lambda_away") or 0)
    if lam >= 3.0:
        c += 1
    if lam >= 3.3:
        c += 1
    return c


def _gw_banker_legs(matches):
    """Candidate legs from one GW's matches. 'verified' legs come from the
    play types with measured hit rates; 'market' legs are fallbacks so a
    ticket can always be built."""
    rates = _banker_verified_rates()
    verified, market = [], []
    for m in matches:
        mk = (m.get("home_team"), m.get("away_team"))
        desc = f"{m.get('home_team')}—{m.get('away_team')}"

        co = {"Home Win": m.get("live_home_odds"),
              "Away Win": m.get("live_away_odds")}.get(m.get("prediction"))
        if co and co > 1:
            call = "1" if m["prediction"] == "Home Win" else "2"
            mkt = "1X2"
            if co <= 1.45 and rates["fav145"]["n"] >= 50:
                verified.append({"match": mk, "desc": desc, "call": call,
                                 "market": mkt, "conf": m.get("confidence"),
                                 "odds": co,
                                 "p": _leg_p(rates["fav145"]["hit"], co)})
            elif co <= 1.75:
                market.append({"match": mk, "desc": desc, "call": call,
                               "market": mkt, "conf": m.get("confidence"),
                               "odds": co, "p": _leg_p(None, co)})

        ou_p = m.get("over_2_5_pct", 50)
        conf = max(ou_p, 100 - ou_p)
        odds = m.get("live_over_25") if ou_p > 50 else m.get("live_under_25")
        # signals only apply to OVER picks (goal-fest agreement)
        sig = _over_signal_count(m) if ou_p > 50 else 0
        if odds and odds > 1:
            call = "O2.5" if ou_p > 50 else "U2.5"
            mkt = "Over/Under 2.5"
            tier = "ou60" if conf >= 60 else ("ou55" if conf >= 55 else None)
            if tier and rates[tier]["n"] >= 50:
                verified.append({"match": mk, "desc": desc, "call": call,
                                 "market": mkt, "conf": conf, "odds": odds,
                                 "signals": sig,
                                 "p": _leg_p(rates[tier]["hit"], odds)})
            elif conf >= 52:
                market.append({"match": mk, "desc": desc, "call": call,
                               "market": mkt, "conf": conf, "signals": sig,
                               "odds": odds, "p": _leg_p(None, odds)})
    return verified, market


def _build_ticket(legs, lo, hi, exclude=frozenset()):
    import itertools
    pool = [l for l in legs if l["match"] not in exclude]
    pool.sort(key=lambda l: -l["p"])
    pool = pool[:10]
    best = None
    for r in (1, 2, 3, 4):
        for combo in itertools.combinations(pool, r):
            if len(set(l["match"] for l in combo)) < r:
                continue  # one leg per match
            tot = 1.0
            p = 1.0
            for l in combo:
                tot *= l["odds"]
                p *= l["p"]
            if lo <= tot <= hi and (best is None or p > best["praw"]):
                best = {"legs": list(combo), "total": round(tot, 2), "praw": p}
    return best


def _gw_bankers(matches, single_lo=1.60, single_hi=2.04, allowed_calls=None,
                min_signals=0):
    """Build three plays from ONE gameweek:
    - single: the martingale-safe pick — one verified leg in the single odds
      window (default 1.60-2.04; narrow it to raise the win rate),
    - two: ~2-odds ticket (2.05-2.90), strongest legs first,
    - three: ~3-odds ticket (3.05-3.90) from DIFFERENT matches.
    Falls back to market-priced legs so tickets exist nearly every GW
    (marked provisional).

    allowed_calls: optional whitelist for the SINGLE's call, e.g. {"O2.5"}.
    min_signals: if >0, an Over-2.5 single must have at least this many
      secondary signals agreeing (BTTS + expected goals) — the "upgraded
      single" (higher win rate, fewer bets). 0 = off.
    """
    verified, market = _gw_banker_legs(matches)

    def build(lo, hi, exclude):
        best = _build_ticket(verified, lo, hi, exclude)
        provisional = False
        if best is None:
            best = _build_ticket(verified + market, lo, hi, exclude)
            provisional = best is not None
        if best:
            best["p"] = round(best.pop("praw") * 100, 1)
            best["provisional"] = provisional
        return best

    # Martingale-safe single: best verified leg in the single odds window,
    # optionally restricted to certain calls and to goal-fest agreement
    def _ok(l):
        if not (single_lo <= l["odds"] <= single_hi):
            return False
        if allowed_calls and l["call"] not in allowed_calls:
            return False
        # signal filter only constrains OVER picks (it is a goal-fest test)
        if min_signals > 0 and l["call"] == "O2.5" \
                and l.get("signals", 0) < min_signals:
            return False
        return True

    single = None
    pool = [l for l in verified if _ok(l)]
    if not pool:
        pool = [l for l in market if _ok(l)]
    if pool:
        leg = max(pool, key=lambda l: l["p"])
        single = {"legs": [leg], "total": leg["odds"],
                  "p": round(leg["p"] * 100, 1),
                  "provisional": leg not in verified}

    used_s = frozenset(l["match"] for l in single["legs"]) if single else frozenset()
    two = build(2.05, 2.90, used_s)
    used = used_s | (frozenset(l["match"] for l in two["legs"]) if two else frozenset())
    three = build(3.05, 3.90, used)
    return single, two, three


def _gw_ng_pick(matches, min_odds=1.90):
    """ONE No-Goal (BTTS-No) candidate per gameweek — the match least likely
    to see both teams score.

    Backtest (110k evals, 2026-07-26): ranking a GW's matches by the model's
    own BTTS% and taking the single lowest gives 49.3% NG pooled — BELOW the
    ~48-51% break-even once real NG prices are applied, so it is NOT a
    league-wide edge. It held only in ITALY (53.0%, n=1,960; split-half
    H1 51.7 / H2 54.3 — did not decay), so callers should restrict leagues.

    Pricing matters more than the pick here: live NG prices run 1.82-2.25 and
    the most NG-likely match carries the SHORTEST price, so a pick is only
    worth taking when the live NG odds clear `min_odds`. Returns None when
    the market prices the pick too short to beat.
    """
    best = None
    for m in matches:
        btts = m.get("btts_pct")
        if btts is None:
            continue
        if best is None or btts < best.get("btts_pct", 100):
            best = m
    if best is None:
        return None
    ng_odds = best.get("live_ng_odds")
    if not ng_odds or ng_odds < min_odds:
        return None   # priced too short (or GG/NG not scraped for this GW yet)
    return {
        "legs": [{"match": (best.get("home_team"), best.get("away_team")),
                  "market": "GG/NG", "call": "NG", "odds": ng_odds,
                  "conf": round(100 - (best.get("btts_pct") or 50), 1),
                  "signals": 0}],
        "total": ng_odds,
        "p": round(100 - (best.get("btts_pct") or 50), 1),
        "provisional": False,
    }


BANKERS_PENDING_PATH = os.path.join(ACCURACY_DIR, "bankers_pending.json")
BANKERS_TOTALS_PATH = os.path.join(ACCURACY_DIR, "bankers_totals.json")
BANKERS_OUTCOMES_PATH = os.path.join(ACCURACY_DIR, "bankers_outcomes.jsonl")


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, data):
    try:
        os.makedirs(ACCURACY_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def snapshot_gw_bankers(league, week, single, two, three):
    """Record the tickets we'd stake on this GW (once per league+week+cycle)
    so their real outcome can be tracked."""
    if not single and not two and not three:
        return
    pending = _load_json(BANKERS_PENDING_PATH, {})
    now = datetime.now()
    key = f"{league}_W{week}"
    prev = pending.get(key)
    if prev:
        try:
            age = (now - datetime.strptime(prev["ts"], "%Y-%m-%d %H:%M")).total_seconds()
            if age < SNAPSHOT_MAX_AGE:
                return  # this GW cycle already snapshotted
        except (ValueError, TypeError):
            pass
    def strip_ticket(t):
        if not t:
            return None
        return {"total": t["total"], "p": t["p"],
                "provisional": t.get("provisional", False),
                "legs": [{"home": l["match"][0], "away": l["match"][1],
                          "call": l["call"], "odds": l["odds"]} for l in t["legs"]]}
    pending[key] = {"league": league, "week": week,
                    "ts": now.strftime("%Y-%m-%d %H:%M"),
                    "one": strip_ticket(single),
                    "two": strip_ticket(two), "three": strip_ticket(three)}
    _save_json(BANKERS_PENDING_PATH, pending)


def _leg_hit(leg, res):
    hs, aws = int(res["home_score"]), int(res["away_score"])
    call = leg["call"]
    if call == "1":
        return hs > aws
    if call == "2":
        return aws > hs
    if call == "O2.5":
        return hs + aws > 2.5
    if call == "U2.5":
        return hs + aws <= 2.5
    return False


def evaluate_pending_bankers(league, league_data):
    """Resolve pending banker tickets for this league against results that
    surfaced after the ticket was recorded; accumulate the W/L record."""
    pending = _load_json(BANKERS_PENDING_PATH, {})
    mine = {k: v for k, v in pending.items() if v.get("league") == league}
    if not mine:
        return

    results = defaultdict(list)
    for m in league_data.get(league, []):
        try:
            fdt = datetime.strptime(m.get("_file_ts", ""), "%Y%m%d_%H%M%S")
        except ValueError:
            continue
        results[(m.get("week"), m.get("home_team"), m.get("away_team"))].append((fdt, m))

    totals = _load_json(BANKERS_TOTALS_PATH, {})
    changed = False
    outcome_records = []
    for key, snap in mine.items():
        try:
            ts = datetime.strptime(snap["ts"], "%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            pending.pop(key, None)
            changed = True
            continue
        age = (datetime.now() - ts).total_seconds()
        if age > SNAPSHOT_MAX_AGE:
            pending.pop(key, None)  # expired unresolved (results never seen)
            changed = True
            continue

        def resolve(ticket):
            if not ticket:
                return None
            hits = []
            for leg in ticket["legs"]:
                actual = None
                for fdt, m in results.get((snap["week"], leg["home"], leg["away"]), []):
                    if -120 <= (fdt - ts).total_seconds() <= SNAPSHOT_MAX_AGE:
                        actual = m
                        break
                if actual is None:
                    return None  # not all legs resolved yet
                hits.append(_leg_hit(leg, actual))
            return all(hits)

        r1 = resolve(snap.get("one"))
        r2 = resolve(snap.get("two"))
        r3 = resolve(snap.get("three"))
        resolved_all = all(
            snap.get(nm) is None or rr is not None
            for nm, rr in (("one", r1), ("two", r2), ("three", r3)))
        if resolved_all:
            for name, r, tk in (("one", r1, snap.get("one")),
                                ("two", r2, snap.get("two")),
                                ("three", r3, snap.get("three"))):
                if r is None or not tk:
                    continue
                t = totals.setdefault(name, {"n": 0, "w": 0, "stake": 0.0, "ret": 0.0})
                t["n"] += 1
                t["w"] += int(r)
                t["stake"] += 1.0
                t["ret"] += tk["total"] if r else 0.0
                # Per-outcome log with timestamps — feeds losing-streak
                # stats for martingale staking
                outcome_records.append({
                    "ts": snap["ts"], "league": league, "week": snap["week"],
                    "kind": name, "won": bool(r), "odds": tk["total"]})
            pending.pop(key, None)
            changed = True

    if changed:
        _save_json(BANKERS_PENDING_PATH, pending)
        _save_json(BANKERS_TOTALS_PATH, totals)
    if outcome_records:
        try:
            with open(BANKERS_OUTCOMES_PATH, "a", encoding="utf-8") as f:
                for rec in outcome_records:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass


def compute_banker_streaks(league=None):
    """Losing-streak stats per banker type, for martingale staking:
    current run of consecutive losses, plus the worst run within the last
    2 days / 7 days / 30 days, and the worst ever seen."""
    now = datetime.now()
    seqs = {"one": [], "two": [], "three": []}
    try:
        with open(BANKERS_OUTCOMES_PATH, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if league and rec.get("league") != league:
                    continue
                kind = rec.get("kind")
                if kind not in seqs:
                    continue
                try:
                    ts = datetime.strptime(rec.get("ts", ""), "%Y-%m-%d %H:%M")
                except (ValueError, TypeError):
                    continue
                seqs[kind].append((ts, bool(rec.get("won")), rec.get("odds") or 0))
    except FileNotFoundError:
        pass
    except Exception:
        pass

    out = {}
    for kind, seq in seqs.items():
        if not seq:
            continue
        seq.sort(key=lambda t: t[0])

        def max_streak(items):
            worst = cur = 0
            for item in items:
                cur = 0 if item[1] else cur + 1
                worst = max(worst, cur)
            return worst

        current = 0
        for item in reversed(seq):
            if item[1]:
                break
            current += 1

        wins = sum(1 for item in seq if item[1])
        ret = sum(item[2] for item in seq if item[1])
        out[kind] = {
            "n": len(seq),
            "current": current,
            "d2": max_streak([s for s in seq if (now - s[0]).days < 2]),
            "d7": max_streak([s for s in seq if (now - s[0]).days < 7]),
            "d30": max_streak([s for s in seq if (now - s[0]).days < 30]),
            "max": max_streak(seq),
            # Record over the SAME outcomes the streaks are computed from,
            # so the two lines can never disagree
            "w": wins,
            "win_pct": round(wins / len(seq) * 100, 1),
            "roi": round((ret - len(seq)) / len(seq) * 100, 1),
        }
    return out


def _reconstruct_singles(min_odds=1.60, max_odds=2.04, min_signals=0):
    """Rebuild the banker SINGLE each GW cycle actually offered, from the eval
    logs — same construction as _gw_bankers — tagged with its call, odds band,
    league, hour and gameweek. This is the raw material for the stability
    monitor (bankers_outcomes.jsonl doesn't record the call/market).

    min_signals>0 reconstructs the UPGRADED single instead: only Over 2.5 legs
    with at least this many goal-fest signals qualify (empty GWs are skipped).
    """
    rates = _banker_verified_rates()
    out = []
    for path in glob.glob(os.path.join(ACCURACY_DIR, "eval_log_*.jsonl")):
        league = os.path.basename(path)[9:-6].title()
        groups = defaultdict(list)
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                        t = datetime.strptime(r["evaluated_at"], "%Y-%m-%d %H:%M")
                    except Exception:
                        continue
                    if r.get("actual_home") is None:
                        continue
                    key = (r.get("week"), t.strftime("%Y-%m-%d"), t.hour * 60 // 45)
                    groups[key].append((t, r))
        except Exception:
            continue

        for key, items in groups.items():
            verified, market = [], []
            for _, r in items:
                hs, aws = r["actual_home"], r["actual_away"]
                co = {"Home Win": r.get("home_odds"),
                      "Away Win": r.get("away_odds")}.get(r.get("prediction"))
                if co and co > 1:
                    call = "1" if r["prediction"] == "Home Win" else "2"
                    won = (hs > aws) if call == "1" else (aws > hs)
                    leg = {"call": call, "odds": co, "won": won}
                    if co <= 1.45 and rates["fav145"]["n"] >= 50:
                        verified.append({**leg, "p": _leg_p(rates["fav145"]["hit"], co)})
                    elif co <= 1.75:
                        market.append({**leg, "p": _leg_p(None, co)})
                p = r.get("over_2_5_pct")
                if p:
                    conf = max(p, 100 - p)
                    call = "O2.5" if p > 50 else "U2.5"
                    odds = r.get("over_25_odds") if p > 50 else r.get("under_25_odds")
                    if odds and odds > 1:
                        tot = hs + aws
                        won = (tot > 2.5) if p > 50 else (tot <= 2.5)
                        sig = _over_signal_count(r) if p > 50 else 0
                        leg = {"call": call, "odds": odds, "won": won, "signals": sig}
                        tier = "ou60" if conf >= 60 else ("ou55" if conf >= 55 else None)
                        if tier and rates[tier]["n"] >= 50:
                            verified.append({**leg, "p": _leg_p(rates[tier]["hit"], odds)})
                        elif conf >= 52:
                            market.append({**leg, "p": _leg_p(None, odds)})

            def _in_window(l):
                if not (min_odds <= l["odds"] <= max_odds):
                    return False
                if min_signals > 0 and (l["call"] != "O2.5"
                                        or l.get("signals", 0) < min_signals):
                    return False
                return True

            pool = [l for l in verified if _in_window(l)] \
                or [l for l in market if _in_window(l)]
            if not pool:
                continue
            best = max(pool, key=lambda l: l["p"])
            t0 = min(t for t, _ in items)
            out.append({**best, "league": league, "t": t0,
                        "week": key[0] or 0, "hour": t0.hour})
    out.sort(key=lambda l: l["t"])
    return out


def _cell(rs):
    n = len(rs)
    if not n:
        return None
    w = sum(1 for l in rs if l["won"])
    impl = sum(1.0 / l["odds"] for l in rs) / n * 100
    ret = sum(l["odds"] for l in rs if l["won"])
    return {"n": n, "win": round(w / n * 100, 1), "impl": round(impl, 1),
            "roi": round((ret - n) / n * 100, 1)}


def compute_stability_report(min_n=60, singles=None):
    """For every candidate pattern, split the history in HALF and report both
    halves side by side. A real edge repeats; noise flips. Nothing here is
    applied automatically — it is an early-warning board."""
    if singles is None:
        singles = _reconstruct_singles()
    if len(singles) < 200:
        return {"ready": False, "n": len(singles), "groups": []}

    half = len(singles) // 2
    first, second = singles[:half], singles[half:]

    def dim(title, note, labeller, order=None):
        labels = order or sorted(set(labeller(l) for l in singles
                                     if labeller(l) is not None))
        rows = []
        for lab in labels:
            a = _cell([l for l in first if labeller(l) == lab])
            b = _cell([l for l in second if labeller(l) == lab])
            full = _cell([l for l in singles if labeller(l) == lab])
            if not full or full["n"] < min_n or not a or not b:
                continue
            if a["n"] < 20 or b["n"] < 20:
                continue
            same_side = (a["roi"] > 0) == (b["roi"] > 0)
            swing = round(b["win"] - a["win"], 1)
            rows.append({"label": str(lab), "first": a, "second": b,
                         "full": full, "stable": same_side and abs(swing) <= 3.0,
                         "swing": swing, "same_side": same_side})
        verdict = None
        if rows:
            n_stable = sum(1 for r in rows if r["stable"])
            verdict = {"stable": n_stable, "total": len(rows),
                       "ok": n_stable >= max(2, len(rows) * 0.6)}
        return {"title": title, "note": note, "rows": rows, "verdict": verdict}

    def band(l):
        o = l["odds"]
        if o < 1.65:
            return "1.60-1.64"
        if o < 1.70:
            return "1.65-1.69"
        if o < 1.76:
            return "1.70-1.75"
        if o < 1.86:
            return "1.76-1.85"
        return "1.86-2.04"

    def gwband(l):
        w = l["week"]
        if not w:
            return None
        lo = ((w - 1) // 8) * 8 + 1
        return "GW %d-%d" % (lo, lo + 7)

    groups = [
        dim("Call type", "Which selection the banker used",
            lambda l: l["call"], ["1", "2", "O2.5", "U2.5"]),
        dim("Odds band", "Does a price range really win more?", band,
            ["1.60-1.64", "1.65-1.69", "1.70-1.75", "1.76-1.85", "1.86-2.04"]),
        dim("League", "Is any league genuinely better?", lambda l: l["league"]),
        dim("Hour of day", "Local time the GW settled", lambda l: "%02d:00" % l["hour"]),
        dim("Gameweek band", "Any part of the season to avoid?", gwband),
    ]

    q = len(singles) // 4
    quarters = []
    for i in range(4):
        seg = singles[i * q:(i + 1) * q] if i < 3 else singles[3 * q:]
        c = _cell(seg)
        if c:
            c["from"] = seg[0]["t"].strftime("%m-%d %H:%M")
            c["to"] = seg[-1]["t"].strftime("%m-%d %H:%M")
            quarters.append(c)

    return {"ready": True, "n": len(singles), "groups": groups,
            "quarters": quarters, "overall": _cell(singles),
            "span": (singles[0]["t"].strftime("%Y-%m-%d"),
                     singles[-1]["t"].strftime("%Y-%m-%d"))}


HOUR_BLOCKS = [("00-03", 0, 4), ("04-07", 4, 8), ("08-11", 8, 12),
               ("12-15", 12, 16), ("16-19", 16, 20), ("20-23", 20, 24)]


def compute_hour_league_matrix(min_n=40, singles=None):
    """League x hour-block matrix for banker SINGLES.

    Hours are merged into 4-hour blocks: a per-league per-hour grid would be
    ~144 cells of n~45 — far too thin to read. Each cell also carries its
    split-half verdict, so a cell that looks good but FLIPPED is visible.
    Times are your local clock (when the GW settled).
    """
    if singles is None:
        singles = _reconstruct_singles()
    if len(singles) < 200:
        return {"ready": False, "n": len(singles)}

    half = len(singles) // 2
    first, second = singles[:half], singles[half:]
    leagues = sorted(set(l["league"] for l in singles))

    rows = []
    for lg in leagues:
        cells = []
        for lab, lo, hi in HOUR_BLOCKS:
            pick = lambda src: [l for l in src
                                if l["league"] == lg and lo <= l["hour"] < hi]
            full = _cell(pick(singles))
            if not full or full["n"] < min_n:
                cells.append({"label": lab, "thin": True,
                              "n": full["n"] if full else 0})
                continue
            a, b = _cell(pick(first)), _cell(pick(second))
            stab = None
            if a and b and a["n"] >= 15 and b["n"] >= 15:
                same = (a["roi"] > 0) == (b["roi"] > 0)
                swing = round(b["win"] - a["win"], 1)
                stab = {"a": a, "b": b, "swing": swing, "same": same,
                        "hold": same and abs(swing) <= 3.0}
            cells.append({"label": lab, "thin": False, "full": full, "stab": stab})
        rows.append({"league": lg, "cells": cells,
                     "total": _cell([l for l in singles if l["league"] == lg])})

    margins = [{"label": lab,
                "cell": _cell([l for l in singles if lo <= l["hour"] < hi])}
               for lab, lo, hi in HOUR_BLOCKS]

    # how many cells in the whole grid actually hold up?
    all_cells = [c for r in rows for c in r["cells"]
                 if not c["thin"] and c.get("stab")]
    held = sum(1 for c in all_cells if c["stab"]["hold"])
    return {"ready": True, "blocks": [b[0] for b in HOUR_BLOCKS],
            "rows": rows, "margins": margins, "overall": _cell(singles),
            "held": held, "tested": len(all_cells)}


def _single_streak_table(min_signals=0, min_odds=None):
    """Per-league win% + REAL streaks for the (upgraded) SINGLE, reconstructed
    from the eval logs. Streaks are within-league (what a martingale feels).

    The UPGRADED single must use the LOWER floor (upgraded_odds_min, 1.50):
    goal-fest Overs are priced ~1.50-1.60, so reading them through the plain
    single's 1.60 floor doesn't measure the strategy — it measures which
    league happens to price its goal-fests above 1.60 (that alone made
    Germany look like n=65 instead of ~480, and hid France/Spain/Turkey
    entirely). This must match what the bettor's upgraded_single preset bets.
    """
    if min_odds is None:
        min_odds = 1.50 if min_signals > 0 else 1.60
    singles = _reconstruct_singles(min_odds=min_odds, min_signals=min_signals)

    def streak_info(seq):
        worst = cur = 0
        runs = defaultdict(int)
        for won in seq:
            if won:
                if cur:
                    runs[cur] += 1
                cur = 0
            else:
                cur += 1
                worst = max(worst, cur)
        if cur:
            runs[cur] += 1
        return worst, dict(sorted(runs.items()))

    rows = []
    for lg in sorted(set(l["league"] for l in singles)):
        rs = sorted([l for l in singles if l["league"] == lg], key=lambda l: l["t"])
        n = len(rs)
        if n < 25:
            continue
        w = sum(1 for l in rs if l["won"])
        ret = sum(l["odds"] for l in rs if l["won"])
        impl = sum(1.0 / l["odds"] for l in rs) / n * 100
        mx, runs = streak_info([l["won"] for l in rs])
        four = sum(v for k, v in runs.items() if k >= 4)
        rows.append({
            "league": lg, "n": n,
            "win_pct": round(w / n * 100, 1),
            "impl_pct": round(impl, 1),
            "edge": round(w / n * 100 - impl, 1),
            "roi": round((ret - n) / n * 100, 1),
            "avg_odds": round(sum(l["odds"] for l in rs) / n, 2),
            "max_streak": mx, "four_plus": four,
            "per_freq": int(n / four) if four else None,
            "runs": runs,
        })
    rows.sort(key=lambda d: (d["max_streak"], -d["win_pct"]))
    return rows


def compute_league_type_table():
    """Per-league x per-type banker performance with REAL streaks.

    Streaks are computed within each league separately — a martingale runs
    inside one league, so merging leagues into one sequence would invent
    streaks nobody ever experienced.
    """
    rows = []
    try:
        with open(BANKERS_OUTCOMES_PATH, encoding="utf-8") as f:
            for i, line in enumerate(f):
                try:
                    r = json.loads(line)
                    r["_t"] = datetime.strptime(r["ts"], "%Y-%m-%d %H:%M")
                except Exception:
                    continue
                if r.get("odds") is None or r.get("won") is None:
                    continue
                r["_line"] = i
                rows.append(r)
    except FileNotFoundError:
        return []
    except Exception:
        return []

    def streak_info(seq):
        worst = cur = 0
        runs = defaultdict(int)
        for won in seq:
            if won:
                if cur:
                    runs[cur] += 1
                cur = 0
            else:
                cur += 1
                worst = max(worst, cur)
        if cur:
            runs[cur] += 1
        return worst, dict(sorted(runs.items()))

    out = []
    leagues = sorted(set(r["league"] for r in rows))
    for kind, label in (("one", "SINGLE"), ("two", "2-ODDS"), ("three", "3-ODDS")):
        for lg in leagues:
            rs = sorted([r for r in rows if r["kind"] == kind and r["league"] == lg],
                        key=lambda r: (r["_t"], r["_line"]))
            n = len(rs)
            if n < 40:
                continue
            w = sum(1 for r in rs if r["won"])
            ret = sum(r["odds"] for r in rs if r["won"])
            mx, runs = streak_info([r["won"] for r in rs])
            four_plus = sum(v for k, v in runs.items() if k >= 4)
            out.append({
                "kind": kind, "type": label, "league": lg, "n": n,
                "win_pct": round(w / n * 100, 1),
                "avg_odds": round(sum(r["odds"] for r in rs) / n, 2),
                "max_streak": mx,
                "four_plus": four_plus,
                "per_freq": int(n / four_plus) if four_plus else None,
                "roi": round((ret - n) / n * 100, 1),
                "runs": runs,
            })
    out.sort(key=lambda d: -d["roi"])
    return out


def load_banker_record():
    totals = _load_json(BANKERS_TOTALS_PATH, {})
    rec = {}
    for name in ("one", "two", "three"):
        t = totals.get(name)
        if t and t.get("n"):
            rec[name] = {"n": t["n"], "w": t["w"],
                         "win_pct": round(t["w"] / t["n"] * 100, 1),
                         "roi": round((t["ret"] - t["stake"]) / t["stake"] * 100, 1)}
    return rec


def _collect_banker_waves(max_waves=5):
    """Group every league's upcoming GWs into play-waves. Leagues run
    near-concurrently (~4.5 min per GW), so each league's i-th upcoming GW
    plays within a couple of minutes of the others' — wave 0 is what's
    playing/next everywhere, wave 1 is one GW later, and so on. Per wave,
    build each league's three banker plays and keep the surest few of each
    type across leagues."""
    waves = [{"idx": i, "singles": [], "twos": [], "threes": []}
             for i in range(max_waves)]
    with state_lock:
        upcoming_map = {lg: list(state.get("football_upcoming", {}).get(lg, []))
                        for lg in FOOTBALL_LEAGUES}
        cur_weeks = dict(state.get("current_weeks", {}))

    for lg, preds in upcoming_map.items():
        if not preds:
            continue
        cur = cur_weeks.get(lg) or preds[0].get("week") or 1
        total_w = LEAGUE_WEEKS.get(lg, 38)
        by_w = defaultdict(list)
        for p in preds:
            if p.get("week"):
                by_w[p["week"]].append(p)
        ordered = sorted(by_w.items(), key=lambda kv: (kv[0] - cur) % total_w)
        for i, (wk, ms) in enumerate(ordered[:max_waves]):
            try:
                single, two, three = _gw_bankers(ms)
            except Exception:
                continue
            for name, tk in (("singles", single), ("twos", two), ("threes", three)):
                if tk:
                    waves[i][name].append({"league": lg, "gw": wk, **tk})

    for w in waves:
        for name in ("singles", "twos", "threes"):
            w[name].sort(key=lambda t: (-t["p"], t.get("provisional", False)))
            w[name] = w[name][:3]
    return [w for w in waves if w["singles"] or w["twos"] or w["threes"]]


@app.route("/bankers")
def bankers_page():
    rates, singles, tickets = _collect_bankers()
    return render_template("bankers.html",
                           rates=rates,
                           waves=_collect_banker_waves(),
                           banker_record=load_banker_record(),
                           banker_streaks=compute_banker_streaks(),
                           last_update=state.get("last_update"))


# ═══════════════════════════════════════════════════════════
#  ROUTES
# ═══════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html",
                           last_update=state.get("last_update"),
                           data_loaded=state.get("data_loaded", False))


@app.route("/football")
def football_home():
    leagues = []
    for lg in FOOTBALL_LEAGUES:
        leagues.append({
            "name": lg,
            "flag": LEAGUE_FLAGS.get(lg, ""),
            "teams": LEAGUE_TEAM_COUNT.get(lg, 0),
            "total_weeks": LEAGUE_WEEKS.get(lg, 38),
            "current_week": state.get("current_weeks", {}).get(lg, "?"),
            "current_season": state.get("current_seasons", {}).get(lg, "?"),
            "has_data": lg in state.get("football_models", {}),
        })
    return render_template("football.html", leagues=leagues,
                           last_update=state.get("last_update"))


@app.route("/football/<league>")
def football_league(league):
    league = league.title()
    if league not in state.get("football_models", {}):
        return render_template("league.html", league=league, error=True,
                               last_update=state.get("last_update"))

    model = state["football_models"][league]
    table = state["football_tables"].get(league, [])
    upcoming = state["football_upcoming"].get(league, [])
    upcoming_improved = state.get("football_upcoming_improved", {}).get(league, [])
    acc_stats = state.get("accuracy_stats", {}).get(league, {})

    current_week = state.get("current_weeks", {}).get(league, 1)
    current_season = state.get("current_seasons", {}).get(league, 1)
    total_weeks = LEAGUE_WEEKS.get(league, 38)

    # Order weeks by distance from the current GW so a season wrap
    # (38, 1, 2, ...) still lists the current week first
    def _week_order(w):
        if not w:
            return 999
        return (w - current_week) % total_weeks

    upcoming_by_week = defaultdict(list)
    for p in upcoming:
        w = p.get("week") or 0
        upcoming_by_week[w].append(p)
    upcoming_weeks = sorted(upcoming_by_week.items(), key=lambda kv: _week_order(kv[0]))

    # Per-GW banker plays (martingale-safe single + ~2 odds + ~3 odds), plus
    # the UPGRADED single (Over 2.5 with 2+ goal-fest signals: ~61% win vs
    # ~55%). Upgraded fires only some GWs — that is the point.
    gw_bankers = {}
    for w, ms in upcoming_by_week.items():
        try:
            single, two, three = _gw_bankers(ms)
            # Goal-fests are priced ~1.50-1.60, BELOW the plain single's
            # floor — building the upgraded pick at 1.60 silently drops most
            # of them (and all of France/Spain/Turkey's). Use the upgraded
            # floor, same as the bettor's upgraded_single preset.
            up_single, _, _ = _gw_bankers(ms, single_lo=1.50,
                                          allowed_calls={"O2.5"}, min_signals=2)
            if single or two or three:
                gw_bankers[w] = {"one": single, "two": two, "three": three,
                                 "upgraded": up_single}
        except Exception:
            pass

    improved_by_week = defaultdict(list)
    for p in upcoming_improved:
        w = p.get("week") or 0
        improved_by_week[w].append(p)
    improved_weeks = sorted(improved_by_week.items(), key=lambda kv: _week_order(kv[0]))

    # Top pick: the play with the highest VERIFIED win rate available in the
    # current GW (not just model edge) — falls back to strongest O/U edge
    rates = _banker_verified_rates()
    top_pick = None
    top_pick_play = None
    cur_matches = upcoming_by_week.get(current_week, upcoming)
    best_rate = 0
    for m in cur_matches:
        cands = []
        co = {"Home Win": m.get("live_home_odds"),
              "Away Win": m.get("live_away_odds")}.get(m.get("prediction"))
        if co and 1.0 < co <= 1.45 and rates["fav145"]["n"] >= 50:
            cands.append(("1" if m["prediction"] == "Home Win" else "2",
                          co, rates["fav145"]))
        ou_p = m.get("over_2_5_pct", 50)
        conf = max(ou_p, 100 - ou_p)
        odds = m.get("live_over_25") if ou_p > 50 else m.get("live_under_25")
        tier = "ou60" if conf >= 60 else ("ou55" if conf >= 55 else None)
        if tier and odds and odds > 1 and rates[tier]["n"] >= 50:
            cands.append(("O2.5" if ou_p > 50 else "U2.5", odds, rates[tier]))
        for call, odds_, r in cands:
            if r["hit"] > best_rate:
                best_rate = r["hit"]
                top_pick = m
                top_pick_play = {"call": call, "odds": odds_,
                                 "rate": r["hit"], "n": r["n"]}
    if top_pick is None:
        best_edge = 0
        for m in upcoming:
            o25 = m.get("over_2_5_pct", 50)
            edge = abs(o25 - 50)
            if edge > best_edge:
                best_edge = edge
                top_pick = m

    # Per-league record + streaks from the SAME outcome log, so the two
    # lines always agree; fall back to global record while a league has no
    # resolved outcomes yet
    league_streaks = compute_banker_streaks(league)
    if league_streaks:
        league_banker_record = {
            k: {"n": v["n"], "w": v["w"],
                "win_pct": v["win_pct"], "roi": v["roi"]}
            for k, v in league_streaks.items()
        }
    else:
        league_banker_record = load_banker_record()

    return render_template("league.html",
                           league=league,
                           flag=LEAGUE_FLAGS.get(league, ""),
                           error=False,
                           model=model,
                           table=table,
                           current_week=current_week,
                           current_season=current_season,
                           total_weeks=total_weeks,
                           total_teams=LEAGUE_TEAM_COUNT.get(league, 0),
                           upcoming=upcoming_weeks,
                           upcoming_improved=improved_weeks,
                           gw_bankers=gw_bankers,
                           banker_record=league_banker_record,
                           banker_streaks=league_streaks,
                           accuracy_stats=acc_stats,
                           top_pick=top_pick,
                           top_pick_play=top_pick_play,
                           last_update=state.get("last_update"))


@app.route("/racing")
def racing_home():
    sports = {}
    accuracy = state.get("racing_accuracy", {})
    for key, data in state.get("racing_predictions", {}).items():
        m = data["model"]
        sport = m["sport"]
        if sport not in sports:
            sports[sport] = []
        acc = accuracy.get(key, {})
        sports[sport].append({
            "key": key,
            "venue": m["venue"],
            "n": m["n"],
            "even_rate": m.get("even_rate"),
            "over_rate": m.get("over_rate"),
            "upcoming": len(data.get("races", [])),
            "winner_pct": acc.get("winner_pct"),
        })
    return render_template("racing.html", sports=sports,
                           last_update=state.get("last_update"))


@app.route("/racing/<path:venue_key>")
def racing_venue(venue_key):
    preds = state.get("racing_predictions", {})
    if venue_key not in preds:
        return render_template("venue.html", venue_key=venue_key, error=True,
                               last_update=state.get("last_update"))

    data = preds[venue_key]
    return render_template("venue.html",
                           venue_key=venue_key,
                           error=False,
                           model=data["model"],
                           races=data.get("races", []),
                           accuracy=state.get("racing_accuracy", {}).get(venue_key, {}),
                           last_update=state.get("last_update"))


_HEALTH_COMPONENTS = [
    ("virtual_session", "Games site session",
     "Connection to the Sportybet virtual games provider"),
    ("odds_scrape", "Football odds scraper",
     "Collects upcoming fixtures + live odds for all 6 leagues"),
    ("results_scrape", "Football results scraper",
     "Collects finished match scores (feeds accuracy + streaks)"),
    ("racing_upcoming", "Racing upcoming scraper",
     "Collects upcoming races, runners and odds for all venues"),
    ("racing_results", "Racing results scraper",
     "Collects finished race results (feeds racing accuracy)"),
    ("models_rebuild", "Prediction models",
     "Rebuilds all predictions from the collected data"),
]


@app.route("/stability")
def stability_page():
    singles = _reconstruct_singles()
    return render_template("stability.html",
                           rep=compute_stability_report(singles=singles),
                           matrix=compute_hour_league_matrix(singles=singles),
                           last_update=state.get("last_update"))


@app.route("/performance")
def performance_page():
    table = compute_league_type_table()
    singles = sorted([t for t in table if t["kind"] == "one"],
                     key=lambda t: (t["max_streak"], -t["win_pct"]))
    return render_template("performance.html",
                           best=table[:6], worst=table[-4:][::-1],
                           table=table, singles=singles,
                           upgraded=_single_streak_table(min_signals=2),
                           total=sum(t["n"] for t in table),
                           last_update=state.get("last_update"))


@app.route("/patternlab")
def patternlab_page():
    """Pattern Lab — the ChatGPT/DeepSeek framework as a separate research +
    live-read surface (short rolling window, cycle-by-cycle standalone scoring,
    regime detection, walk-forward, ACTIVE/WEAK/DEAD monitor)."""
    try:
        import pattern_lab
        data = pattern_lab.compute_overview()
    except Exception as e:
        data = {"leagues": [], "error": str(e), "window": 10,
                "n_patterns": 0, "total_bets": 0, "grand_best": None,
                "any_edge": False}
    return render_template("patternlab.html", d=data,
                           last_update=state.get("last_update"))


@app.route("/bets")
def bets_page():
    """Bet Picks — the actionable DeepSeek layer: concrete match + selection +
    odds you can stake, a headline Confident Call, a global Top 10, and
    per-league groups (three tabs). Built from live upcoming fixtures + the
    short-memory streak read."""
    from flask import request as _rq
    try:
        min_odds = float(_rq.args.get("min_odds", 1.40))
    except (TypeError, ValueError):
        min_odds = 1.40
    min_odds = max(1.0, min(20.0, round(min_odds, 2)))
    with state_lock:
        upcoming = {lg: list(v) for lg, v in
                    state.get("football_upcoming", {}).items()}
    try:
        import pattern_lab
        d = pattern_lab.build_bet_picks(upcoming, gw_window=3, top_n=10,
                                        min_odds=min_odds)
    except Exception as e:
        d = {"error": str(e), "confident_call": None, "top": [],
             "per_league": {}, "all": [], "n": 0, "window": 10, "gw_window": 3}
    d["min_odds"] = min_odds
    try:
        import cc_forward
        d["forward"] = cc_forward.summary()
    except Exception:
        d["forward"] = {}
    return render_template("betpicks.html", d=d, min_odds=min_odds,
                           last_update=state.get("last_update"))


@app.route("/api/pattern_call")
def api_pattern_call():
    """The auto-bettor's Confident-Call feed. Given the league + the exact GW
    the bettor read off the LIVE page (page-driven timing, so never a stale/far
    GW) + an odds range, return the single best stakeable pick for that GW as a
    placeable spec {home, away, code, odd_name, tab, sel, odds, conf, reason}
    or {} if none qualifies."""
    from flask import request as _rq
    league = (_rq.args.get("league") or "").title()
    try:
        week = int(_rq.args.get("week") or 0)
    except ValueError:
        week = 0

    def _f(name, default):
        try:
            return float(_rq.args.get(name))
        except (TypeError, ValueError):
            return default
    min_odds = max(1.0, _f("min_odds", 1.40))
    max_odds = _f("max_odds", 100.0)
    # Auto-bettor asks for placeable-only (markets we can reliably click on the
    # live page: O/U 2.5 + 1X2 Home/Away). The Bet Picks page passes all=1.
    placeable_only = (_rq.args.get("placeable_only", "1") != "0")

    with state_lock:
        preds = list(state.get("football_upcoming", {}).get(league, []))
    if not preds:
        return jsonify({})
    try:
        import pattern_lab
        # build over a wide window so the requested week is covered, floored at
        # the caller's min_odds; per-fixture best is chosen from PLACEABLE
        # markets only when placeable_only, so the auto-bettor never gets a
        # pick it cannot click.
        d = pattern_lab.build_bet_picks({league: preds}, gw_window=6,
                                        top_n=50, min_odds=min_odds,
                                        placeable_only=placeable_only)
    except Exception as e:
        return jsonify({"error": str(e)})
    cands = [p for p in d.get("all", [])
             if p.get("week") == week and p.get("odds", 0) <= max_odds]
    if not cands:
        return jsonify({})
    p = max(cands, key=lambda x: x["conf"])
    return jsonify({
        "league": league, "week": week,
        "home": p["home"], "away": p["away"], "match": p["match"],
        "code": p["code"], "odd_name": p["odd_name"], "tab": p["tab"],
        "cells": p.get("cells", 8), "market": p.get("market"),
        "sel": p["sel"], "odds": p["odds"], "conf": p["conf"],
        "model_prob": p["model_prob"], "reason": p["reason"],
        "placeable": p.get("placeable", False),
    })


@app.route("/api/pattern_live")
def api_pattern_live():
    """Currently-open cycles across all leagues, hottest first — the live
    ride-and-switch board (also consumable by the bettor if wired later)."""
    try:
        import pattern_lab
        return jsonify({"board": pattern_lab.compute_live_board()})
    except Exception as e:
        return jsonify({"board": [], "error": str(e)})


@app.route("/status")
def status_page():
    with state_lock:
        health = {k: dict(v) for k, v in state.get("health", {}).items()}
        cur_weeks = dict(state.get("current_weeks", {}))
        last_update = state.get("last_update")

    comps = []
    all_ok = True
    for key, label, desc in _HEALTH_COMPONENTS:
        info = health.get(key)
        if not info:
            comps.append({"label": label, "desc": desc, "ok": False,
                          "unknown": True, "ok_at": None, "error": None})
            continue
        ok = not info.get("error")
        if not ok:
            all_ok = False
        comps.append({"label": label, "desc": desc, "ok": ok, "unknown": False,
                      "ok_at": info.get("ok_at"), "error": info.get("error")})

    return render_template("status.html",
                           comps=comps, all_ok=all_ok,
                           version=APP_VERSION,
                           current_weeks=cur_weeks,
                           last_update=last_update)


@app.route("/api/banker_pick")
def api_banker_pick():
    """The EXACT banker ticket (single/two/three) for one specific
    league + gameweek — same construction as the Bankers page. The bettor
    reads the live page for which GW is next, then asks here for that GW's
    banker games; it clicks them on the live page at live odds.
    Returns {legs:[{home,away,market,call,odds,conf}], total, provisional}
    or {} if no ticket for that GW/type."""
    from flask import request as _rq
    league = (_rq.args.get("league") or "").title()
    btype = (_rq.args.get("type") or "single").lower()
    try:
        week = int(_rq.args.get("week") or 0)
    except ValueError:
        week = 0

    # Optional caller overrides (the auto-bettor passes these from its config):
    #   smin/smax  – narrow the SINGLE's odds window (default 1.60-2.04)
    #   calls      – whitelist of calls for the SINGLE, e.g. "O2.5" or "O2.5,1"
    def _f(name, default):
        try:
            return float(_rq.args.get(name))
        except (TypeError, ValueError):
            return default
    smin = _f("smin", 1.60)
    smax = _f("smax", 2.04)
    calls = {c.strip() for c in (_rq.args.get("calls") or "").split(",") if c.strip()}
    try:
        minsig = int(_rq.args.get("minsig") or 0)
    except ValueError:
        minsig = 0

    with state_lock:
        preds = list(state.get("football_upcoming", {}).get(league, []))
    matches = [p for p in preds if p.get("week") == week]
    if not matches:
        return jsonify({})
    if btype == "ng":
        # One NG candidate for this GW, only if the live NG price clears the
        # floor (see _gw_ng_pick — short prices are what kill this market)
        try:
            ticket = _gw_ng_pick(matches, min_odds=_f("ngmin", 1.90))
        except Exception:
            return jsonify({})
        if not ticket:
            return jsonify({})
        return jsonify({"league": league, "week": week, "type": "ng",
                        "legs": [{"home": l["match"][0], "away": l["match"][1],
                                  "market": l["market"], "call": l["call"],
                                  "odds": l["odds"], "conf": l["conf"],
                                  "signals": 0} for l in ticket["legs"]],
                        "total": ticket["total"], "provisional": False,
                        "est_win_pct": ticket["p"]})
    try:
        single, two, three = _gw_bankers(matches, single_lo=smin, single_hi=smax,
                                         allowed_calls=calls or None,
                                         min_signals=minsig)
    except Exception:
        return jsonify({})
    ticket = {"single": single, "two": two, "three": three}.get(btype)
    if not ticket:
        return jsonify({})
    legs = [{"home": l["match"][0], "away": l["match"][1],
             "market": l.get("market", "Over/Under 2.5"),
             "call": l["call"], "odds": l.get("odds"),
             "conf": l.get("conf"), "signals": l.get("signals", 0)}
            for l in ticket["legs"]]
    return jsonify({"league": league, "week": week, "type": btype,
                    "legs": legs, "total": ticket.get("total"),
                    "provisional": ticket.get("provisional", False),
                    "est_win_pct": ticket.get("p")})


@app.route("/api/bet_pick")
def api_bet_pick():
    """The auto-bettor's pick feed.
    ?type=single|two|three  -> football banker ticket from the next
                               not-yet-playing wave (legs list)
    ?type=racing_place&sport=...&venue=... -> rank-calibrated 2nd and 3rd
                               picks of the next bettable race (PLACE market)
    Returns {} when nothing qualifies."""
    from flask import request as _rq
    btype = (_rq.args.get("type") or "single").lower()

    if btype == "racing_place":
        sport = _rq.args.get("sport") or "Greyhound Racing"
        venue = _rq.args.get("venue") or "Santa Monica"
        key = f"{sport} — {venue}"
        with state_lock:
            data = state.get("racing_predictions", {}).get(key, {})
            races = list(data.get("races", []))
        for rp in races:
            cd = str(rp.get("countdown") or "")
            m = re.match(r"(\d{1,2}):(\d{2})", cd)
            secs = (int(m.group(1)) * 60 + int(m.group(2))) if m else 0
            if secs < 50:
                continue  # too close to start — next race
            # rank-calibrated picks when learned, else model ranks 2 and 3
            runners = rp.get("imp_runners") or rp.get("runners") or []
            if len(runners) < 3:
                continue
            picks = [{"name": r.get("name", ""), "badge": r.get("badge"),
                      "win_prob": r.get("win_prob"),
                      "win_odds": r.get("win_odds"),
                      "place_odds": r.get("place_odds"),
                      "show_odds": r.get("show_odds")}
                     for r in runners[1:3]]
            return jsonify({
                "type": "racing_place", "sport": sport, "venue": venue,
                "race_id": rp.get("race_id"), "countdown": cd,
                "market": "PLACE", "picks": picks,
                "rank_learned": bool(rp.get("imp_ready")),
                "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
        return jsonify({})

    kind = {"single": "singles", "two": "twos", "three": "threes"}.get(btype)
    if not kind:
        return jsonify({})
    try:
        min_wave = max(1, int(_rq.args.get("min_wave") or 1))
    except ValueError:
        min_wave = 1
    leagues_f = [l.strip().title()
                 for l in (_rq.args.get("leagues") or "").split(",") if l.strip()]
    try:
        waves = _collect_banker_waves(max_waves=4)
    except Exception:
        waves = []
    for w in waves:
        if w["idx"] < min_wave:
            continue  # wave 0 is already playing; caller may skip further ahead
        for s in w[kind]:
            if leagues_f and s["league"] not in leagues_f:
                continue
            if s.get("provisional"):
                continue  # only verified-tier plays for real money
            return jsonify({
                "type": btype,
                "league": s["league"], "week": s["gw"],
                "legs": [{"home": l["match"][0], "away": l["match"][1],
                          "market": l.get("market", ""), "call": l["call"],
                          "odds": l["odds"]} for l in s["legs"]],
                "total_odds": s["total"], "est_win_pct": s["p"],
                "wave": w["idx"],
                "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
    return jsonify({})


@app.route("/api/racing_result")
def api_racing_result():
    """Dry-run settlement for racing PLACE bets: did this runner finish in
    the top 2 of the given race?"""
    from flask import request as _rq
    sport = _rq.args.get("sport") or "Greyhound Racing"
    venue = _rq.args.get("venue") or "Santa Monica"
    race_id = str(_rq.args.get("race_id") or "")
    name = _rq.args.get("name") or ""
    market = (_rq.args.get("market") or "show").lower()
    need_pos = {"win": 1, "place": 2, "show": 3}.get(market, 3)
    key = f"{sport} — {venue}"
    with state_lock:
        model = state.get("racing_models", {}).get(key, {})
        recent = list(model.get("recent_races", []))
    for r in recent:
        if str(r.get("race_id")) != race_id:
            continue
        pos = (r.get("positions") or {}).get(name)
        if not pos:
            return jsonify({"status": "missed", "pos": None})
        return jsonify({"status": "placed" if pos <= need_pos else "missed",
                        "pos": pos})
    return jsonify({"status": "pending"})


@app.route("/api/model_call")
def api_model_call():
    """Stateless model call for one fixture — used by the auto-bettor,
    which reads fixtures/odds from the live page (never lags) and only
    needs the model's opinion."""
    from flask import request as _rq
    league = (_rq.args.get("league") or "").title()
    home = _rq.args.get("home") or ""
    away = _rq.args.get("away") or ""
    with state_lock:
        model = state.get("football_models", {}).get(league)
    if not model:
        return jsonify({})
    pred = predict_match(model, home, away)
    if not pred:
        return jsonify({})
    return jsonify({k: pred[k] for k in
                    ("prediction", "confidence", "over_2_5_pct",
                     "ou25_call", "btts_pct")})


@app.route("/api/bet_result")
def api_bet_result():
    """Result cross-check for the auto-bettor: was this call a win?
    Only trusts results scraped within the last 2 hours (fixtures recycle
    every season cycle)."""
    from flask import request as _rq
    league = (_rq.args.get("league") or "").title()
    home = _rq.args.get("home") or ""
    away = _rq.args.get("away") or ""
    call = _rq.args.get("call") or ""
    try:
        week = int(_rq.args.get("week") or 0)
    except ValueError:
        week = 0

    with state_lock:
        rows = list(state.get("recent_results", {}).get(league, []))

    cutoff = datetime.now() - timedelta(hours=2)
    for m in reversed(rows):
        if m["week"] != week or m["home"] != home or m["away"] != away:
            continue
        try:
            fdt = datetime.strptime(m.get("_file_ts", ""), "%Y%m%d_%H%M%S")
        except ValueError:
            continue
        if fdt < cutoff:
            continue
        if m["hs"] is None or m["as"] is None:
            continue
        won = _leg_hit({"call": call},
                       {"home_score": m["hs"], "away_score": m["as"]})
        return jsonify({"status": "won" if won else "lost",
                        "score": f"{m['hs']}-{m['as']}"})
    return jsonify({"status": "pending"})


@app.route("/api/status")
def api_status():
    return jsonify({
        "version": APP_VERSION,
        "last_update": state.get("last_update"),
        "scraper_status": state.get("scraper_status"),
        "scraper_running": state.get("scraper_running"),
        "data_loaded": state.get("data_loaded"),
        "health": state.get("health", {}),
        "football_leagues": list(state.get("football_models", {}).keys()),
        "racing_venues": list(state.get("racing_predictions", {}).keys()),
        "current_weeks": state.get("current_weeks", {}),
    })


@app.route("/api/football/<league>")
def api_football(league):
    league = league.title()
    upcoming = state.get("football_upcoming", {}).get(league, [])
    return jsonify({
        "league": league,
        "current_week": state.get("current_weeks", {}).get(league, 1),
        "upcoming_count": len(upcoming),
        "predictions": upcoming[:20],
    })


# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  SPORTYBET VIRTUAL PREDICTOR - WEB APP")
    print(f"  {datetime.now().strftime('%Y-%m-%d  %H:%M:%S')}")
    print("=" * 60)
    print("  Starting at http://127.0.0.1:5000")
    print("=" * 60)

    scraper_thread = threading.Thread(target=background_scraper, daemon=True)
    scraper_thread.start()

    odds_thread = threading.Thread(target=background_odds_poller, daemon=True)
    odds_thread.start()

    threading.Thread(target=watchdog_thread, daemon=True).start()

    def open_browser():
        time.sleep(1.5)
        webbrowser.open("http://127.0.0.1:5000")
        time.sleep(2)
        webbrowser.open("http://127.0.0.1:5000/status")
    
    threading.Thread(target=open_browser, daemon=True).start()

    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
