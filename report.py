#!/usr/bin/env python3
"""
Arenal empty-court audit: report.

Reads arenal_audit.db (filled by tracker.py) and produces:
  - a summary in the terminal
  - report.html  (the numbers for your pitch)
  - events.csv   (raw events, for your own analysis)

Prices and peak hours default to Arenal Meise's published rates
(indoor doubles, 4 players). Adjust PRICES / PEAK below if a club differs.
"""

import csv
import html
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from tracker import DB_PATH, TZ, connect

OUT_DIR = Path(__file__).parent

# Revenue per court per 30-minute block (4 players).
# Meise peak: EUR 8 pp/hour -> EUR 32/hour -> EUR 16 per 30 min.
# Meise off-peak: EUR 5 pp/hour -> EUR 20/hour -> EUR 10 per 30 min.
PRICES = {"default": {"peak": 16.0, "offpeak": 10.0}}

# Peak hours (local time), from Meise's price page.
PEAK = {
    "weekday": (17, 23),   # Mon-Fri 17:00-23:00
    "weekend": (10, 18),   # Sat-Sun 10:00-18:00
}

REFUND_CUTOFF_H = 8      # Arenal refunds cancellations made > 8 h before start
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def is_peak(dt):
    lo, hi = PEAK["weekend"] if dt.weekday() >= 5 else PEAK["weekday"]
    return lo <= dt.hour < hi


def price(club_id, dt):
    p = PRICES.get(club_id, PRICES["default"])
    return p["peak"] if is_peak(dt) else p["offpeak"]


def eur(x):
    return f"EUR {x:,.0f}".replace(",", ".")


def hours(blocks):
    return blocks / 2


def load(conn):
    clubs = dict(conn.execute("SELECT club_id, name FROM clubs"))
    events = [
        dict(id=r[0], observed_at=datetime.fromisoformat(r[1]), club_id=r[2],
             court_id=r[3], block_start=datetime.fromisoformat(r[4]), type=r[5])
        for r in conn.execute(
            "SELECT id, observed_at, club_id, court_id, block_start, type "
            "FROM events ORDER BY observed_at, id")
    ]
    runs = [
        (datetime.fromisoformat(r[0]), r[1])
        for r in conn.execute("SELECT run_at, ok FROM runs ORDER BY run_at")
    ]
    courts = defaultdict(set)
    for e in events:
        courts[e["club_id"]].add(e["court_id"])
    for club_id, court_id in conn.execute("SELECT DISTINCT club_id, court_id FROM free_blocks"):
        courts[club_id].add(court_id)
    return clubs, events, runs, courts


def analyse(clubs, events, courts):
    """Per club metrics."""
    by_key = defaultdict(list)          # (club, court, block) -> events in order
    for e in events:
        by_key[(e["club_id"], e["court_id"], e["block_start"])].append(e)

    res = {}
    for club_id, name in clubs.items():
        m = dict(
            name=name, courts=len(courts.get(club_id, ())),
            opened=0, opened_peak=0,
            refilled=0, refilled_peak=0, refill_minutes=[],
            cancel_lost=0, cancel_lost_peak=0, cancel_lost_eur=0.0,
            late_refills=0, late_refills_eur=0.0,
            empty=0, empty_peak=0, empty_eur=0.0, empty_peak_eur=0.0,
            lead=defaultdict(int),
            empty_by_day=defaultdict(lambda: [0, 0]),    # weekday -> [peak, offpeak] blocks
            empty_peak_slots=defaultdict(int),           # (weekday, hour) -> blocks
            pending=0,
        )
        for (cid, _court, block), evs in by_key.items():
            if cid != club_id:
                continue
            peak = is_peak(block)
            for i, e in enumerate(evs):
                if e["type"] == "expired_empty":
                    m["empty"] += 1
                    m["empty_eur"] += price(club_id, block)
                    m["empty_by_day"][block.weekday()][0 if peak else 1] += 1
                    if peak:
                        m["empty_peak"] += 1
                        m["empty_peak_eur"] += price(club_id, block)
                        m["empty_peak_slots"][(block.weekday(), block.hour)] += 1
                if e["type"] != "opened":
                    continue

                m["opened"] += 1
                m["opened_peak"] += peak
                lead_h = (block - e["observed_at"]).total_seconds() / 3600
                bucket = ("> 24 h" if lead_h > 24 else
                          "8 - 24 h" if lead_h > REFUND_CUTOFF_H else
                          "< 8 h (not refunded)")
                m["lead"][bucket] += 1

                nxt = evs[i + 1] if i + 1 < len(evs) else None
                if nxt is None:
                    m["pending"] += 1
                elif nxt["type"] == "booked":
                    m["refilled"] += 1
                    m["refilled_peak"] += peak
                    m["refill_minutes"].append(
                        (nxt["observed_at"] - e["observed_at"]).total_seconds() / 60)
                    if lead_h <= REFUND_CUTOFF_H:
                        # Late cancellation is not refunded, so a refill is paid twice.
                        m["late_refills"] += 1
                        m["late_refills_eur"] += price(club_id, block)
                elif nxt["type"] == "expired_empty":
                    m["cancel_lost"] += 1
                    m["cancel_lost_peak"] += peak
                    m["cancel_lost_eur"] += price(club_id, block)
        res[club_id] = m
    return res


