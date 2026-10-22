#!/usr/bin/env python3
"""
Arenal empty-court audit: report.

Reads arenal_audit.db (filled by tracker.py) and produces:
  - a summary in the terminal
  - report.html   (all numbers, per club)
  - directie.html (Dutch one-page version for Arenal's management)
  - events.csv    (raw events, for your own analysis)

Prices are per club (CLUB_PRICES). Peak hours are Meise's for every club (PEAK).
"""

import csv
import html
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from tracker import BLOCK, DB_PATH, TZ, connect

OUT_DIR = Path(__file__).parent

# Indoor doubles, EUR per person per hour (peak, off-peak, source), 4 players per court.
# "website": read from the club's own price page (Oct 2026).
# "aanname": no indoor price found; assumed from comparable clubs.
CLUB_PRICES = {
    1: (8.0, 5.0, "aanname"),    # Verrebroek
    2: (7.0, 5.0, "website"),    # Brugge (page gives "from EUR 7")
    3: (8.0, 5.0, "website"),    # Grimbergen
    4: (8.0, 5.0, "website"),    # Mechelen
    5: (8.0, 5.0, "website"),    # Meise
    6: (8.0, 5.0, "website"),    # Waregem
    7: (8.5, 5.0, "website"),    # Hoogerheide
    8: (8.5, 5.0, "website"),    # Kerkrade
    9: (8.0, 5.0, "website"),    # Lommel
    11: (8.0, 5.0, "website"),   # Bree
    12: (8.0, 5.0, "aanname"),   # Roeselare
    13: (8.5, 5.0, "aanname"),   # Terneuzen (as the other Dutch clubs)
    14: (8.0, 5.0, "website"),   # Antwerpen
    15: (7.0, 5.0, "aanname"),   # Knokke (as Brugge)
}
DEFAULT_PRICE = (8.0, 5.0, "aanname")
PLAYERS = 4

# Peak hours (local time), from Meise's price page.
PEAK = {
    "weekday": (17, 23),   # Mon-Fri 17:00-23:00
    "weekend": (10, 18),   # Sat-Sun 10:00-18:00
}

REFUND_CUTOFF_H = 8      # Arenal refunds cancellations made > 8 h before start
MIN_RESOLVED_BLOCKS = 4  # show a rebook rate only once 2 h of cancellations are resolved
GAP_TOLERANCE = timedelta(minutes=30)   # court time not checked for longer than this is unverified
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def is_peak(dt):
    lo, hi = PEAK["weekend"] if dt.weekday() >= 5 else PEAK["weekday"]
    return lo <= dt.hour < hi


def price(club_id, dt):
    """Revenue of one 30-minute block on one court."""
    peak, offpeak, _src = CLUB_PRICES.get(club_id, DEFAULT_PRICE)
    return (peak if is_peak(dt) else offpeak) * PLAYERS / 2


def rebook_rate(m):
    """Share of resolved cancellations that got rebooked, None while too few are resolved.

    A cancellation is resolved once it is rebooked or its start time passed while
    still free. Cancellations still open are left out, so they are reported apart.
    """
    done = m["refilled"] + m["cancel_lost"]
    return m["refilled"] / done * 100 if done >= MIN_RESOLVED_BLOCKS else None


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


def is_blind(t, blind):
    return any(lo < t <= hi for lo, hi in blind)


