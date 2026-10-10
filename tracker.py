#!/usr/bin/env python3
"""
Arenal empty-court audit: tracker.

Run every 10 minutes (cron / Task Scheduler). Each run:
  1. Fetches public court availability for the next N days for the configured clubs.
  2. Turns it into free 30-minute blocks per court.
  3. Compares with the previous run and logs events in SQLite:
       opened         a block that was booked is free again (cancellation)
       booked         a free block got booked
       expired_empty  a block reached its start time while still free (lost revenue)

Outdoor courts are excluded (see EXCLUDE_ENVIRONMENTS).

Only uses the Python standard library (Python 3.9+).
"""

import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------- config
CLUBS = {5: "Meise", 3: "Grimbergen"}
SPORT = "padel"
DAYS_AHEAD = 14                     # how many days ahead to track (incl. today)
REQUEST_PAUSE_SEC = 1.5             # be polite to their API
DB_PATH = Path(__file__).with_name("arenal_audit.db")
API_URL = "https://api.arenal.be/api/bookable-clubs?date={date}&ids={ids}&sport={sport}"
COURTS_URL = "https://api.arenal.be/api/courts"
# Court environments Arenal uses. The API answers in the caller's language:
# Dutch "Binnen" / "Overdekt" / "Buiten", English "Indoor" / "Covered" / "Outdoor".
EXCLUDE_ENVIRONMENTS = {"buiten", "outdoor", "extérieur", "exterieur"}
# Used only if the court list can't be fetched and nothing is cached yet
# (outdoor courts as of Oct 2026: Meise 75, 76, 77; Grimbergen 24).
FALLBACK_EXCLUDED_COURTS = {24, 75, 76, 77}
COURTS_REFRESH = timedelta(hours=24)
TZ = ZoneInfo("Europe/Brussels")
BLOCK = timedelta(minutes=30)
HEADERS = {
    "Accept": "application/json",
    "Origin": "https://app.arenal.be",
    "Referer": "https://app.arenal.be/",
    "User-Agent": "Mozilla/5.0 (court-availability-audit)",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS free_blocks (
    club_id INTEGER, court_id INTEGER, block_start TEXT,
    PRIMARY KEY (club_id, court_id, block_start)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at TEXT, club_id INTEGER, court_id INTEGER,
    block_start TEXT, type TEXT
);
CREATE TABLE IF NOT EXISTS seen_dates (
    club_id INTEGER, date TEXT, PRIMARY KEY (club_id, date)
);
CREATE TABLE IF NOT EXISTS runs (
    run_at TEXT, ok INTEGER, dates_fetched INTEGER, free_blocks INTEGER, note TEXT
);
CREATE TABLE IF NOT EXISTS clubs (club_id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE IF NOT EXISTS courts (
    court_id INTEGER PRIMARY KEY, name TEXT, environment TEXT, fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type, club_id);
"""


# ---------------------------------------------------------------- fetching
def fetch_day(date_str):
    """Return the parsed JSON for one date, all configured clubs in one call."""
    ids = ",".join(str(c) for c in CLUBS)
    url = API_URL.format(date=date_str, ids=ids, sport=SPORT)
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_courts():
    """Return all Arenal courts (the endpoint is cursor-paginated, 15 per page)."""
    url, courts = COURTS_URL, []
    for _ in range(100):
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=20) as resp:
            page = json.loads(resp.read().decode("utf-8"))
        courts.extend(page.get("data", []))
        url = (page.get("links") or {}).get("next")
        if not url:
            break
    return courts


def excluded_courts(conn, now, fetch_courts_fn=fetch_courts):
    """Court ids to skip, refreshed from the API once a day and cached in the db."""
    cur = conn.cursor()
    last = cur.execute("SELECT MAX(fetched_at) FROM courts").fetchone()[0]
    stale = last is None or now - datetime.fromisoformat(last) > COURTS_REFRESH
    if stale:
        try:
            courts = fetch_courts_fn()
            if courts:
                cur.execute("DELETE FROM courts")
                cur.executemany(
                    "INSERT INTO courts VALUES (?, ?, ?, ?)",
                    [(c["id"], c.get("name"), (c.get("environment_type") or {}).get("name"),
                      now.isoformat()) for c in courts],
                )
                conn.commit()
        except (urllib.error.URLError, ValueError, KeyError, TimeoutError) as e:
            print(f"  warning: could not refresh court list ({e})", file=sys.stderr)
    rows = cur.execute("SELECT court_id, environment FROM courts").fetchall()
    if not rows:
        return set(FALLBACK_EXCLUDED_COURTS)
    return {cid for cid, env in rows if (env or "").strip().lower() in EXCLUDE_ENVIRONMENTS}


def blocks_from_payload(payload, skip_courts=frozenset()):
    """
    Turn the API payload into a set of (club_id, court_id, block_start_iso).
    Each timeslot is a bookable slot (60/90/120 min) on one court, so the union
    of all timeslots per court gives the sellable free time on that court.
    Courts in skip_courts (outdoor) are ignored.
    """
    out = set()
    for club in payload.get("data", []):
        club_id = club.get("id")
        if club_id not in CLUBS:
            continue
        for ts in club.get("timeslots", []):
            if ts["court_id"] in skip_courts:
                continue
            start = datetime.fromisoformat(ts["starts_at"]).astimezone(TZ)
            end = datetime.fromisoformat(ts["ends_at"]).astimezone(TZ)
            t = start
            while t < end:
                out.add((club_id, ts["court_id"], t.isoformat()))
                t += BLOCK
    return out


# ---------------------------------------------------------------- core logic
def run_once(conn, now=None, fetch=fetch_day, fetch_courts_fn=fetch_courts):
    now = now or datetime.now(TZ)
    now_iso = now.isoformat()
    cur = conn.cursor()

    for cid, name in CLUBS.items():
        cur.execute("INSERT OR REPLACE INTO clubs VALUES (?, ?)", (cid, name))

    # 0. Skip outdoor courts, and drop anything already recorded for them
    #    (e.g. from runs before this filter existed).
    skip = excluded_courts(conn, now, fetch_courts_fn)
    if skip:
        marks = ",".join("?" * len(skip))
        cur.execute(f"DELETE FROM free_blocks WHERE court_id IN ({marks})", tuple(skip))
        cur.execute(f"DELETE FROM events WHERE court_id IN ({marks})", tuple(skip))

    # 1. Blocks whose start time has passed while still free = lost revenue.
    expired = cur.execute(
        "SELECT club_id, court_id, block_start FROM free_blocks"
    ).fetchall()
    for club_id, court_id, bs in expired:
        if datetime.fromisoformat(bs) <= now:
            cur.execute(
                "INSERT INTO events (observed_at, club_id, court_id, block_start, type) "
                "VALUES (?, ?, ?, ?, 'expired_empty')",
                (now_iso, club_id, court_id, bs),
            )
            cur.execute(
                "DELETE FROM free_blocks WHERE club_id=? AND court_id=? AND block_start=?",
                (club_id, court_id, bs),
            )

    # 2. Fetch current availability, date by date.
    dates = [(now + timedelta(days=d)).date().isoformat() for d in range(DAYS_AHEAD)]
    current, fetched_dates, errors = set(), [], []
    for i, d in enumerate(dates):
        try:
            current |= blocks_from_payload(fetch(d), skip)
            fetched_dates.append(d)
        except (urllib.error.URLError, ValueError, KeyError, TimeoutError) as e:
            errors.append(f"{d}: {e}")
        if i < len(dates) - 1 and fetch is fetch_day:
            time.sleep(REQUEST_PAUSE_SEC)

    # Only consider future blocks (the API may still list a slot that started a minute ago).
    current = {b for b in current if datetime.fromisoformat(b[2]) > now}

    # 3. Diff per successfully fetched date.
    for d in fetched_dates:
        for club_id in CLUBS:
            new_date = cur.execute(
                "SELECT 1 FROM seen_dates WHERE club_id=? AND date=?", (club_id, d)
            ).fetchone() is None

            prev = {
                (club_id, r[0], r[1])
                for r in cur.execute(
                    "SELECT court_id, block_start FROM free_blocks "
                    "WHERE club_id=? AND substr(block_start,1,10)=?",
                    (club_id, d),
                )
            }
            now_free = {b for b in current if b[0] == club_id and b[2][:10] == d}

            if not new_date:  # first sight of a date is the baseline, no events
                for b in now_free - prev:
                    cur.execute(
                        "INSERT INTO events (observed_at, club_id, court_id, block_start, type) "
                        "VALUES (?, ?, ?, ?, 'opened')", (now_iso, *b))
                for b in prev - now_free:
                    cur.execute(
                        "INSERT INTO events (observed_at, club_id, court_id, block_start, type) "
                        "VALUES (?, ?, ?, ?, 'booked')", (now_iso, *b))
            else:
                cur.execute("INSERT INTO seen_dates VALUES (?, ?)", (club_id, d))

            cur.execute(
                "DELETE FROM free_blocks WHERE club_id=? AND substr(block_start,1,10)=?",
                (club_id, d),
            )
            cur.executemany("INSERT INTO free_blocks VALUES (?, ?, ?)", list(now_free))

    total_free = cur.execute("SELECT COUNT(*) FROM free_blocks").fetchone()[0]
    ok = 1 if not errors else 0
    cur.execute(
        "INSERT INTO runs VALUES (?, ?, ?, ?, ?)",
        (now_iso, ok, len(fetched_dates), total_free, "; ".join(errors)[:1000]),
    )
    conn.commit()
    return {"fetched": len(fetched_dates), "errors": errors, "free_blocks": total_free}


def connect(path=DB_PATH):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def audit_stopped():
    stop = os.environ.get("AUDIT_STOP")  # e.g. 2026-10-24T12:00
    return bool(stop) and datetime.now(TZ) >= datetime.fromisoformat(stop).replace(tzinfo=TZ)


def main():
    if "--stopped" in sys.argv:          # exit 0 if the audit period is over
        sys.exit(0 if audit_stopped() else 1)
    if audit_stopped():
        print(f"audit period ended ({os.environ.get('AUDIT_STOP')}), not checking")
        return
    conn = connect()
    res = run_once(conn)
    stamp = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    print(f"[{stamp}] fetched {res['fetched']}/{DAYS_AHEAD} days, "
          f"{res['free_blocks']} free blocks tracked")
    for e in res["errors"]:
        print(f"  error {e}", file=sys.stderr)
    if res["fetched"] == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