def coverage(runs):
    if not runs:
        return None
    gaps = [(a, b) for (a, _), (b, _) in zip(runs, runs[1:]) if b - a > timedelta(minutes=30)]
    first, last = runs[0][0], runs[-1][0]
    days = max((last - first).total_seconds() / 86400, 1e-9)
    return dict(first=first, last=last, n=len(runs), days=days,
                failed=sum(1 for _, ok in runs if not ok), gaps=gaps)


def summary_lines(m, cov):
    days = cov["days"] if cov else 1
    per_month = 30 / days if days >= 1 else None
    med = statistics.median(m["refill_minutes"]) if m["refill_minutes"] else None
    lines = [
        f"{m['name']} ({m['courts']} indoor courts seen)",
        f"  Court time reopened by cancellations: {hours(m['opened']):.1f} h "
        f"({hours(m['opened_peak']):.1f} h at peak)",
        f"    rebooked: {hours(m['refilled']):.1f} h"
        + (f", median {med:.0f} min to rebook" if med is not None else ""),
        f"    stayed empty: {hours(m['cancel_lost']):.1f} h = {eur(m['cancel_lost_eur'])} lost",
        f"  All empty court time at start: {hours(m['empty']):.1f} h = {eur(m['empty_eur'])}"
        f"  (peak {hours(m['empty_peak']):.1f} h = {eur(m['empty_peak_eur'])})",
    ]
    if per_month and days >= 3:
        lines.append(f"  Projected per month: cancellations lost {eur(m['cancel_lost_eur'] * per_month)}, "
                     f"empty peak time {eur(m['empty_peak_eur'] * per_month)}")
    return lines


def write_csv(clubs, events, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["observed_at", "club", "court_id", "block_start", "weekday",
                    "peak", "type"])
        for e in events:
            b = e["block_start"]
            w.writerow([e["observed_at"].strftime("%Y-%m-%d %H:%M"),
                        clubs.get(e["club_id"], e["club_id"]), e["court_id"],
                        b.strftime("%Y-%m-%d %H:%M"), WEEKDAYS[b.weekday()],
                        int(is_peak(b)), e["type"]])