def analyse(clubs, events, courts, blind=()):
    """Per club metrics.

    Court time that started inside a blind window (no successful check for over
    GAP_TOLERANCE) may have been booked unseen, so it is not counted as empty.
    """
    by_key = defaultdict(list)          # (club, court, block) -> events in order
    for e in events:
        by_key[(e["club_id"], e["court_id"], e["block_start"])].append(e)

    res = {}
    for club_id, name in clubs.items():
        m = dict(
            name=name, courts=len(courts.get(club_id, ())),
            opened=0, opened_peak=0,
            refilled=0, refilled_peak=0, refill_minutes=[],
            cancel_lost=0, cancel_lost_peak=0, cancel_lost_eur=0.0, cancel_lost_peak_eur=0.0,
            late_refills=0, late_refills_eur=0.0, late_refills_peak=0, late_refills_peak_eur=0.0,
            lead_peak=defaultdict(int),
            empty=0, empty_peak=0, empty_eur=0.0, empty_peak_eur=0.0,
            lead=defaultdict(int),
            empty_by_day=defaultdict(lambda: [0, 0]),    # weekday -> [peak, offpeak] blocks
            empty_peak_slots=defaultdict(int),           # (weekday, hour) -> blocks
            empty_slots=defaultdict(int),                # (weekday, hour) -> blocks, all hours
            pending=0, pending_peak=0,
        )
        for (cid, _court, block), evs in by_key.items():
            if cid != club_id:
                continue
            peak = is_peak(block)
            unverified = is_blind(block, blind)
            for i, e in enumerate(evs):
                if e["type"] == "expired_empty" and not unverified:
                    m["empty"] += 1
                    m["empty_eur"] += price(club_id, block)
                    m["empty_by_day"][block.weekday()][0 if peak else 1] += 1
                    m["empty_slots"][(block.weekday(), block.hour)] += 1
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
                if peak:
                    m["lead_peak"][bucket] += 1

                nxt = evs[i + 1] if i + 1 < len(evs) else None
                if nxt is None or (nxt["type"] == "expired_empty" and unverified):
                    m["pending"] += 1
                    m["pending_peak"] += peak
                elif nxt["type"] == "booked":
                    m["refilled"] += 1
                    m["refilled_peak"] += peak
                    m["refill_minutes"].append(
                        (nxt["observed_at"] - e["observed_at"]).total_seconds() / 60)
                    if lead_h <= REFUND_CUTOFF_H:
                        # Late cancellation is not refunded, so a refill is paid twice.
                        m["late_refills"] += 1
                        m["late_refills_eur"] += price(club_id, block)
                        if peak:
                            m["late_refills_peak"] += 1
                            m["late_refills_peak_eur"] += price(club_id, block)
                elif nxt["type"] == "expired_empty":
                    m["cancel_lost"] += 1
                    m["cancel_lost_peak"] += peak
                    m["cancel_lost_eur"] += price(club_id, block)
                    if peak:
                        m["cancel_lost_peak_eur"] += price(club_id, block)
        res[club_id] = m
    return res


