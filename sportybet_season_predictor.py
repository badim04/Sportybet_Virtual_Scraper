"""
╔══════════════════════════════════════════════════════════════════╗
║  SPORTYBET VIRTUAL SEASON PREDICTOR  v1                          ║
║  ─────────────────────────────────────────────────────────────── ║
║  Uses 4 past seasons of historical data to build team-level      ║
║  Poisson models and predict the next 38 gameweeks.               ║
║                                                                  ║
║  Football: team attack/defense strength → Poisson match pred     ║
║  Racing:   venue-level E/O, O/U, runner win-rate analysis        ║
║                                                                  ║
║  Usage:                                                          ║
║    python sportybet_season_predictor.py                           ║
║    python sportybet_season_predictor.py --headless                ║
║    python sportybet_season_predictor.py --no-scrape               ║
║    python sportybet_season_predictor.py --seasons 6               ║
║    python sportybet_season_predictor.py --football                ║
║    python sportybet_season_predictor.py --racing                  ║
║                                                                  ║
║  Default: scrape fresh results + upcoming odds, then predict.    ║
║  --no-scrape skips browser, predicts from saved data only.       ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os, sys, json, glob, argparse, math, time, itertools, importlib
from collections import defaultdict, Counter
from datetime import datetime

if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
    )

# ═══════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════

DATA_DIR = "data"
OUT_DIR  = "predictions"

FOOTBALL_LEAGUES = ["England", "France", "Germany", "Italy", "Spain", "Turkey"]

LEAGUE_TEAM_COUNT = {
    "England": 20, "Italy": 20, "Spain": 20,
    "France": 18, "Germany": 18, "Turkey": 18,
}

LEAGUE_WEEKS = {k: (v - 1) * 2 for k, v in LEAGUE_TEAM_COUNT.items()}


# ═══════════════════════════════════════════════════════════
#  SECTION 1 — LOAD DATA
# ═══════════════════════════════════════════════════════════

def load_football(data_dir, num_seasons=4):
    files = sorted(glob.glob(os.path.join(data_dir, "sportybet_results_*.json")))
    if not files:
        files = sorted(glob.glob("sportybet_results_*.json"))

    records, seen = [], set()
    for f in files:
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
                    records.append(r)
        except Exception as e:
            print(f"  ⚠  {os.path.basename(f)}: {e}")

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
        max_weeks = LEAGUE_WEEKS.get(league, 38)
        truly_complete = []
        for s in complete:
            s_matches = [m for m in matches if m.get("season") == s]
            weeks = set(m.get("week") for m in s_matches)
            if len(s_matches) >= max_weeks * 0.9:
                truly_complete.append(s)

        use_seasons = truly_complete[:num_seasons] if truly_complete else complete[:num_seasons]
        if not use_seasons:
            use_seasons = seasons[:num_seasons]

        selected = [m for m in matches if m.get("season") in use_seasons]
        filtered[league] = selected
        print(f"  ✓ {league:<10}: {len(selected):>5} matches from seasons {use_seasons}")

    return filtered


def load_racing(data_dir):
    files = sorted(glob.glob(os.path.join(data_dir, "sportybet_racing_*.json")))
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
            print(f"  ⚠  {os.path.basename(f)}: {e}")

    by_sport = defaultdict(list)
    for r in records:
        by_sport[r.get("sport", "Unknown")].append(r)

    for sport, races in by_sport.items():
        venues = set(r.get("race_name") for r in races)
        print(f"  ✓ {sport:<20}: {len(races):>5} races, venues: {', '.join(sorted(venues))}")

    return by_sport


# ═══════════════════════════════════════════════════════════
#  SECTION 2 — FOOTBALL: TEAM-LEVEL POISSON MODEL
# ═══════════════════════════════════════════════════════════

def build_team_models(league_data):
    models = {}

    for league, matches in league_data.items():
        if len(matches) < 50:
            continue

        n = len(matches)
        total_home_goals = sum(m["home_score"] for m in matches)
        total_away_goals = sum(m["away_score"] for m in matches)
        avg_home = total_home_goals / n
        avg_away = total_away_goals / n

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

            home_attack = (s["home_goals_for"] / hm) / max(avg_home, 0.01)
            home_defense = (s["home_goals_against"] / hm) / max(avg_away, 0.01)
            away_attack = (s["away_goals_for"] / am) / max(avg_away, 0.01)
            away_defense = (s["away_goals_against"] / am) / max(avg_home, 0.01)

            teams[team] = {
                "home_attack": home_attack,
                "home_defense": home_defense,
                "away_attack": away_attack,
                "away_defense": away_defense,
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


# ═══════════════════════════════════════════════════════════
#  SECTION 3 — POISSON PREDICTION ENGINE
# ═══════════════════════════════════════════════════════════

def poisson_pmf(k, lam):
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def predict_match(model, home_team, away_team, max_goals=7):
    teams = model["teams"]
    if home_team not in teams or away_team not in teams:
        return None

    ht = teams[home_team]
    at = teams[away_team]

    lambda_home = model["avg_home"] * ht["home_attack"] * at["away_defense"]
    lambda_away = model["avg_away"] * at["away_attack"] * ht["home_defense"]

    lambda_home = max(0.1, min(lambda_home, 5.0))
    lambda_away = max(0.1, min(lambda_away, 5.0))

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

    most_likely_home = round(lambda_home)
    most_likely_away = round(lambda_away)

    if home_win >= draw and home_win >= away_win:
        prediction = "Home Win"
    elif draw >= away_win:
        prediction = "Draw"
    else:
        prediction = "Away Win"

    return {
        "home_team": home_team,
        "away_team": away_team,
        "lambda_home": round(lambda_home, 2),
        "lambda_away": round(lambda_away, 2),
        "predicted_score": f"{most_likely_home}-{most_likely_away}",
        "home_win_pct": round(home_win * 100, 1),
        "draw_pct": round(draw * 100, 1),
        "away_win_pct": round(away_win * 100, 1),
        "over_1_5_pct": round(over_15 * 100, 1),
        "over_2_5_pct": round(over_25 * 100, 1),
        "over_3_5_pct": round(over_35 * 100, 1),
        "btts_pct": round(btts * 100, 1),
        "prediction": prediction,
        "confidence": round(max(home_win, draw, away_win) * 100, 1),
        "top_scores": [(f"{h}-{a}", round(p * 100, 1)) for (h, a), p in top_scores],
    }


# ═══════════════════════════════════════════════════════════
#  SECTION 4 — GENERATE ROUND-ROBIN FIXTURES
# ═══════════════════════════════════════════════════════════

def generate_round_robin(teams_list):
    teams = list(teams_list)
    n = len(teams)
    if n % 2 == 1:
        teams.append("BYE")
        n += 1

    schedule = []
    rotation = list(teams)

    for round_num in range(n - 1):
        round_matches = []
        for i in range(n // 2):
            home = rotation[i]
            away = rotation[n - 1 - i]
            if home != "BYE" and away != "BYE":
                round_matches.append((home, away))
        schedule.append(round_matches)
        rotation = [rotation[0]] + [rotation[-1]] + rotation[1:-1]

    reverse = []
    for round_matches in schedule:
        reverse.append([(a, h) for h, a in round_matches])

    return schedule + reverse


# ═══════════════════════════════════════════════════════════
#  SECTION 5 — PREDICT FULL SEASON
# ═══════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════
#  SECTION 6 — RACING MODEL
# ═══════════════════════════════════════════════════════════

def build_racing_models(racing_data):
    models = {}

    for sport, races in racing_data.items():
        by_venue = defaultdict(list)
        for r in races:
            by_venue[r.get("race_name", "Unknown")].append(r)

        for venue, venue_races in by_venue.items():
            n = len(venue_races)
            key = f"{sport} — {venue}"

            eo_results = []
            ou_results = []
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

            models[key] = {
                "sport": sport,
                "venue": venue,
                "n": n,
                "even_rate": even_count / len(eo_results) if eo_results else None,
                "odd_rate": (len(eo_results) - even_count) / len(eo_results) if eo_results else None,
                "eo_n": len(eo_results),
                "over_rate": over_count / len(ou_results) if ou_results else None,
                "under_rate": (len(ou_results) - over_count) / len(ou_results) if ou_results else None,
                "ou_n": len(ou_results),
                "avg_win_odds": sum(win_odds_list) / len(win_odds_list) if win_odds_list else None,
                "top_winners": runner_wins.most_common(10),
                "total_unique_runners": len(runner_appearances),
            }

    return models


def predict_racing_sessions(models, num_sessions=50):
    predictions = {}

    for key, m in models.items():
        preds = []

        eo_pred = None
        eo_conf = None
        if m["even_rate"] is not None:
            if m["even_rate"] > 0.52:
                eo_pred = "EVEN"
                eo_conf = m["even_rate"]
            elif m["odd_rate"] > 0.52:
                eo_pred = "ODD"
                eo_conf = m["odd_rate"]
            else:
                eo_pred = "EVEN" if m["even_rate"] >= 0.5 else "ODD"
                eo_conf = max(m["even_rate"], m["odd_rate"])

        ou_pred = None
        ou_conf = None
        if m["over_rate"] is not None:
            if m["over_rate"] > 0.52:
                ou_pred = "OVER"
                ou_conf = m["over_rate"]
            elif m["under_rate"] > 0.52:
                ou_pred = "UNDER"
                ou_conf = m["under_rate"]
            else:
                ou_pred = "OVER" if m["over_rate"] >= 0.5 else "UNDER"
                ou_conf = max(m["over_rate"], m["under_rate"])

        for i in range(1, num_sessions + 1):
            preds.append({
                "race_num": i,
                "eo_prediction": eo_pred,
                "eo_confidence": round(eo_conf * 100, 1) if eo_conf else None,
                "ou_prediction": ou_pred,
                "ou_confidence": round(ou_conf * 100, 1) if ou_conf else None,
                "top_runners": [name for name, _ in m["top_winners"][:5]],
            })

        predictions[key] = {
            "model": m,
            "predictions": preds,
        }

    return predictions


# ═══════════════════════════════════════════════════════════
#  SECTION 7 — DISPLAY
# ═══════════════════════════════════════════════════════════

def display_team_rankings(models):
    for league in FOOTBALL_LEAGUES:
        if league not in models:
            continue
        model = models[league]
        teams = model["teams"]

        power = []
        for team, s in teams.items():
            score = (s["home_attack"] + s["away_attack"]) / 2
            power.append((team, score, s))
        power.sort(key=lambda x: -x[1])

        w = 100
        print(f"\n{'═' * w}")
        print(f"  {league.upper()} — TEAM POWER RANKINGS (from {model['n']} matches)")
        print(f"{'═' * w}")
        print(f"  {'#':>2} {'Team':<5} {'Power':>6} {'HW%':>6} {'HD%':>6} {'AW%':>6} "
              f"{'HGpg':>5} {'AGpg':>5} {'HCpg':>5} {'ACpg':>5}")
        print(f"  {'─' * (w - 2)}")

        for rank, (team, pwr, s) in enumerate(power, 1):
            print(f"  {rank:>2} {team:<5} {pwr:>6.3f} "
                  f"{s['home_win_rate']*100:>5.1f}% {s['home_draw_rate']*100:>5.1f}% "
                  f"{s['away_win_rate']*100:>5.1f}% "
                  f"{s['home_gpg']:>5.2f} {s['away_gpg']:>5.2f} "
                  f"{s['home_conceded_pg']:>5.2f} {s['away_conceded_pg']:>5.2f}")
        print(f"{'═' * w}")


def display_season_preview(league, season_preds, table):
    w = 100
    print(f"\n{'═' * w}")
    print(f"  {league.upper()} — PREDICTED FINAL TABLE")
    print(f"{'═' * w}")
    print(f"  {'#':>2} {'Team':<5} {'P':>3} {'W':>3} {'D':>3} {'L':>3} "
          f"{'GF':>4} {'GA':>4} {'GD':>4} {'Pts':>4}")
    print(f"  {'─' * (w - 2)}")

    for rank, (team, s) in enumerate(table, 1):
        gd = s["GF"] - s["GA"]
        marker = ""
        if rank <= 4:
            marker = " *"
        elif rank >= len(table) - 2:
            marker = " !"
        print(f"  {rank:>2} {team:<5} {s['P']:>3} {s['W']:>3} {s['D']:>3} {s['L']:>3} "
              f"{s['GF']:>4} {s['GA']:>4} {gd:>+4} {s['Pts']:>4}{marker}")

    print(f"  {'─' * (w - 2)}")
    print(f"  * = Top 4  |  ! = Relegation zone")
    print(f"{'═' * w}")


def display_week_predictions(league, week_data, week_nums=None):
    if week_nums is None:
        week_nums = [1, 2, 3]

    for wp in week_data:
        if wp["week"] not in week_nums:
            continue

        print(f"\n  ── {league} Week {wp['week']} ──")
        print(f"  {'Home':<5} {'Score':>5} {'Away':<5}  {'Pred':<9} {'Conf':>5}  "
              f"{'HW%':>5} {'D%':>5} {'AW%':>5}  {'O2.5':>5} {'BTTS':>5}")

        for m in wp["matches"]:
            print(f"  {m['home_team']:<5} {m['predicted_score']:>5} {m['away_team']:<5}  "
                  f"{m['prediction']:<9} {m['confidence']:>4.1f}%  "
                  f"{m['home_win_pct']:>4.1f}% {m['draw_pct']:>4.1f}% {m['away_win_pct']:>4.1f}%  "
                  f"{m['over_2_5_pct']:>4.1f}% {m['btts_pct']:>4.1f}%")


def display_racing_predictions(racing_preds):
    w = 90
    print(f"\n{'═' * w}")
    print(f"  RACING PREDICTIONS (next 50 races per venue)")
    print(f"{'═' * w}")

    for key, data in sorted(racing_preds.items()):
        m = data["model"]
        p = data["predictions"][0] if data["predictions"] else None
        if not p:
            continue

        print(f"\n  {key}")
        print(f"  {'─' * (w - 4)}")
        print(f"  Based on {m['n']} historical races")

        if m["even_rate"] is not None:
            print(f"  Even/Odd split: Even {m['even_rate']*100:.1f}% | "
                  f"Odd {m['odd_rate']*100:.1f}% "
                  f"(n={m['eo_n']})")
            print(f"  → Prediction: {p['eo_prediction']} "
                  f"(confidence: {p['eo_confidence']:.1f}%)")

        if m["over_rate"] is not None:
            print(f"  Over/Under split: Over {m['over_rate']*100:.1f}% | "
                  f"Under {m['under_rate']*100:.1f}% "
                  f"(n={m['ou_n']})")
            print(f"  → Prediction: {p['ou_prediction']} "
                  f"(confidence: {p['ou_confidence']:.1f}%)")

        if m["top_winners"]:
            top = ", ".join(f"{name}({cnt})" for name, cnt in m["top_winners"][:5])
            print(f"  Most frequent winners: {top}")
            print(f"  Unique runners seen: {m['total_unique_runners']}")

        if m["avg_win_odds"] is not None:
            print(f"  Average win odds: {m['avg_win_odds']:.2f}")

    print(f"\n{'═' * w}")


# ═══════════════════════════════════════════════════════════
#  SECTION 8 — EXCEL OUTPUT
# ═══════════════════════════════════════════════════════════

def save_to_excel(all_football, all_tables, racing_preds, timestamp):
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        print("  ⚠ openpyxl not installed — skipping Excel output")
        return

    os.makedirs(OUT_DIR, exist_ok=True)

    thin = Side(style="thin", color="BBBBBB")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    hdr_fill = PatternFill("solid", fgColor="1F3864")
    hdr_font = Font(bold=True, color="FFFFFF", size=11)
    hdr_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    center = Alignment(horizontal="center", vertical="center")

    hw_fill = PatternFill("solid", fgColor="C6EFCE")
    draw_fill = PatternFill("solid", fgColor="FFEB9C")
    aw_fill = PatternFill("solid", fgColor="F4CCCC")
    high_conf = PatternFill("solid", fgColor="B6D7A8")

    # ── Football Predictions Workbook ──
    if all_football:
        path = os.path.join(OUT_DIR, f"Season_Predictions_Football_{timestamp}.xlsx")
        wb = openpyxl.Workbook()
        wb.remove(wb.active)

        for league in FOOTBALL_LEAGUES:
            if league not in all_football:
                continue

            season_data, table = all_football[league], all_tables[league]

            # --- Predicted Table Sheet ---
            ws = wb.create_sheet(f"{league} Table")
            headers = ["#", "Team", "P", "W", "D", "L", "GF", "GA", "GD", "Pts"]
            for ci, h in enumerate(headers, 1):
                cell = ws.cell(row=1, column=ci, value=h)
                cell.fill = hdr_fill
                cell.font = hdr_font
                cell.alignment = hdr_align
            ws.row_dimensions[1].height = 24

            for ri, (team, s) in enumerate(table, 2):
                gd = s["GF"] - s["GA"]
                rank = ri - 1
                vals = [rank, team, s["P"], s["W"], s["D"], s["L"],
                        s["GF"], s["GA"], gd, s["Pts"]]
                fill = None
                if rank <= 4:
                    fill = hw_fill
                elif rank >= len(table) - 2:
                    fill = aw_fill

                for ci, v in enumerate(vals, 1):
                    cell = ws.cell(row=ri, column=ci, value=v)
                    cell.border = border
                    cell.alignment = center
                    if fill:
                        cell.fill = fill

            widths = [4, 6, 4, 4, 4, 4, 5, 5, 5, 5]
            for ci, w in enumerate(widths, 1):
                ws.column_dimensions[get_column_letter(ci)].width = w
            ws.freeze_panes = "A2"

            # --- Match Predictions Sheet ---
            ws2 = wb.create_sheet(f"{league} Matches")
            m_headers = ["Week", "Home", "Score", "Away", "Prediction", "Confidence",
                         "HW%", "Draw%", "AW%", "O1.5%", "O2.5%", "O3.5%", "BTTS%",
                         "Top Score 1", "Top Score 2", "Top Score 3"]
            for ci, h in enumerate(m_headers, 1):
                cell = ws2.cell(row=1, column=ci, value=h)
                cell.fill = hdr_fill
                cell.font = hdr_font
                cell.alignment = hdr_align
            ws2.row_dimensions[1].height = 24

            ri = 2
            for wp in season_data:
                for m in wp["matches"]:
                    fill = None
                    if m["prediction"] == "Home Win":
                        fill = hw_fill
                    elif m["prediction"] == "Draw":
                        fill = draw_fill
                    elif m["prediction"] == "Away Win":
                        fill = aw_fill

                    top_scores = m.get("top_scores", [])
                    ts1 = f"{top_scores[0][0]} ({top_scores[0][1]}%)" if len(top_scores) > 0 else ""
                    ts2 = f"{top_scores[1][0]} ({top_scores[1][1]}%)" if len(top_scores) > 1 else ""
                    ts3 = f"{top_scores[2][0]} ({top_scores[2][1]}%)" if len(top_scores) > 2 else ""

                    vals = [m["week"], m["home_team"], m["predicted_score"], m["away_team"],
                            m["prediction"], m["confidence"],
                            m["home_win_pct"], m["draw_pct"], m["away_win_pct"],
                            m["over_1_5_pct"], m["over_2_5_pct"], m["over_3_5_pct"],
                            m["btts_pct"], ts1, ts2, ts3]

                    for ci, v in enumerate(vals, 1):
                        cell = ws2.cell(row=ri, column=ci, value=v)
                        cell.border = border
                        cell.alignment = center
                        if fill:
                            cell.fill = fill
                        if ci == 6 and isinstance(v, (int, float)) and v >= 50:
                            cell.fill = high_conf
                            cell.font = Font(bold=True)

                    ri += 1

            m_widths = [5, 5, 6, 5, 10, 10, 6, 6, 6, 6, 6, 6, 6, 14, 14, 14]
            for ci, w in enumerate(m_widths, 1):
                ws2.column_dimensions[get_column_letter(ci)].width = w
            ws2.freeze_panes = "A2"
            ws2.auto_filter.ref = ws2.dimensions

        wb.save(path)
        print(f"  ✓ Football predictions: {path}")

    # ── Racing Predictions Workbook ──
    if racing_preds:
        path = os.path.join(OUT_DIR, f"Season_Predictions_Racing_{timestamp}.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Racing Analysis"

        headers = ["Sport", "Venue", "Races", "Even%", "Odd%", "E/O Samples",
                    "E/O Prediction", "E/O Confidence",
                    "Over%", "Under%", "O/U Samples",
                    "O/U Prediction", "O/U Confidence",
                    "Avg Win Odds", "Top Runners"]
        for ci, h in enumerate(headers, 1):
            cell = ws.cell(row=1, column=ci, value=h)
            cell.fill = hdr_fill
            cell.font = hdr_font
            cell.alignment = hdr_align
        ws.row_dimensions[1].height = 24

        ri = 2
        for key, data in sorted(racing_preds.items()):
            m = data["model"]
            p = data["predictions"][0] if data["predictions"] else {}

            top_runners = ", ".join(f"{name}({cnt})" for name, cnt in m["top_winners"][:5])

            vals = [
                m["sport"], m["venue"], m["n"],
                round(m["even_rate"] * 100, 1) if m["even_rate"] is not None else "N/A",
                round(m["odd_rate"] * 100, 1) if m["odd_rate"] is not None else "N/A",
                m["eo_n"],
                p.get("eo_prediction", "N/A"),
                p.get("eo_confidence", "N/A"),
                round(m["over_rate"] * 100, 1) if m["over_rate"] is not None else "N/A",
                round(m["under_rate"] * 100, 1) if m["under_rate"] is not None else "N/A",
                m["ou_n"],
                p.get("ou_prediction", "N/A"),
                p.get("ou_confidence", "N/A"),
                round(m["avg_win_odds"], 2) if m["avg_win_odds"] is not None else "N/A",
                top_runners,
            ]

            for ci, v in enumerate(vals, 1):
                cell = ws.cell(row=ri, column=ci, value=v)
                cell.border = border
                cell.alignment = center
            ri += 1

        # Per-race predictions sheet
        ws2 = wb.create_sheet("Race Predictions")
        r_headers = ["#", "Sport / Venue", "E/O Prediction", "E/O Conf%",
                      "O/U Prediction", "O/U Conf%", "Watch Runners"]
        for ci, h in enumerate(r_headers, 1):
            cell = ws2.cell(row=1, column=ci, value=h)
            cell.fill = hdr_fill
            cell.font = hdr_font
            cell.alignment = hdr_align

        ri = 2
        for key, data in sorted(racing_preds.items()):
            for p in data["predictions"]:
                runners = ", ".join(p.get("top_runners", [])[:3])
                vals = [p["race_num"], key, p["eo_prediction"],
                        p["eo_confidence"], p["ou_prediction"],
                        p["ou_confidence"], runners]
                for ci, v in enumerate(vals, 1):
                    cell = ws2.cell(row=ri, column=ci, value=v)
                    cell.border = border
                    cell.alignment = center

                    if ci in (3, 5) and isinstance(v, str):
                        if v in ("EVEN", "OVER"):
                            cell.fill = hw_fill
                        elif v in ("ODD", "UNDER"):
                            cell.fill = draw_fill
                ri += 1

        r_widths = [5, 30, 14, 10, 14, 10, 30]
        for ci, w in enumerate(r_widths, 1):
            ws2.column_dimensions[get_column_letter(ci)].width = w
        ws2.freeze_panes = "A2"

        wb.save(path)
        print(f"  ✓ Racing predictions:  {path}")

    # ── Football Top O/U 2.5 Picks Workbook ──
    if all_football:
        path = os.path.join(OUT_DIR, f"Season_Top_OU25_Picks_{timestamp}.xlsx")
        wb = openpyxl.Workbook()
        wb.remove(wb.active)

        over_fill = PatternFill("solid", fgColor="C6EFCE")
        under_fill = PatternFill("solid", fgColor="D9E2F3")

        for league in FOOTBALL_LEAGUES:
            if league not in all_football:
                continue

            season_data = all_football[league]
            ws = wb.create_sheet(league)

            headers = ["Week", "Pick", "Home", "Away", "Pred Score",
                       "O2.5%", "U2.5%", "O1.5%", "O3.5%", "BTTS%",
                       "Exp Goals", "Confidence"]
            for ci, h in enumerate(headers, 1):
                cell = ws.cell(row=1, column=ci, value=h)
                cell.fill = hdr_fill
                cell.font = hdr_font
                cell.alignment = hdr_align
            ws.row_dimensions[1].height = 24

            ri = 2
            for wp in season_data:
                if not wp["matches"]:
                    continue

                best = None
                best_edge = 0
                for m in wp["matches"]:
                    o25 = m["over_2_5_pct"]
                    u25 = 100 - o25
                    edge = abs(o25 - 50)
                    if edge > best_edge:
                        best_edge = edge
                        best = m

                if not best:
                    continue

                o25 = best["over_2_5_pct"]
                u25 = 100 - o25
                pick = "OVER 2.5" if o25 > 50 else "UNDER 2.5"
                conf = max(o25, u25)
                exp_goals = best["lambda_home"] + best["lambda_away"]

                vals = [wp["week"], pick, best["home_team"], best["away_team"],
                        best["predicted_score"], round(o25, 1), round(u25, 1),
                        best["over_1_5_pct"], best["over_3_5_pct"],
                        best["btts_pct"], round(exp_goals, 2), round(conf, 1)]

                fill = over_fill if pick == "OVER 2.5" else under_fill
                for ci, v in enumerate(vals, 1):
                    cell = ws.cell(row=ri, column=ci, value=v)
                    cell.border = border
                    cell.alignment = center
                    cell.fill = fill
                    if ci == 2:
                        cell.font = Font(bold=True)
                    if ci == 12 and isinstance(v, (int, float)) and v >= 60:
                        cell.font = Font(bold=True, color="006100")

                ri += 1

            col_widths = [6, 12, 6, 6, 10, 7, 7, 7, 7, 7, 10, 11]
            for ci, cw in enumerate(col_widths, 1):
                ws.column_dimensions[get_column_letter(ci)].width = cw
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions

        wb.save(path)
        print(f"  ✓ Top O/U 2.5 picks:   {path}")


# ═══════════════════════════════════════════════════════════
#  SECTION 9 — LIVE SCRAPING INTEGRATION
#  Imports core functions from existing scrapers so one
#  browser session can: scrape history → scrape upcoming
#  odds → feed into predictions with real fixtures.
# ═══════════════════════════════════════════════════════════

def _import_scrapers():
    project = os.path.dirname(os.path.abspath(__file__))
    if project not in sys.path:
        sys.path.insert(0, project)

    results_mod = importlib.import_module("sportybet_virtual_scraper_v1")
    odds_mod = importlib.import_module("sportybet_virtual_odds_v1")
    return results_mod, odds_mod


def scrape_fresh_data(data_dir, target_seasons=4, headless=False,
                      do_football=True, do_racing=True):
    results_mod, odds_mod = _import_scrapers()
    os.makedirs(data_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    pw = browser = ctx = page = frame = None
    upcoming_odds = {}

    try:
        print("\n  Opening browser (stealth mode)...")
        pw, browser, ctx, page = results_mod.create_stealth_browser(
            headless=headless, proxy_server=None, stealth_mode="advanced"
        )

        site_url = results_mod.SITE_URL
        frame = None

        for attempt in range(1, 4):
            print(f"\n  Loading {site_url} (attempt {attempt}/3)...")
            page.goto(site_url, timeout=60000, wait_until="domcontentloaded")
            print("  Waiting for virtual iframe to initialise...")
            time.sleep(12)

            frame = results_mod.wait_and_find_virtual_frame(page, timeout=45000)

            is_real_frame = (frame is not page
                            and hasattr(frame, 'url')
                            and frame.url
                            and "sportybet.com/ng/virtual" not in frame.url
                            and frame.url != "about:blank")
            if is_real_frame:
                print(f"  ✓ Virtual iframe found on attempt {attempt}")
                break

            print(f"  ⚠ Iframe not found on attempt {attempt} — refreshing...")
            page.reload(timeout=60000, wait_until="domcontentloaded")
            time.sleep(5)

        if frame is None or frame is page:
            print("  ⚠ Virtual iframe never loaded after 3 attempts.")
            print("    Try running standalone scrapers instead:")
            print("      python sportybet_virtual_scraper_v1.py")
            print("      python sportybet_virtual_odds_v1.py")
            raise RuntimeError("Virtual iframe failed to load")

        time.sleep(2)

        # ── Phase 1: Scrape results history ────────────────
        if do_football:
            print(f"\n{'═' * 60}")
            print(f"  PHASE 1: SCRAPING {target_seasons} SEASONS OF RESULTS HISTORY")
            print(f"{'═' * 60}")

            for li, league in enumerate(FOOTBALL_LEAGUES):
                print(f"\n{'─' * 50}")
                print(f"  LEAGUE {li+1}/{len(FOOTBALL_LEAGUES)}: {league}")
                print(f"{'─' * 50}")

                try:
                    matches, _details = results_mod.scrape_league_results(
                        frame, league,
                        target_seasons=target_seasons,
                        first_league=(li == 0),
                        detail_seasons=0,
                    )
                    if matches:
                        fname = os.path.join(
                            data_dir, f"sportybet_results_{league.lower()}_{ts}.json"
                        )
                        with open(fname, "w", encoding="utf-8") as f:
                            json.dump(matches, f, indent=2, ensure_ascii=False)
                        print(f"  ✓ Saved {len(matches)} matches → {fname}")
                    else:
                        print(f"  ⚠ No matches found for {league}")
                except Exception as e:
                    print(f"  ⚠ Error scraping {league} results: {e}")

                if li < len(FOOTBALL_LEAGUES) - 1:
                    print(f"  ⏳ Cooldown 10s...")
                    time.sleep(10)

            # ── Phase 2: Scrape upcoming odds ──────────────
            print(f"\n{'═' * 60}")
            print(f"  PHASE 2: SCRAPING UPCOMING FIXTURES & ODDS")
            print(f"{'═' * 60}")

            for li, league in enumerate(FOOTBALL_LEAGUES):
                print(f"\n{'─' * 50}")
                print(f"  ODDS {li+1}/{len(FOOTBALL_LEAGUES)}: {league}")
                print(f"{'─' * 50}")

                try:
                    odds = odds_mod.scrape_league_odds(frame, league)
                    if odds:
                        upcoming_odds[league] = odds
                        fname = os.path.join(
                            data_dir, f"sportybet_odds_{league.lower()}_{ts}.json"
                        )
                        with open(fname, "w", encoding="utf-8") as f:
                            json.dump(odds, f, indent=2, ensure_ascii=False)
                        print(f"  ✓ Saved {len(odds)} upcoming matches → {fname}")
                    else:
                        print(f"  ⚠ No odds found for {league}")
                except Exception as e:
                    print(f"  ⚠ Error scraping {league} odds: {e}")

                if li < len(FOOTBALL_LEAGUES) - 1:
                    time.sleep(10)

        # ── Phase 3: Scrape racing (if requested) ──────────
        if do_racing:
            print(f"\n{'═' * 60}")
            print(f"  PHASE 3: SCRAPING RACING RESULTS")
            print(f"{'═' * 60}")

            try:
                racing_mod = importlib.import_module("sportybet_virtual_racing_scraper_v1") \
                    if os.path.exists("sportybet_virtual_racing_scraper_v1.py") else None
            except Exception:
                racing_mod = None

            if racing_mod and hasattr(racing_mod, "scrape_sport_results"):
                for sport in ["Greyhound Racing", "Horse Racing",
                              "Speedway Racing", "Motorbike Racing"]:
                    try:
                        races = racing_mod.scrape_sport_results(frame, sport, days_back=7)
                        if races:
                            slug = sport.lower().replace(" ", "_")
                            fname = os.path.join(
                                data_dir, f"sportybet_racing_{slug}_{ts}.json"
                            )
                            with open(fname, "w", encoding="utf-8") as f:
                                json.dump(races, f, indent=2, ensure_ascii=False)
                            print(f"  ✓ Saved {len(races)} {sport} races → {fname}")
                    except Exception as e:
                        print(f"  ⚠ Error scraping {sport}: {e}")
            else:
                print("  ⚠ Racing scraper not available — run it separately:")
                print("    python sportybet_virtual_racing_scraper_v1.py")

    except Exception as e:
        print(f"\n  ⚠ Scraping error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        try:
            if browser:
                browser.close()
            if pw:
                pw.stop()
        except:
            pass

    return upcoming_odds


# ═══════════════════════════════════════════════════════════
#  SECTION 10 — PREDICT WITH REAL UPCOMING FIXTURES
#  The odds scraper returns matches with week strings like
#  "Week 28 - England". We keep the original week label so
#  users can find the exact matches on the platform.
# ═══════════════════════════════════════════════════════════

def _parse_week_num(week_val):
    if isinstance(week_val, int):
        return week_val
    if isinstance(week_val, str):
        import re
        wm = re.search(r'(\d+)', week_val)
        return int(wm.group(1)) if wm else None
    return None


def predict_upcoming_with_model(model, upcoming_matches, league):
    predictions = []
    for match in upcoming_matches:
        home = match.get("home_team", "")
        away = match.get("away_team", "")
        if not home or not away:
            continue

        pred = predict_match(model, home, away)
        if pred is None:
            continue

        raw_week = match.get("week", "")
        pred["week"] = _parse_week_num(raw_week)
        pred["week_label"] = raw_week
        pred["position"] = match.get("position")

        pred["live_home_odds"] = match.get("home_odds")
        pred["live_draw_odds"] = match.get("draw_odds")
        pred["live_away_odds"] = match.get("away_odds")
        pred["live_over_15"] = match.get("over_1.5")
        pred["live_under_15"] = match.get("under_1.5")
        pred["live_over_25"] = match.get("over_2.5")
        pred["live_under_25"] = match.get("under_2.5")
        pred["live_over_35"] = match.get("over_3.5")
        pred["live_under_35"] = match.get("under_3.5")
        pred["live_over_45"] = match.get("over_4.5")
        pred["live_under_45"] = match.get("under_4.5")

        for threshold, model_key in [("15", "over_1_5_pct"), ("25", "over_2_5_pct"),
                                      ("35", "over_3_5_pct")]:
            live_o = pred.get(f"live_over_{threshold}")
            live_u = pred.get(f"live_under_{threshold}")
            hist_o = pred.get(model_key, 0)
            if live_o and live_o > 1:
                pred[f"over_{threshold}_edge"] = round(hist_o - (1 / live_o * 100), 1)
            if live_u and live_u > 1:
                pred[f"under_{threshold}_edge"] = round((100 - hist_o) - (1 / live_u * 100), 1)

        if pred.get("live_home_odds") and pred["live_home_odds"] > 1:
            pred["hw_edge"] = round(pred["home_win_pct"] - (1 / pred["live_home_odds"] * 100), 1)
        if pred.get("live_draw_odds") and pred["live_draw_odds"] > 1:
            pred["d_edge"] = round(pred["draw_pct"] - (1 / pred["live_draw_odds"] * 100), 1)
        if pred.get("live_away_odds") and pred["live_away_odds"] > 1:
            pred["aw_edge"] = round(pred["away_win_pct"] - (1 / pred["live_away_odds"] * 100), 1)

        predictions.append(pred)

    return predictions


def save_upcoming_predictions(all_upcoming, timestamp):
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        print("  ⚠ openpyxl not installed — skipping upcoming Excel")
        return

    os.makedirs(OUT_DIR, exist_ok=True)

    thin = Side(style="thin", color="BBBBBB")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    hdr_fill = PatternFill("solid", fgColor="1F3864")
    hdr_font = Font(bold=True, color="FFFFFF", size=11)
    hdr_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    center = Alignment(horizontal="center", vertical="center")

    hw_fill = PatternFill("solid", fgColor="C6EFCE")
    draw_fill = PatternFill("solid", fgColor="FFEB9C")
    aw_fill = PatternFill("solid", fgColor="F4CCCC")
    edge_fill = PatternFill("solid", fgColor="B6D7A8")
    neg_edge = PatternFill("solid", fgColor="F4CCCC")
    week_sep = PatternFill("solid", fgColor="D9D9D9")

    # ── File 1: Full upcoming predictions ──────────────────
    path = os.path.join(OUT_DIR, f"Upcoming_Predictions_{timestamp}.xlsx")
    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    for league, preds in all_upcoming.items():
        if not preds:
            continue

        ws = wb.create_sheet(league)
        headers = ["#", "Week", "Home", "Away", "Pred Score", "Prediction", "Conf%",
                    "HW%", "D%", "AW%",
                    "O1.5%", "O2.5%", "O3.5%", "BTTS%",
                    "Live HW", "Live D", "Live AW",
                    "Live O2.5", "Live U2.5",
                    "HW Edge", "D Edge", "AW Edge",
                    "O2.5 Edge", "U2.5 Edge"]
        for ci, h in enumerate(headers, 1):
            cell = ws.cell(row=1, column=ci, value=h)
            cell.fill = hdr_fill
            cell.font = hdr_font
            cell.alignment = hdr_align
        ws.row_dimensions[1].height = 28

        preds.sort(key=lambda m: (m.get("week") or 0, m.get("position") or 0))

        prev_week = None
        ri = 2
        for m in preds:
            cur_week = m.get("week")
            if prev_week is not None and cur_week != prev_week:
                for ci in range(1, len(headers) + 1):
                    cell = ws.cell(row=ri, column=ci)
                    cell.fill = week_sep
                    cell.border = border
                ri += 1
            prev_week = cur_week

            fill = hw_fill if m["prediction"] == "Home Win" else \
                   draw_fill if m["prediction"] == "Draw" else aw_fill

            vals = [
                m.get("position", ""), m.get("week", ""),
                m["home_team"], m["away_team"],
                m["predicted_score"], m["prediction"], m["confidence"],
                m["home_win_pct"], m["draw_pct"], m["away_win_pct"],
                m["over_1_5_pct"], m["over_2_5_pct"], m["over_3_5_pct"],
                m["btts_pct"],
                m.get("live_home_odds", ""), m.get("live_draw_odds", ""),
                m.get("live_away_odds", ""),
                m.get("live_over_25", ""), m.get("live_under_25", ""),
                m.get("hw_edge", ""), m.get("d_edge", ""),
                m.get("aw_edge", ""),
                m.get("over_25_edge", ""), m.get("under_25_edge", ""),
            ]

            for ci, v in enumerate(vals, 1):
                cell = ws.cell(row=ri, column=ci, value=v)
                cell.border = border
                cell.alignment = center
                cell.fill = fill

                if ci >= 20 and isinstance(v, (int, float)):
                    if v > 3:
                        cell.fill = edge_fill
                        cell.font = Font(bold=True, color="006100")
                    elif v < -3:
                        cell.fill = neg_edge

            ri += 1

        col_widths = [4, 6, 5, 5, 9, 10, 6,
                      6, 5, 6, 6, 6, 6, 6,
                      8, 8, 8, 8, 8,
                      8, 8, 8, 9, 9]
        for ci, cw in enumerate(col_widths, 1):
            ws.column_dimensions[get_column_letter(ci)].width = cw
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions

    wb.save(path)
    print(f"  ✓ Upcoming predictions: {path}")

    # ── File 2: Top O/U 2.5 pick per upcoming gameweek ────
    path2 = os.path.join(OUT_DIR, f"Upcoming_Top_OU25_Picks_{timestamp}.xlsx")
    wb2 = openpyxl.Workbook()
    wb2.remove(wb2.active)

    over_fill = PatternFill("solid", fgColor="C6EFCE")
    under_fill = PatternFill("solid", fgColor="D9E2F3")

    for league, preds in all_upcoming.items():
        if not preds:
            continue

        ws = wb2.create_sheet(league)
        headers = ["Week", "Pick", "#", "Home", "Away", "Pred Score",
                    "O2.5%", "U2.5%", "O1.5%", "O3.5%", "BTTS%",
                    "Exp Goals", "Confidence",
                    "Live O2.5", "Live U2.5", "O2.5 Edge", "U2.5 Edge"]
        for ci, h in enumerate(headers, 1):
            cell = ws.cell(row=1, column=ci, value=h)
            cell.fill = hdr_fill
            cell.font = hdr_font
            cell.alignment = hdr_align
        ws.row_dimensions[1].height = 24

        by_week = defaultdict(list)
        for m in preds:
            by_week[m.get("week") or 0].append(m)

        ri = 2
        for week_num in sorted(by_week.keys()):
            week_matches = by_week[week_num]

            best = None
            best_edge = 0
            for m in week_matches:
                o25 = m["over_2_5_pct"]
                edge = abs(o25 - 50)
                if edge > best_edge:
                    best_edge = edge
                    best = m

            if not best:
                continue

            o25 = best["over_2_5_pct"]
            u25 = 100 - o25
            pick = "OVER 2.5" if o25 > 50 else "UNDER 2.5"
            conf = max(o25, u25)
            exp_goals = best["lambda_home"] + best["lambda_away"]

            vals = [week_num, pick, best.get("position", ""),
                    best["home_team"], best["away_team"],
                    best["predicted_score"], round(o25, 1), round(u25, 1),
                    best["over_1_5_pct"], best["over_3_5_pct"],
                    best["btts_pct"], round(exp_goals, 2), round(conf, 1),
                    best.get("live_over_25", ""), best.get("live_under_25", ""),
                    best.get("over_25_edge", ""), best.get("under_25_edge", "")]

            fill = over_fill if pick == "OVER 2.5" else under_fill
            for ci, v in enumerate(vals, 1):
                cell = ws.cell(row=ri, column=ci, value=v)
                cell.border = border
                cell.alignment = center
                cell.fill = fill
                if ci == 2:
                    cell.font = Font(bold=True)
                if ci == 13 and isinstance(v, (int, float)) and v >= 60:
                    cell.font = Font(bold=True, color="006100")
                if ci in (16, 17) and isinstance(v, (int, float)) and v > 3:
                    cell.fill = edge_fill
                    cell.font = Font(bold=True, color="006100")

            ri += 1

        col_widths = [6, 12, 4, 5, 5, 9, 7, 7, 7, 7, 7, 9, 9, 8, 8, 9, 9]
        for ci, cw in enumerate(col_widths, 1):
            ws.column_dimensions[get_column_letter(ci)].width = cw
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions

    wb2.save(path2)
    print(f"  ✓ Upcoming O/U 2.5 picks: {path2}")


# ═══════════════════════════════════════════════════════════
#  SECTION 11 — MAIN
# ═══════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="Sportybet Virtual Season Predictor v1")
    ap.add_argument("--no-scrape", action="store_true",
                    help="Skip scraping, predict from saved data only")
    ap.add_argument("--headless", action="store_true", help="Run browser headless")
    ap.add_argument("--seasons", type=int, default=4,
                    help="Number of past seasons to use (default 4)")
    ap.add_argument("--football", action="store_true", help="Football only")
    ap.add_argument("--racing", action="store_true", help="Racing only")
    ap.add_argument("--data-dir", default=DATA_DIR, help="Data directory")
    ap.add_argument("--show-weeks", type=str, default="1,2,3",
                    help="Comma-separated week numbers to display in detail")
    args = ap.parse_args()

    do_football = True
    do_racing = True
    if args.football and not args.racing:
        do_racing = False
    if args.racing and not args.football:
        do_football = False

    show_weeks = [int(x) for x in args.show_weeks.split(",")]

    w = 70
    print("═" * w)
    print("  SPORTYBET VIRTUAL SEASON PREDICTOR  v1")
    print(f"  {datetime.now().strftime('%Y-%m-%d  %H:%M:%S')}")
    do_scrape = not args.no_scrape
    print(f"  Mode: {'SCRAPE + PREDICT' if do_scrape else 'PREDICT (from saved data)'}")
    print(f"  Using {args.seasons} past seasons for model training")
    print(f"  Data: {os.path.abspath(args.data_dir)}")
    print("═" * w)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    all_football = {}
    all_tables = {}
    all_upcoming = {}
    racing_preds = {}
    upcoming_odds = {}

    # ── Phase 0: Scrape (default) ──────────────────────────
    if do_scrape:
        upcoming_odds = scrape_fresh_data(
            args.data_dir,
            target_seasons=args.seasons,
            headless=args.headless,
            do_football=do_football,
            do_racing=do_racing,
        )

    # ── Load saved odds if we didn't scrape ──────────────
    if not do_scrape and do_football and not upcoming_odds:
        odds_files = sorted(glob.glob(os.path.join(args.data_dir, "sportybet_odds_*.json")))
        if odds_files:
            print("\n  Loading saved odds files...")
            for f in odds_files:
                try:
                    with open(f, encoding="utf-8") as fp:
                        data = json.load(fp)
                    if isinstance(data, list) and data:
                        league = data[0].get("league", "")
                        if league and league not in upcoming_odds:
                            upcoming_odds[league] = data
                            print(f"  ✓ {league}: {len(data)} matches from {os.path.basename(f)}")
                except Exception as e:
                    print(f"  ⚠ {os.path.basename(f)}: {e}")

    # ── Phase 1: Load & model ────────────────────────────
    if do_football:
        print("\n  Loading football data...")
        league_data = load_football(args.data_dir, args.seasons)

        if league_data:
            print("\n  Building team-level Poisson models...")
            models = build_team_models(league_data)

            display_team_rankings(models)

            # ── PRIMARY: Predict real upcoming fixtures ───
            if upcoming_odds:
                print(f"\n{'═' * w}")
                print("  UPCOMING MATCH PREDICTIONS (real fixtures from platform)")
                print(f"{'═' * w}")
                for league in FOOTBALL_LEAGUES:
                    if league not in models or league not in upcoming_odds:
                        continue
                    preds = predict_upcoming_with_model(
                        models[league], upcoming_odds[league], league
                    )
                    if preds:
                        all_upcoming[league] = preds
                        weeks = sorted(set(p.get("week") for p in preds if p.get("week")))
                        print(f"\n  {league}: {len(preds)} matches across "
                              f"Week(s) {', '.join(str(w2) for w2 in weeks)}")
                        for p in preds:
                            pred_tag = p["prediction"][:1]
                            o25 = p["over_2_5_pct"]
                            edge_str = ""
                            e = p.get("over_25_edge") or p.get("under_25_edge")
                            if e and abs(e) > 2:
                                edge_str = f"  edge {e:+.1f}%"
                            print(f"    W{p.get('week','?'):>2} #{p.get('position',''):>2} "
                                  f"{p['home_team']:<4} v {p['away_team']:<4}  "
                                  f"→ {p['predicted_score']}  {pred_tag} {p['confidence']:.0f}%  "
                                  f"O2.5={o25:.0f}%{edge_str}")

            # ── SECONDARY: Season simulation ──────────────
            if not args.no_scrape or not upcoming_odds:
                print(f"\n{'═' * w}")
                print("  SEASON SIMULATION (generated round-robin fixtures)")
                print(f"{'═' * w}")
                for league in FOOTBALL_LEAGUES:
                    if league not in models:
                        continue
                    season_preds, table = predict_season(models[league], league)
                    all_football[league] = season_preds
                    all_tables[league] = table
                    display_season_preview(league, season_preds, table)

    # ── Racing ────────────────────────────────────────────
    if do_racing:
        print("\n  Loading racing data...")
        racing_data = load_racing(args.data_dir)

        if racing_data:
            print("\n  Building racing models...")
            racing_models = build_racing_models(racing_data)
            racing_preds = predict_racing_sessions(racing_models)
            display_racing_predictions(racing_preds)

    # ── Save to Excel ─────────────────────────────────────
    print("\n  Saving predictions to Excel...")

    if all_upcoming:
        save_upcoming_predictions(all_upcoming, timestamp)

    if all_football:
        save_to_excel(all_football, all_tables, racing_preds, timestamp)
    elif racing_preds:
        save_to_excel({}, {}, racing_preds, timestamp)

    print(f"\n{'═' * w}")
    print("  DONE")
    print(f"  Output folder: {os.path.abspath(OUT_DIR)}")
    print(f"  Files generated:")
    if all_upcoming:
        print(f"    ★ Upcoming_Predictions         — real fixtures + model + live odds + edge")
        print(f"    ★ Upcoming_Top_OU25_Picks      — best O/U 2.5 pick per upcoming gameweek")
    if all_football:
        print(f"      Season_Predictions_Football  — full season simulation")
        print(f"      Season_Top_OU25_Picks        — simulated O/U 2.5 picks (round-robin)")
    if racing_preds:
        print(f"      Season_Predictions_Racing    — racing analysis + predictions")
    print(f"{'═' * w}\n")


if __name__ == "__main__":
    main()
