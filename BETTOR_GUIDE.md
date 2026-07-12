# Auto-Bettor Guide

Bets the predictor's verified SINGLE pick each gameweek with martingale
staking, inside your configured time windows.

## How to run

1. Start the predictor app first (it feeds the picks):
   `python app.py`
2. In a SECOND terminal:
   `python sportybet_auto_bettor.py`
3. A browser window opens. **Log in yourself** (the bot never sees your
   password; login is remembered in `bettor_profile/` for next time).
4. Watch the terminal — every decision is logged.

## First runs: DRY RUN

`bettor_config.json` ships with `"dry_run": true` — the bot does the whole
cycle (pick, build slip, verify, martingale math, settle, streaks) but never
presses Place Bet. Run at least one full window like this and check the log
feels right. Then set `"dry_run": false` for real money.

## Config (bettor_config.json)

| key | meaning |
|---|---|
| base_stake | first stake of every martingale sequence (NGN) |
| max_stake | hard cap — if the streak needs more, the bot STOPS rather than bet a stake that can't recover |
| daily_loss_limit | stop for the day at this much down |
| daily_profit_target | stop for the day at this much up |
| betting_windows | e.g. `[["07:00","10:00"],["22:00","23:59"]]` — edit anytime (restart to apply) |
| odds_tolerance | max difference between the pick's odds and the slip's odds |
| min_odds / max_odds | picks outside this range are skipped |

## Behavior rules

- New bets only START inside a window; a losing streak is chased past the
  window end until the first WIN settles, then it stops (loss limit still
  applies while chasing).
- The next bet is only placed after the previous one is SETTLED and verified
  in **My Bets** (source of truth). Unknown result = the bot stops and tells
  you to check manually — it never guesses.
- Every slip is verified before staking: exactly 1 selection, right match,
  right week, right market, right selection, odds within tolerance,
  potential return matches stake x odds. Any mismatch = no bet, skip GW.
- A crash/restart can never double-bet: a marker written before each
  placement is reconciled against My Bets on startup.
- Timing is randomized (pauses, typing speed) to look human.
- Any daily stop (profit target, loss limit, stake cap) EXITS the program.
  Starting it again is your explicit consent to continue.

## Files

- `data/bettor/state.json` — day ledger, current streak
- `data/bettor/bets.jsonl` — every bet ever (dry-run flagged)
- `bettor_profile/` — the logged-in browser profile (keep private!)
