# SportyPredict — What to do when it's stuck / not updating

The app self-heals from most problems (bad internet, sleep/hibernate, the
site rejecting sessions) within ~10 minutes. Only act if it's been stuck
longer than that.

## Step 1 — Read the [Health] line (tells you WHAT is broken)

In the app's console window (or http://127.0.0.1:5000/api/status), find the
latest line like:

    [Health] models_rebuild: ok 09:05 | odds_scrape: ok 09:03 | results_scrape: !! 08:40 error...

- `ok <time>` = that part worked at that time.
- `!!` = that part is failing, with the reason.
- If everything says ok with recent times, the app is fine — the site itself
  may just be slow.

Common reasons you'll see:
- `CORE-1002 login error` — the games site rejected the session. The app
  retries automatically; typically recovers in 1-5 minutes.
- `Watchdog ... killing its browser` — a hung browser was auto-killed.
  Normal after internet drops or waking from sleep; recovers on its own.
- `0 leagues scraped` — page layout problem or no internet; the app keeps
  retrying every cycle.

## Step 2 — If stuck >10 min: double-click `fix_stuck.bat`

It kills the app AND the invisible leftover scraper browsers (the usual
reason a plain restart doesn't help), waits, and restarts the app in a
visible window. This fixes ~95% of stuck states.

> Why a plain restart sometimes doesn't work: killed scraper browsers can
> linger invisibly and hold sessions/resources. `fix_stuck.bat` clears them.

## Step 3 — If STILL stuck after fix_stuck.bat

1. Check your internet: open https://www.sportybet.com/ng/virtual/ in your
   normal browser. If the virtual games don't load there, the app can't
   scrape either — wait until the site works for you.
2. If the site shows "Login error ... CORE-1002" in your own browser too,
   the provider is blocking your network temporarily. Wait 10-15 minutes
   (or switch network/hotspot) and run `fix_stuck.bat` again.
3. Reboot the PC (clears every stuck process and network state), then start
   the app again.

## Notes

- Sleep/hibernate/shutdown while the app runs is SAFE — no data is lost
  (everything is saved to files as it happens). On wake the watchdog kills
  the frozen browsers and everything reconnects within ~10 minutes.
- Never delete the `data/` folder — that's your entire prediction history,
  accuracy records, and banker streak logs.
- The console window must stay open; closing it stops the app.