def coverage(runs):
    if not runs:
        return None
    gaps = [(a, b) for (a, _), (b, _) in zip(runs, runs[1:]) if b - a > timedelta(minutes=30)]
    first, last = runs[0][0], runs[-1][0]
    days = max((last - first).total_seconds() / 86400, 1e-9)
    # Blind windows: from GAP_TOLERANCE after a successful check until the next one,
    # when checks were missing or failing (e.g. Arenal's API down).
    ok = [t for t, good in runs if good]
    blind = [(a + GAP_TOLERANCE, b) for a, b in zip(ok, ok[1:]) if b - a > GAP_TOLERANCE]
    if ok and last - ok[-1] > GAP_TOLERANCE:
        blind.append((ok[-1] + GAP_TOLERANCE, last))
    return dict(first=first, last=last, n=len(runs), days=days,
                failed=sum(1 for _, ok in runs if not ok), gaps=gaps, blind=blind)


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
        f"    still open: {hours(m['pending']):.1f} h",
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
        refill_rate = rebook_rate(m)
        resolved = m["refilled"] + m["cancel_lost"]
        rate_sub = f"of {hours(resolved):.1f} h resolved, {hours(m['pending']):.1f} h still open"
        kpis = [
            ("Empty peak court time", f"{hours(m['empty_peak']):.0f} h", eur(m["empty_peak_eur"])),
            ("Cancelled and never rebooked", f"{hours(m['cancel_lost']):.0f} h", eur(m["cancel_lost_eur"])),
            ("Rebook rate after cancellation",
             f"{refill_rate:.0f}%" if refill_rate is not None else "too early",
             rate_sub + (f", median {med:.0f} min" if med is not None else "")),
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

    def rebook(m):
        r = rebook_rate(m)
        return f"{r:.0f}%" if r is not None else "too early"

    ranked = sorted(res.values(), key=lambda m: -(m["empty_peak_eur"] + m["cancel_lost_eur"]))
    mult = per_month or 1
    unit = "per month (projected)" if per_month else "so far"
    overview_rows = "".join(
        f"<tr><td>{esc(m['name'])}</td><td>{m['courts']}</td>"
        f"<td>{eur(m['empty_peak_eur'] * mult)}</td><td>{eur(m['cancel_lost_eur'] * mult)}</td>"
        f"<td>{hours(m['opened']):.0f} h</td><td>{hours(m['pending']):.0f} h</td><td>{rebook(m)}</td></tr>"
        for m in ranked)
    tot_peak = sum(m["empty_peak_eur"] for m in ranked) * mult
    tot_cancel = sum(m["cancel_lost_eur"] for m in ranked) * mult
    overview = f"""
<section class="card">
  <h2>All clubs <span class="muted">ranked by lost revenue, {unit}</span></h2>
  <div class="scroll"><table>
    <tr><th>Club</th><th>Indoor courts</th><th>Empty peak time</th><th>Cancelled, never rebooked</th><th>Cancellations</th><th>Still open</th><th>Rebook rate</th></tr>
    {overview_rows}
    <tr><th>Total</th><th>{sum(m['courts'] for m in ranked)}</th><th>{eur(tot_peak)}</th><th>{eur(tot_cancel)}</th><th>{hours(sum(m['opened'] for m in ranked)):.0f} h</th><th>{hours(sum(m['pending'] for m in ranked)):.0f} h</th><th></th></tr>
  </table></div>
</section>""" if len(ranked) > 1 else ""

    body = overview + "".join(club_card(m) for m in ranked)
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
.scroll {{ overflow-x:auto; }}
.pitch {{ font-size:16px; background:color-mix(in srgb, var(--accent) 10%, transparent); padding:10px 12px; border-radius:8px; }}
footer {{ color:var(--muted); font-size:13px; }}
</style></head><body><main>
<h1>Empty court audit</h1>
<div class="muted">Tracked: {esc(cov_txt)}</div>
{body}
<footer><p>Method: public availability checked every 10 minutes. "Cancelled" means court time that was booked and became bookable again.
"Empty" means court time still unbooked when it started. Indoor courts only; outdoor courts are excluded.
Values use each club's indoor doubles rate for 4 players where its price page lists one, otherwise an assumed rate
(see directie.html); peak hours are Meise's for every club. The hours are exact.
The rebook rate counts only resolved cancellations (rebooked, or start time passed while free) and is shown once
{hours(MIN_RESOLVED_BLOCKS):.0f} h are resolved; "still open" cancellations can still be rebooked.
Cancellations rebooked between two checks are not visible, so the rebook rate is a lower bound.</p></footer>
</main></body></html>"""
    path.write_text(page, encoding="utf-8")


# ---------------------------------------------------------------- directie (Dutch, for management)
DAGEN = ["ma", "di", "wo", "do", "vr", "za", "zo"]
DAGEN_LANG = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]
HEAT_HOURS = range(min(lo for lo, _ in PEAK.values()), max(hi for _, hi in PEAK.values()))


def peak_view(m):
    """The club's metrics restricted to peak hours: off-peak empty time is expected, not a loss."""
    v = dict(m)
    v.update(
        empty=m["empty_peak"], empty_eur=m["empty_peak_eur"], empty_slots=m["empty_peak_slots"],
        opened=m["opened_peak"], refilled=m["refilled_peak"], pending=m["pending_peak"],
        cancel_lost=m["cancel_lost_peak"], cancel_lost_eur=m["cancel_lost_peak_eur"],
        lead=m["lead_peak"], late_refills=m["late_refills_peak"],
        late_refills_eur=m["late_refills_peak_eur"],
    )
    return v


def is_peak_slot(weekday, hour):
    return is_peak(datetime(2026, 1, 5 + weekday, hour))   # 5 Jan 2026 is a Monday


def eur_nl(x):
    return f"€ {x:,.0f}".replace(",", ".")


def num_nl(x, d=1):
    return f"{x:,.{d}f}".replace(",", "_").replace(".", ",").replace("_", ".")


def halfhour_counts(cov):
    """How many 30-minute blocks per (weekday, hour) started while tracking.

    Counted exactly like expired_empty blocks (start after the first check, at or
    before the last), so a partly passed hour weighs the same on both sides.
    Blocks in a blind window are left out here as they are from the empty counts;
    they are returned apart, per (weekday, hour), to report what was left out.
    """
    counts, blind = defaultdict(int), defaultdict(int)
    if not cov:
        return counts, blind
    t = cov["first"].replace(minute=0, second=0, microsecond=0)
    while t <= cov["first"]:
        t += BLOCK
    while t <= cov["last"]:
        (blind if is_blind(t, cov["blind"]) else counts)[(t.weekday(), t.hour)] += 1
        t += BLOCK
    return counts, blind


def write_directie(res, cov, path):
    esc = html.escape
    res = {cid: peak_view(m) for cid, m in res.items()}
    days = cov["days"] if cov else 0
    mult = 30 / days if days >= 3 else None
    clubs = sorted(res.items(), key=lambda kv: -kv[1]["empty_peak_eur"])
    ms = [m for _, m in clubs]
    counts, blind_counts = halfhour_counts(cov)
    peak_seen = sum(n for (d, h), n in counts.items() if is_peak_slot(d, h))
    blind_peak = sum(n for (d, h), n in blind_counts.items() if is_peak_slot(d, h))
    blind_peak_h = hours(blind_peak)
    if mult and peak_seen:
        mult *= (peak_seen + blind_peak) / peak_seen    # project from the peak time actually measured
    weekdays_seen = sorted({wd for wd, _h in counts})

    def amount(x):
        return eur_nl(x * mult) if mult else eur_nl(x)
    unit = "per maand (schatting)" if mult else "tot nu toe"

    tot = {k: sum(m[k] for m in ms) for k in (
        "empty", "empty_eur", "empty_peak", "empty_peak_eur", "opened", "refilled",
        "cancel_lost", "cancel_lost_eur", "pending", "courts", "late_refills", "late_refills_eur")}
    tot_rate = rebook_rate(tot)
    capacity = peak_seen * tot["courts"]   # blocks of peak court time measured
    empty_share = min(tot["empty_peak"] / capacity * 100, 100) if capacity else None

    # -- status line
    if not cov:
        status = '<p class="banner">Nog geen metingen.</p>'
    else:
        seen = ", ".join(DAGEN_LANG[d] for d in weekdays_seen)
        period = f"{cov['first']:%d/%m %H:%M} tot {cov['last']:%d/%m %H:%M}"
        if days < 7:
            status = (f'<p class="banner"><b>Voorlopige cijfers.</b> {num_nl(days)} dagen gemeten '
                      f"({esc(seen)}). "
                      + ("Maandbedragen zijn een doortrekking van deze periode; "
                         if mult else "Maandbedragen verschijnen vanaf 3 dagen meting; ")
                      + "betrouwbaar vanaf een volle week met alle weekdagen.</p>")
        else:
            status = ""
        period_txt = f"Gemeten van {period} ({num_nl(days)} dagen, {cov['n']} metingen)."
        if blind_peak_h:
            period_txt += (f" {num_nl(blind_peak_h)} uur piek zonder meting (storing) "
                           f"telt niet mee.")

    # -- headline
    if mult:
        lead = (f"Elke maand staat er naar schatting voor <b>{amount(tot['empty_peak_eur'])}</b> "
                f"aan indoorpistes leeg tijdens piekuren, verspreid over {len(ms)} Arenal-clubs.")
    else:
        lead = (f"In {num_nl(days)} dag{'en' if days >= 1.5 else ''} stond er "
                f"<b>{num_nl(hours(tot['empty_peak']), 0)} uur</b> indoorpiste leeg tijdens piekuren, "
                f"samen <b>{eur_nl(tot['empty_peak_eur'])}</b> aan gemiste verhuur in {len(ms)} clubs.")

    tiles = [
        ("Lege piekuren", amount(tot["empty_peak_eur"]),
         f"{num_nl(hours(tot['empty_peak'] * (mult or 1)), 0)} uur piste {unit}"),
        ("Piekcapaciteit leeg", f"{empty_share:.0f}%" if empty_share is not None else "-",
         f"van {num_nl(hours(capacity), 0)} uur piekcapaciteit gemeten"),
        ("Geannuleerde piekuren niet herboekt", amount(tot["cancel_lost_eur"]),
         f"{num_nl(hours(tot['cancel_lost'] * (mult or 1)), 0)} uur, terugbetaald aan de speler"),
        ("Herboekt na annulering in piekuren", f"{tot_rate:.0f}%" if tot_rate is not None else "nog te vroeg",
         f"van {num_nl(hours(tot['refilled'] + tot['cancel_lost']))} u afgehandeld, "
         f"{num_nl(hours(tot['pending']))} u nog open"),
    ]
    tiles_html = "".join(
        f'<div class="tile"><div class="t-label">{esc(l)}</div><div class="t-value">{esc(v)}</div>'
        f'<div class="t-sub">{esc(s)}</div></div>' for l, v, s in tiles)

    # -- ranking: empty peak revenue per club
    top = max((m["empty_peak_eur"] for m in ms), default=0) or 1
    rank_rows = "".join(
        f'<div class="row" data-tip="{esc(m["name"])}: {num_nl(hours(m["empty_peak"]))} u lege piekuren, '
        f'{eur_nl(m["empty_peak_eur"])} {"in de meetperiode" if mult else "tot nu toe"}, '
        f'{m["courts"]} indoorpistes">'
        f'<div class="name">{esc(m["name"])}</div>'
        f'<div class="track"><div class="bar" style="width:{m["empty_peak_eur"] / top * 100:.1f}%"></div></div>'
        f'<div class="val">{amount(m["empty_peak_eur"])}'
        f'<span class="muted"> · {amount(m["empty_peak_eur"] / m["courts"]) if m["courts"] else "-"}/piste</span></div>'
        f'</div>' for m in ms)

    # -- heatmap: average share of courts empty per weekday x hour
    def heatmap(sel, total_courts, slots):
        head = '<div class="hm-corner"></div>' + "".join(
            f'<div class="hm-h">{h}</div>' for h in HEAT_HOURS)
        cells = []
        for d in range(7):
            cells.append(f'<div class="hm-d">{DAGEN[d]}</div>')
            for h in HEAT_HOURS:
                n = counts.get((d, h), 0)
                if not is_peak_slot(d, h):
                    cells.append('<div class="hm-c off"></div>')
                    continue
                cls = "hm-c"
                if not n or not total_courts:
                    cells.append(f'<div class="{cls} na" data-tip="{DAGEN_LANG[d]} {h:02d}:00: niet gemeten"></div>')
                    continue
                avg = slots.get((d, h), 0) / n     # empty blocks per measured block = courts empty
                share = min(avg / total_courts, 1)
                label = f"{share * 100:.0f}" if share >= 0.25 else ""
                tip = (f"{DAGEN_LANG[d]} {h:02d}:00-{h + 1:02d}:00: "
                       f"gemiddeld {num_nl(avg)} van {total_courts} pistes leeg ({share * 100:.0f}%)")
                ink = " dark" if share >= 0.55 else ""
                cells.append(f'<div class="{cls}{ink}" style="--a:{share:.3f}" data-tip="{esc(tip)}">{label}</div>')
        return (f'<div class="hm" data-club="{sel}"{"" if sel == "all" else " hidden"}>'
                f'{head}{"".join(cells)}</div>')

    all_slots = defaultdict(int)
    for m in ms:
        for k, v in m["empty_slots"].items():
            all_slots[k] += v
    heatmaps = heatmap("all", tot["courts"], all_slots) + "".join(
        heatmap(str(cid), m["courts"], m["empty_slots"]) for cid, m in clubs)
    options = '<option value="all">Alle clubs</option>' + "".join(
        f'<option value="{cid}">{esc(m["name"])}</option>'
        for cid, m in sorted(clubs, key=lambda kv: kv[1]["name"]))

    # -- cancellations: what happened to cancelled court time
    canc = [m for m in ms if m["opened"]]
    canc.sort(key=lambda m: -m["opened"])
    ctop = tot["opened"] or 1    # the total row is the longest bar

    def stack(name, m, strong=False):
        parts = [("rebooked", m["refilled"], "herboekt"), ("lost", m["cancel_lost"], "leeg gebleven"),
                 ("open", m["pending"], "nog open")]
        segs = "".join(
            f'<div class="seg {c}" style="flex:{v}" data-tip="{esc(name)}: {num_nl(hours(v))} u {t}"></div>'
            for c, v, t in parts if v)
        width = m["opened"] / ctop * 100
        return (f'<div class="row{" total" if strong else ""}"><div class="name">{esc(name)}</div>'
                f'<div class="track"><div class="stack" style="width:{width:.1f}%">{segs}</div></div>'
                f'<div class="val">{num_nl(hours(m["opened"]))} u</div></div>')

    canc_rows = "".join(stack(m["name"], m) for m in canc)
    lead_tot = defaultdict(int)
    for m in ms:
        for k, v in m["lead"].items():
            lead_tot[k] += v
    n_lead = sum(lead_tot.values())
    canc_notes = []
    if n_lead:
        early = (lead_tot["> 24 h"] + lead_tot["8 - 24 h"]) / n_lead * 100
        canc_notes.append(
            f"<b>{early:.0f}%</b> van de geannuleerde piekuren wordt meer dan {REFUND_CUTOFF_H} uur vooraf "
            f"geannuleerd. Die speler krijgt zijn geld terug, dus elk uur dat daarna niet opnieuw verkocht "
            f"wordt, is echt verlies. {lead_tot['> 24 h'] / n_lead * 100:.0f}% komt zelfs meer dan een dag "
            f"vooraf vrij: genoeg tijd om de piste opnieuw aan te bieden.")
    if tot["late_refills"]:
        canc_notes.append(
            f"Late annuleringen (minder dan {REFUND_CUTOFF_H} uur vooraf, niet terugbetaald) die toch "
            f"herboekt werden: {num_nl(hours(tot['late_refills']))} uur, {eur_nl(tot['late_refills_eur'])} "
            f"extra omzet omdat de piste twee keer betaald werd.")
    if tot_rate is None and tot["opened"]:
        canc_notes.append("Een herboekpercentage tonen we pas als minstens "
                          f"{hours(MIN_RESOLVED_BLOCKS):.0f} uur aan annuleringen afgehandeld is.")
    canc_html = (
        f'<div class="legend"><span><i class="sw rebooked"></i>herboekt</span>'
        f'<span><i class="sw lost"></i>leeg gebleven</span><span><i class="sw open"></i>nog open</span></div>'
        f'<div class="bars">{canc_rows}{stack("Totaal", tot, True)}</div>'
        + "".join(f"<p>{n}</p>" for n in canc_notes)
    ) if canc else "<p>Nog geen annuleringen gemeten.</p>"

    # -- scenarios
    scen_rows = "".join(
        f"<tr><td>{p}% van de lege piekuren gevuld</td><td class='num'>{amount(tot['empty_peak_eur'] * p / 100)}</td></tr>"
        for p in (10, 25, 50))

    # -- prices
    price_rows = "".join(
        f"<tr><td>{esc(m['name'])}</td><td class='num'>{num_nl(CLUB_PRICES.get(cid, DEFAULT_PRICE)[0] * PLAYERS, 0)}</td>"
        f"<td>{CLUB_PRICES.get(cid, DEFAULT_PRICE)[2]}</td></tr>"
        for cid, m in sorted(clubs, key=lambda kv: kv[1]["name"]))
    wk, we = PEAK["weekday"], PEAK["weekend"]

    page = f"""<!doctype html>
<html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lege pistes Arenal</title>
<style>
:root {{
  color-scheme: light;
  --bg:#f6f6f4; --card:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --muted:#76746f; --line:#e4e3df;
  --track:#efeeea; --blue:#2a78d6; --orange:#eb6834; --gray:#b9b7b0; --banner:#fff4e0; --banner-ink:#6b4a00;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    color-scheme: dark;
    --bg:#121211; --card:#1a1a19; --ink:#f0efec; --ink2:#c3c2b7; --muted:#9b9a93; --line:#2e2d2a;
    --track:#262624; --blue:#3987e5; --orange:#d95926; --gray:#5f5e59; --banner:#2e2410; --banner-ink:#f2d39a;
  }}
}}
:root[data-theme="dark"] {{
  color-scheme: dark;
  --bg:#121211; --card:#1a1a19; --ink:#f0efec; --ink2:#c3c2b7; --muted:#9b9a93; --line:#2e2d2a;
  --track:#262624; --blue:#3987e5; --orange:#d95926; --gray:#5f5e59; --banner:#2e2410; --banner-ink:#f2d39a;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; }}
main {{ max-width:980px; margin:0 auto; padding:40px 16px 56px; }}
h1 {{ font-size:30px; line-height:1.2; margin:0 0 6px; letter-spacing:-.01em; }}
h2 {{ font-size:19px; margin:0 0 4px; }}
.sub {{ color:var(--ink2); margin:0 0 14px; }}
.muted {{ color:var(--muted); }}
.lead {{ font-size:21px; line-height:1.4; margin:18px 0 22px; max-width:46em; }}
.banner {{ background:var(--banner); color:var(--banner-ink); border-radius:10px; padding:10px 14px; font-size:14px; }}
section {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:22px; margin:18px 0; }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(min(200px, 46%), 1fr)); gap:12px; }}
.tile {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 16px; }}
.t-label {{ color:var(--ink2); font-size:13px; }}
.t-value {{ font-size:28px; font-weight:700; font-variant-numeric:tabular-nums; margin:2px 0; }}
.t-sub {{ color:var(--muted); font-size:13px; }}
.bars {{ display:grid; gap:6px; margin-top:12px; }}
.row {{ display:grid; grid-template-columns:110px 1fr 170px; gap:10px; align-items:center; font-size:14px; }}
.row .val {{ font-variant-numeric:tabular-nums; white-space:nowrap; }}
.row.total {{ font-weight:700; border-top:1px solid var(--line); padding-top:8px; margin-top:4px; }}
.track {{ height:14px; }}
.bar {{ height:100%; background:var(--blue); border-radius:0 4px 4px 0; min-width:2px; }}
.stack {{ display:flex; gap:2px; height:100%; min-width:4px; }}
.seg {{ height:100%; }} .seg:last-child {{ border-radius:0 4px 4px 0; }}
.seg.rebooked, .sw.rebooked {{ background:var(--blue); }}
.seg.lost, .sw.lost {{ background:var(--orange); }}
.seg.open, .sw.open {{ background:var(--gray); }}
.legend {{ display:flex; flex-wrap:wrap; gap:16px; font-size:13px; color:var(--ink2); }}
.sw {{ display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:6px; vertical-align:-1px; }}
.controls {{ display:flex; flex-wrap:wrap; gap:12px; align-items:center; justify-content:space-between; margin:10px 0 12px; }}
select {{ font:inherit; color:var(--ink); background:var(--card); border:1px solid var(--line); border-radius:8px; padding:6px 10px; }}
.scale {{ display:flex; align-items:center; gap:8px; font-size:13px; color:var(--ink2); }}
.scale .nm {{ display:inline-block; width:14px; height:10px; border-radius:2px; margin-left:8px;
  background:repeating-linear-gradient(45deg, transparent 0 3px, var(--line) 3px 4px); box-shadow:inset 0 0 0 1px var(--line); }}
.scale .grad {{ width:120px; height:10px; border-radius:2px; background:linear-gradient(90deg, var(--track), var(--blue)); }}
.scroll {{ overflow-x:auto; }}
.hm {{ display:grid; grid-template-columns:30px repeat({len(HEAT_HOURS)}, minmax(26px, 1fr)); gap:2px; min-width:520px; }}
.hm[hidden] {{ display:none; }}
.hm-h, .hm-d {{ font-size:12px; color:var(--muted); text-align:center; }}
.hm-d {{ text-align:left; line-height:30px; }}
.hm-c {{ height:30px; border-radius:3px; font-size:11px; display:flex; align-items:center; justify-content:center;
  font-variant-numeric:tabular-nums; color:var(--ink);
  background:color-mix(in srgb, var(--blue) calc(var(--a, 0) * 100%), var(--track)); }}
.hm-c.dark {{ color:#fff; }}
.hm-c.off {{ background:none; }}
.hm-c.na {{ background:repeating-linear-gradient(45deg, transparent 0 3px, var(--line) 3px 4px); }}
table {{ width:100%; border-collapse:collapse; font-size:14px; }}
th, td {{ text-align:left; padding:7px 4px; border-bottom:1px solid var(--line); }}
th {{ color:var(--ink2); font-weight:600; }} td.num, th.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
.two {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(280px, 1fr)); gap:24px; }}
ul {{ padding-left:18px; margin:8px 0 0; }} li {{ margin:4px 0; }}
footer {{ color:var(--muted); font-size:13px; margin-top:24px; }}
#tip {{ position:fixed; pointer-events:none; z-index:10; max-width:260px; background:var(--ink); color:var(--card);
  font-size:13px; line-height:1.4; padding:6px 9px; border-radius:6px; opacity:0; transition:opacity .08s; }}
@media (max-width:560px) {{
  .row {{ grid-template-columns:84px 1fr; }} .row .val {{ grid-column:2; font-size:13px; }}
  .lead {{ font-size:18px; }} h1 {{ font-size:25px; }} section {{ padding:16px; }}
  .t-value {{ font-size:22px; }} .tile {{ padding:12px; }}
}}
@media print {{ body {{ background:#fff; }} section {{ break-inside:avoid; }} .controls select {{ display:none; }} }}
</style></head><body><main>
<h1>Lege indoorpistes bij Arenal</h1>
<p class="sub">{esc(period_txt) if cov else ""}</p>
{status}
<p class="lead">{lead}</p>
<div class="tiles">{tiles_html}</div>

<section>
  <h2>Waar staan de meeste piekuren leeg?</h2>
  <p class="sub">Gemiste verhuur in piekuren per club, {unit}. Naast het bedrag: per indoorpiste, zodat grote en kleine clubs vergelijkbaar zijn.</p>
  <div class="bars">{rank_rows}</div>
</section>

<section>
  <h2>Wanneer staan de pistes leeg?</h2>
  <p class="sub">Gemiddeld aandeel indoorpistes dat leeg bleef, per weekdag en piekuur. Lege vakjes zijn daluren en tellen niet mee.</p>
  <div class="controls">
    <select id="club" aria-label="Club">{options}</select>
    <div class="scale"><span>0%</span><span class="grad"></span><span>100% leeg</span><span class="nm"></span><span>nog niet gemeten</span></div>
  </div>
  <div class="scroll">{heatmaps}</div>
</section>

<section>
  <h2>Wat gebeurt er met geannuleerde piekuren?</h2>
  <p class="sub">Piekuren die geboekt waren en weer vrijkwamen, per club.</p>
  {canc_html}
</section>

<section class="two">
  <div>
    <h2>Wat staat er op het spel?</h2>
    <p class="sub">Extra verhuur als een deel van de lege piekuren alsnog verkocht wordt, {unit}.</p>
    <table><tr><th>Scenario</th><th class="num">Extra omzet</th></tr>{scen_rows}</table>
  </div>
  <div>
    <h2>Hoe is dit gemeten?</h2>
    <ul>
      <li>Elke 15 minuten de publieke beschikbaarheid van alle indoorpistes bij Arenal, 14 dagen vooruit.</li>
      <li><b>Leeg</b>: een piste die nog vrij was toen het speeluur begon. Zeker niet verkocht.</li>
      <li><b>Geannuleerd</b>: geboekte speeltijd die weer vrij kwam. Herboekt binnen 15 minuten is onzichtbaar, dus het echte aantal annuleringen ligt hoger.</li>
      <li>Piekuren: weekdagen {wk[0]}-{wk[1]} u, weekend {we[0]}-{we[1]} u (zoals in Meise), voor elke club.</li>
      <li>Alleen piekuren tellen mee: dat leeg blijven in daluren is normaal en weinig verlies.</li>
      <li>Halfuren waarin niet gemeten kon worden (storing bij Arenal of de meting), tellen niet mee:
        niet als leeg en niet als gemeten capaciteit.</li>
      <li>Buitenpistes tellen niet mee. De uren zijn exact, de bedragen hangen af van de tarieven hiernaast.</li>
    </ul>
  </div>
</section>

<section>
  <h2>Gebruikte tarieven</h2>
  <p class="sub">Piektarief indoor dubbel, per piste per uur (4 spelers). "website" = prijs van de clubpagina, "aanname" = geen indoorprijs gevonden.</p>
  <div class="scroll"><table><tr><th>Club</th><th class="num">Piek</th><th>Bron</th></tr>{price_rows}</table></div>
</section>

<footer>Bedragen zijn verhuur aan de standaardprijs, zonder kortingen of abonnementen. Bijgewerkt {datetime.now(TZ):%d/%m/%Y %H:%M}.</footer>
</main>
<div id="tip" role="tooltip"></div>
<script>
(function () {{
  var sel = document.getElementById("club");
  sel.addEventListener("change", function () {{
    document.querySelectorAll(".hm").forEach(function (h) {{ h.hidden = h.dataset.club !== sel.value; }});
  }});
  var tip = document.getElementById("tip");
  document.addEventListener("pointerover", function (e) {{
    var t = e.target.closest("[data-tip]");
    if (!t) {{ tip.style.opacity = 0; return; }}
    tip.textContent = t.dataset.tip; tip.style.opacity = 1;
  }});
  document.addEventListener("pointermove", function (e) {{
    var x = Math.min(e.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
    tip.style.left = x + "px"; tip.style.top = (e.clientY + 16) + "px";
  }});
}})();
</script>
</body></html>"""
    path.write_text(page, encoding="utf-8")


def main():
    conn = connect(DB_PATH)
    clubs, events, runs, courts = load(conn)
    cov = coverage(runs)
    res = analyse(clubs, events, courts, cov["blind"] if cov else ())
    if cov:
        print(f"Tracked {cov['days']:.1f} days, {cov['n']} checks "
              f"({cov['failed']} with errors, {len(cov['gaps'])} gaps over 30 min)\n")
    for m in res.values():
        print("\n".join(summary_lines(m, cov)) + "\n")
    write_csv(clubs, events, OUT_DIR / "events.csv")
    write_html(res, cov, OUT_DIR / "report.html")
    write_directie(res, cov, OUT_DIR / "directie.html")
    print(f"Wrote {OUT_DIR / 'report.html'}, {OUT_DIR / 'directie.html'} and {OUT_DIR / 'events.csv'}")


if __name__ == "__main__":
    main()
