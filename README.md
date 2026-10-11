# Arenal empty-court audit

Tracks public padel court availability at Arenal Meise and Arenal Grimbergen for two weeks and turns it into euro figures: cancelled court time that never got rebooked, and peak court time that stayed empty.

## Current setup: runs on GitHub

This repo runs itself: `.github/workflows/track.yml` runs `loop.sh`, which checks availability every 15 minutes for about 5h40m and then starts the next run, until 24 Oct 2026 12:00 (`AUDIT_STOP`). An hourly schedule restarts the chain if it ever breaks. It tracks every Arenal club with padel (the list is read from Arenal once a day), indoor courts only. Data stays on the runner between checks and is saved to `data/audit.sql`, with `report.html` and `directie.html` refreshed, every hour, which keeps the repo small. Public repo, so GitHub Actions minutes are free. Nothing needs to run on your computer. The steps below are only needed to run it yourself instead.

## Files

- `tracker.py` checks availability (next 14 days, both clubs) and logs changes to `arenal_audit.db`. Run it every 10 minutes.
- `report.py` reads the database and writes `report.html` (all numbers, per club), `directie.html` (a one-page Dutch version for Arenal's management: headline, ranking, empty-court heatmap, cancellations, scenarios) and `events.csv` (raw data).

No packages needed, Python 3.9 or newer. On Windows, also run `pip install tzdata` once.

## 1. Test it once

```
cd arenal-audit
python3 tracker.py
```

Expected output: `fetched 14/14 days, ... free blocks tracked`. If you see HTTP 400/403 errors instead, the API wants different headers; send me the error.

## 2. Run it every 10 minutes for two weeks

It needs a machine that stays on. A laptop that sleeps will leave gaps (the report shows them). Your $5 server is ideal.

**Linux server / Mac (cron):** run `crontab -e` and add (adjust the path):

```
*/10 * * * * cd /path/to/arenal-audit && /usr/bin/python3 tracker.py >> tracker.log 2>&1
```

**Windows (Task Scheduler):** create a basic task, trigger daily, then in its properties set "Repeat task every 10 minutes" for "Indefinitely". Action: program `python`, arguments `tracker.py`, start in = the arenal-audit folder.

That's 14 small requests per run, with a pause between each.

## 3. Read the results (any time)

```
python3 report.py
```

Open `report.html`. Projections per month appear once there are 3+ days of data. After two weeks you have your opening line for the clubs.

## How it counts

- **Indoor courts only.** Once a day the tracker reads Arenal's court list and skips every court marked outdoor ("Outdoor" / "Buiten"): currently Meise courts 6, 7, 8 and Grimbergen court 6 (Joma). That leaves 5 indoor courts in Meise and 6 in Grimbergen. To also skip covered courts, add `"covered"` and `"overdekt"` to `EXCLUDE_ENVIRONMENTS` in `tracker.py`. If you already ran an earlier version, outdoor data in the database is removed automatically on the next run.
- Availability is stored as free 30-minute blocks per court (only time that can actually be booked, so small gaps between bookings don't count).
- **opened**: court time that was booked became free again, i.e. a cancellation.
- **booked**: free court time got booked.
- **expired_empty**: court time reached its start while still free, i.e. unsold.
- The first time a date is seen is the baseline, so day one doesn't count as cancellations.
- Revenue uses each club's indoor doubles price for 4 players (`CLUB_PRICES` in `report.py`, per person per hour as on the club's price page, Oct 2026). Most clubs: peak EUR 8, off-peak EUR 5 pp; Brugge EUR 7 peak; the Dutch clubs EUR 8.50 peak. Verrebroek, Roeselare, Terneuzen and Knokke had no indoor price online and are marked "aanname" (assumed). Peak hours are Meise's for every club (weekdays 17-23, weekends 10-18), see `PEAK`.
- Rebook rate = rebooked / (rebooked + stayed empty). Cancellations whose start time hasn't passed yet are "still open" and reported separately, and the rate is shown only once 2 h of cancellations are resolved, so a club with one quick rebook doesn't show 100%.
- A cancellation that gets rebooked within 10 minutes is invisible, so the real cancellation volume is somewhat higher than reported.

To add another Arenal club, add it to `CLUBS` in `tracker.py` (Antwerpen 14, Bree 11, Brugge 2, Hoogerheide 7, Kerkrade 8, Knokke 15, Lommel 9; the others are listed by `https://api.arenal.be/api/clubs`).
