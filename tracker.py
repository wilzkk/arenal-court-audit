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
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------- config
# Clubs to track. TRACK_ALL_CLUBS reads the full list from Arenal once a day
# (every club offering padel); CLUBS is the fallback if that list can't be fetched.
TRACK_ALL_CLUBS = True
CLUBS = {5: "Meise", 3: "Grimbergen"}
CLUBS_URL = "https://api.arenal.be/api/clubs"
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
# (all Arenal outdoor courts as of 10 Oct 2026, e.g. Meise 75-77, Grimbergen 24).
FALLBACK_EXCLUDED_COURTS = {
    12, 13, 14, 15, 24, 25, 26, 27, 28, 36, 37, 50, 51, 52, 53, 75, 76, 77,
    103, 104, 105, 106, 111, 112, 113, 114, 115, 116, 130, 131, 132, 133, 134,
    135, 136, 163, 164, 165, 168, 169, 172, 173,
}
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
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS courts (
    court_id INTEGER PRIMARY KEY, name TEXT, environment TEXT, fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type, club_id);
"""


# ---------------------------------------------------------------- fetching
def _get_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_day(date_str):
    """
    Return {"data": [...], "complete": True} for one date.

    The API pages its answer (5 clubs per page, cursor-based) and leaves out clubs
    that have no free slots left on that date. Its own "next" link drops the date
    and sport parameters (and then fails with HTTP 422), so the next page is
    requested by adding the cursor to the original URL. Only when every page was
    read is the answer "complete": then a club that is absent really has no free
    court time. If any page fails, an exception is raised and the date is skipped.
    """
    ids = ",".join(str(c) for c in sorted(CLUBS))
    base = API_URL.format(date=date_str, ids=ids, sport=SPORT)
    url, data, seen = base, [], set()
    for _page in range(50):
        page = _get_json(url)
        for club in page.get("data", []):
            if club.get("id") in CLUBS and club["id"] not in seen:
                data.append(club)
                seen.add(club["id"])
        cursor = (page.get("meta") or {}).get("next_cursor")
        if not cursor:
            return {"data": data, "complete": True}
        url = base + "&cursor=" + urllib.parse.quote(cursor)
        time.sleep(REQUEST_PAUSE_SEC)
    raise ValueError("too many pages")


def fetch_clubs():
    """Return {club_id: name} for every Arenal club that offers the tracked sport."""
    req = urllib.request.Request(CLUBS_URL, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8")).get("data", [])
    return {c["id"]: c["name"] for c in data if SPORT in (c.get("sports") or [])}


def refresh_clubs(conn, now, fetch_clubs_fn=fetch_clubs):
    """Update the global CLUBS from Arenal's club list, refreshed once a day, cached in the db."""
    global CLUBS
    cur = conn.cursor()
    if TRACK_ALL_CLUBS:
        last = cur.execute("SELECT value FROM meta WHERE key='clubs_fetched_at'").fetchone()
        stale = last is None or now - datetime.fromisoformat(last[0]) > COURTS_REFRESH
        if stale:
            try:
                clubs = fetch_clubs_fn()
                if clubs:
                    cur.executemany("INSERT OR REPLACE INTO clubs VALUES (?, ?)", clubs.items())
                    cur.execute("INSERT OR REPLACE INTO meta VALUES ('clubs_fetched_at', ?)",
                                (now.isoformat(),))
                    cur.execute("INSERT OR REPLACE INTO meta VALUES ('club_ids', ?)",
                                (",".join(str(i) for i in sorted(clubs)),))
                    conn.commit()
            except (urllib.error.URLError, ValueError, KeyError, TimeoutError) as e:
                print(f"  warning: could not refresh club list ({e})", file=sys.stderr)
        ids = cur.execute("SELECT value FROM meta WHERE key='club_ids'").fetchone()
        if ids:
            wanted = {int(i) for i in ids[0].split(",") if i}
            names = dict(cur.execute("SELECT club_id, name FROM clubs"))
            CLUBS = {i: names.get(i, str(i)) for i in sorted(wanted)}
    for cid, name in CLUBS.items():
        cur.execute("INSERT OR REPLACE INTO clubs VALUES (?, ?)", (cid, name))
    return CLUBS


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
def run_once(conn, now=None, fetch=fetch_day, fetch_courts_fn=fetch_courts,
             fetch_clubs_fn=fetch_clubs):
    now = now or datetime.now(TZ)
    now_iso = now.isoformat()
    cur = conn.cursor()

    refresh_clubs(conn, now, fetch_clubs_fn)

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
    current, fetched, errors = set(), {}, []   # fetched: date -> club ids actually received
    for i, d in enumerate(dates):
        try:
            payload = fetch(d)
            current |= blocks_from_payload(payload, skip)
            if payload.get("complete"):
                # Full answer: clubs left out have no free court time that day.
                fetched[d] = set(CLUBS)
            else:
                # Partial answer: only clubs actually returned can be compared.
                fetched[d] = {c.get("id") for c in payload.get("data", []) if c.get("id") in CLUBS}
        except (urllib.error.URLError, ValueError, KeyError, TimeoutError) as e:
            errors.append(f"{d}: {e}")
        if i < len(dates) - 1 and fetch is fetch_day:
            time.sleep(REQUEST_PAUSE_SEC)

    # Only consider future blocks (the API may still list a slot that started a minute ago).
    current = {b for b in current if datetime.fromisoformat(b[2]) > now}

    # 3. Diff per date, only for clubs the API actually returned for that date.
    #    A club missing from the answer is "not fetched", never "fully booked".
    fetched_dates = [d for d in dates if d in fetched]
    for d in fetched_dates:
        for club_id in sorted(fetched[d]):
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