def write_html(res, cov, path):
    esc = html.escape
    days = cov["days"] if cov else 0
    per_month = 30 / days if days >= 3 else None

    def club_card(m):
        med = statistics.median(m["refill_minutes"]) if m["refill_minutes"] else None
        refill_rate = (m["refilled"] / (m["refilled"] + m["cancel_lost"]) * 100
                       if (m["refilled"] + m["cancel_lost"]) else None)
        kpis = [
            ("Empty peak court time", f"{hours(m['empty_peak']):.0f} h", eur(m["empty_peak_eur"])),
            ("Cancelled and never rebooked", f"{hours(m['cancel_lost']):.0f} h", eur(m["cancel_lost_eur"])),
            ("Rebook rate after cancellation", f"{refill_rate:.0f}%" if refill_rate is not None else "n/a",
             f"median {med:.0f} min" if med is not None else ""),
            ("All empty court time", f"{hours(m['empty']):.0f} h", eur(m["empty_eur"])),
        ]
        kpi_html = "".join(
            f'<div class="kpi"><div class="label">{esc(l)}</div>'
            f'<div class="value">{esc(v)}</div><div class="sub">{esc(s)}</div></div>'
            for l, v, s in kpis)

        proj = ""
        if per_month:
            proj = (f'<p class="pitch">At this rate: about <b>{eur(m["empty_peak_eur"] * per_month)}</b> '
                    f'of peak court time and <b>{eur(m["cancel_lost_eur"] * per_month)}</b> '
                    f'of cancelled bookings go unsold each month.</p>')

        lead_rows = "".join(
            f"<tr><td>{esc(k)}</td><td>{hours(v):.1f} h</td></tr>"
            for k, v in sorted(m["lead"].items()))
        day_rows = "".join(
            f"<tr><td>{WEEKDAYS[d]}</td><td>{hours(v[0]):.1f} h</td><td>{hours(v[1]):.1f} h</td></tr>"
            for d, v in sorted(m["empty_by_day"].items()))
        top = sorted(m["empty_peak_slots"].items(), key=lambda kv: -kv[1])[:6]
        top_rows = "".join(
            f"<tr><td>{WEEKDAYS[d]} {h:02d}:00-{h + 1:02d}:00</td><td>{hours(v):.1f} h</td></tr>"
            for (d, h), v in top)
        late = (f"<p>Late cancellations (under {REFUND_CUTOFF_H} h, not refunded) that were still "
                f"rebooked: {hours(m['late_refills']):.1f} h, worth {eur(m['late_refills_eur'])} "
                f"in extra revenue (the club is paid twice).</p>") if m["late_refills"] else ""

        return f"""
<section class="card">
  <h2>Arenal {esc(m['name'])} <span class="muted">{m['courts']} indoor courts</span></h2>
  {proj}
  <div class="kpis">{kpi_html}</div>
  <div class="grid">
    <div><h3>When cancellations happen</h3>
      <table><tr><th>Before start</th><th>Court time</th></tr>{lead_rows or '<tr><td colspan=2>none yet</td></tr>'}</table>
      {late}</div>
    <div><h3>Empty court time by day</h3>
      <table><tr><th>Day</th><th>Peak</th><th>Off-peak</th></tr>{day_rows or '<tr><td colspan=3>none yet</td></tr>'}</table></div>
    <div><h3>Most often empty peak hours</h3>
      <table><tr><th>Slot</th><th>Empty</th></tr>{top_rows or '<tr><td colspan=2>none yet</td></tr>'}</table></div>
  </div>
</section>"""

    cov_txt = "No runs yet."
    if cov:
        cov_txt = (f"{cov['first']:%d %b %H:%M} to {cov['last']:%d %b %H:%M} "
                   f"({days:.1f} days, {cov['n']} checks, {cov['failed']} with errors, "
                   f"{len(cov['gaps'])} gaps over 30 min)")

    body = "".join(club_card(m) for m in res.values())
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Empty Court Audit</title>
<style>
:root {{ --bg:#f6f7f9; --card:#fff; --ink:#15181d; --muted:#6b7280; --line:#e5e7eb; --accent:#1d4ed8; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0f1115; --card:#181b21; --ink:#e8eaed; --muted:#9aa0a6; --line:#2a2f37; --accent:#7aa2ff; }} }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 system-ui, -apple-system, Segoe UI, sans-serif; }}
main {{ max-width:1000px; margin:0 auto; padding:32px 16px; }}
h1 {{ margin:0 0 4px; font-size:26px; }} h2 {{ margin:0 0 12px; font-size:20px; }} h3 {{ font-size:14px; margin:0 0 8px; color:var(--muted); font-weight:600; }}
.muted {{ color:var(--muted); font-weight:400; font-size:14px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:20px; margin:20px 0; }}
.kpis {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:12px; margin:12px 0 20px; }}
.kpi {{ border:1px solid var(--line); border-radius:10px; padding:12px; }}
.kpi .label {{ color:var(--muted); font-size:13px; }} .kpi .value {{ font-size:24px; font-weight:700; }} .kpi .sub {{ color:var(--accent); font-size:14px; font-weight:600; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:20px; }}
table {{ width:100%; border-collapse:collapse; font-size:14px; }} th, td {{ text-align:left; padding:6px 4px; border-bottom:1px solid var(--line); }} th {{ color:var(--muted); font-weight:600; }}
.pitch {{ font-size:16px; background:color-mix(in srgb, var(--accent) 10%, transparent); padding:10px 12px; border-radius:8px; }}
footer {{ color:var(--muted); font-size:13px; }}
</style></head><body><main>
<h1>Empty court audit</h1>
<div class="muted">Tracked: {esc(cov_txt)}</div>
{body}
<footer><p>Method: public availability checked every 10 minutes. "Cancelled" means court time that was booked and became bookable again.
"Empty" means court time still unbooked when it started. Indoor courts only; outdoor courts are excluded.
Values use Meise's published indoor doubles rates for 4 players (peak EUR 32/h, off-peak EUR 20/h).
Cancellations rebooked between two checks are not visible, so the rebook rate is a lower bound.</p></footer>
</main></body></html>"""
    path.write_text(page, encoding="utf-8")


def main():
    conn = connect(DB_PATH)
    clubs, events, runs, courts = load(conn)
    cov = coverage(runs)
    res = analyse(clubs, events, courts)
    if cov:
        print(f"Tracked {cov['days']:.1f} days, {cov['n']} checks "
              f"({cov['failed']} with errors, {len(cov['gaps'])} gaps over 30 min)\n")
    for m in res.values():
        print("\n".join(summary_lines(m, cov)) + "\n")
    write_csv(clubs, events, OUT_DIR / "events.csv")
    write_html(res, cov, OUT_DIR / "report.html")
    print(f"Wrote {OUT_DIR / 'report.html'} and {OUT_DIR / 'events.csv'}")


if __name__ == "__main__":
    main()
