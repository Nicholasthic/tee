#!/usr/bin/env python3
"""
tee — show every four-ball available nearby, next 7 days.

    python3 tee.py                  grouped list in the terminal
    python3 tee.py --open           also build tee.html and open it
    python3 tee.py --players 2      any group size
    python3 tee.py --before 10:00   morning only
    python3 tee.py --day sat        one weekday only
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

import scan as engine

ROOT = Path(__file__).parent
HTML_OUT = ROOT / "tee.html"

DAY_ALIASES = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3,
    "fri": 4, "sat": 5, "sun": 6,
}


def collect(days: int, players: int, max_drive: int, all_fees: bool):
    config = yaml.safe_load((ROOT / "clubs.yaml").read_text())
    clubs = [c for c in config["clubs"]
             if c.get("enabled", True) and c.get("drive_min", 0) <= max_drive]

    session = engine.make_session()
    openings, problems = [], []

    for club in clubs:
        print(f"  checking {club['name']}…", flush=True, file=sys.stderr)
        found, err = engine.scan_club(session, club, days, players, None, all_fees)
        if err:
            problems.append(err)
        openings.extend(found)

    return openings, problems


def apply_filters(openings, before: str | None, after: str | None,
                  weekday: int | None, empty_only: bool = False):
    out = []
    for o in openings:
        if empty_only and not o.is_empty_row:
            continue
        if before and o.tee_time >= before:
            continue
        if after and o.tee_time <= after:
            continue
        if weekday is not None:
            if datetime.strptime(o.day, "%Y-%m-%d").weekday() != weekday:
                continue
        out.append(o)
    return out


def pretty_day(iso: str) -> str:
    d = datetime.strptime(iso, "%Y-%m-%d").date()
    today = date.today()
    if d == today:
        prefix = "Today"
    elif d == today + timedelta(days=1):
        prefix = "Tomorrow"
    else:
        prefix = d.strftime("%a")
    return f"{prefix} {d.strftime('%-d %b')}"


def pretty_time(hhmm: str) -> str:
    h, m = int(hhmm[:2]), hhmm[3:]
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12}:{m}{suffix}"


def merge_fees(group) -> str:
    """Several fee groups often serve the same sheet. Show them as one line."""
    seen = []
    for g in group:
        if g.fee_label not in seen:
            seen.append(g.fee_label)
    if len(seen) == 1:
        return seen[0]
    return "  ·  ".join(seen[:3]) + ("  ·  …" if len(seen) > 3 else "")


# ---------------------------------------------------------------- terminal

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"


def print_table(openings, players: int):
    if not openings:
        print(f"\nNothing with {players}+ spots in that window.")
        return

    by_day = defaultdict(list)
    for o in openings:
        by_day[o.day].append(o)

    print()
    for day in sorted(by_day):
        slots = sorted(by_day[day], key=lambda o: (o.tee_time, o.drive_min))
        uniq = len({(x.club, x.tee_time) for x in slots})
        print(f"{BOLD}{pretty_day(day)}{RESET}  {DIM}{uniq} slots{RESET}")

        # One row per club. The same sheet is served under several fee groups
        # (walking / cart), so merge them and list the fee options together.
        by_club = defaultdict(list)
        for s in slots:
            by_club[(s.club, s.drive_min)].append(s)

        def order(kv):
            return (min(g.tee_time for g in kv[1]), kv[0][1])

        for (club, drive), group in sorted(by_club.items(), key=order):
            fee = merge_fees(group)
            times = sorted({g.tee_time for g in group})
            if len(times) > 6:
                shown = f"{pretty_time(times[0])} – {pretty_time(times[-1])}  ({len(times)} times)"
            else:
                shown = ", ".join(pretty_time(t) for t in times)
            occ = ""
            if any(not g.is_empty_row for g in group):
                occ = f" {DIM}(shared){RESET}"
            print(f"  {club:<26} {DIM}{drive:>2}min{RESET}  {shown}{occ}")
            print(f"  {DIM}{'':<26}        {fee}{RESET}")
        print()


# ---------------------------------------------------------------- html

def build_html(openings, players: int) -> str:
    by_day = defaultdict(list)
    for o in openings:
        by_day[o.day].append(o)

    cards = []
    for day in sorted(by_day):
        slots = sorted(by_day[day], key=lambda o: (o.tee_time, o.drive_min))
        by_club = defaultdict(list)
        for s in slots:
            by_club[(s.club, s.drive_min)].append(s)

        rows = []
        def order(kv):
            return (min(g.tee_time for g in kv[1]), kv[0][1])

        for (club, drive), group in sorted(by_club.items(), key=order):
            fee = merge_fees(group)
            url = min(group, key=lambda g: g.tee_time).url
            times = sorted({g.tee_time for g in group})
            chips = "".join(f"<span class=t>{pretty_time(t)}</span>" for t in times[:14])
            more = f"<span class=more>+{len(times)-14}</span>" if len(times) > 14 else ""
            rows.append(f"""
        <a class="club" href="{url}" target="_blank" rel="noopener">
          <div class="ch">
            <span class="cn">{club}</span>
            <span class="dr">{drive} min{"" if all(g.is_empty_row for g in group) else " · shared"}</span>
          </div>
          <div class="fee">{fee}</div>
          <div class="times">{chips}{more}</div>
        </a>""")

        cards.append(f"""
      <section class="day">
        <h2>{pretty_day(day)}<span class="ct">{len({(x.club, x.tee_time) for x in slots})}</span></h2>
        {''.join(rows)}
      </section>""")

    body = "".join(cards) or '<p class="empty">Nothing open in that window.</p>'
    stamp = datetime.now().strftime("%-d %b, %-I:%M%p").lower()

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tee times</title>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {{
    --ink:#14161a; --bone:#f4f1ea; --line:#d9d3c6;
    --moss:#3f5641; --muted:#7b756a;
  }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{
    background:var(--bone); color:var(--ink);
    font-family:'IBM Plex Mono',ui-monospace,monospace;
    font-size:13px; line-height:1.5;
    padding:48px 20px 80px; -webkit-font-smoothing:antialiased;
  }}
  .wrap {{ max-width:660px; margin:0 auto; }}
  header {{ border-bottom:2px solid var(--ink); padding-bottom:14px; margin-bottom:40px; }}
  h1 {{ font-family:'Instrument Serif',serif; font-size:44px; font-weight:400; line-height:1; }}
  h1 em {{ font-style:italic; color:var(--moss); }}
  .sub {{ font-size:11px; letter-spacing:.14em; text-transform:uppercase;
         color:var(--muted); margin-top:10px; }}
  .day {{ margin-bottom:38px; }}
  .day h2 {{
    font-family:'Instrument Serif',serif; font-size:22px; font-weight:400;
    display:flex; align-items:baseline; gap:12px;
    border-bottom:1px solid var(--line); padding-bottom:7px; margin-bottom:4px;
  }}
  .day h2 .ct {{ font-family:'IBM Plex Mono',monospace; font-size:10px;
                color:var(--muted); letter-spacing:.1em; }}
  .club {{
    display:block; text-decoration:none; color:inherit;
    padding:15px 0 17px; border-bottom:1px solid var(--line);
    transition:padding-left .12s ease;
  }}
  .club:hover {{ padding-left:9px; }}
  .club:hover .cn {{ color:var(--moss); }}
  .ch {{ display:flex; justify-content:space-between; align-items:baseline; }}
  .cn {{ font-size:15px; font-weight:500; }}
  .dr {{ font-size:10px; color:var(--muted); letter-spacing:.08em; }}
  .fee {{ font-size:11px; color:var(--muted); margin:2px 0 9px; }}
  .times {{ display:flex; flex-wrap:wrap; gap:5px; }}
  .t {{ border:1px solid var(--line); padding:3px 8px; font-size:11px;
       background:#fff; }}
  .more {{ font-size:11px; color:var(--muted); padding:3px 2px; }}
  .empty {{ color:var(--muted); padding:40px 0; }}
  footer {{ margin-top:56px; font-size:10px; color:var(--muted);
           letter-spacing:.1em; text-transform:uppercase; }}
</style></head><body>
<div class="wrap">
  <header>
    <h1>Four-<em>balls</em></h1>
    <div class="sub">{players}+ spots · within 45 min · updated {stamp}</div>
  </header>
  {body}
  <footer>Tap a club to book · prices at the club</footer>
</div>
</body></html>"""


# ---------------------------------------------------------------- main

def main() -> int:
    p = argparse.ArgumentParser(description="Show available tee times nearby.")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--players", type=int, default=4)
    p.add_argument("--max-drive", type=int, default=45)
    p.add_argument("--before", metavar="HH:MM", help="only times before this")
    p.add_argument("--after", metavar="HH:MM", help="only times after this")
    p.add_argument("--day", help="mon tue wed thu fri sat sun")
    p.add_argument("--shared", action="store_true",
                   help="also show rows that already have players on them")
    p.add_argument("--all-fees", action="store_true")
    p.add_argument("--open", action="store_true", help="write tee.html and open it")
    args = p.parse_args()

    weekday = None
    if args.day:
        key = args.day[:3].lower()
        if key not in DAY_ALIASES:
            print(f"Unknown day: {args.day}", file=sys.stderr)
            return 1
        weekday = DAY_ALIASES[key]

    openings, problems = collect(args.days, args.players, args.max_drive, args.all_fees)
    openings = apply_filters(openings, args.before, args.after, weekday,
                             empty_only=not args.shared)
    openings.sort(key=lambda o: (o.day, o.tee_time, o.drive_min))

    for msg in problems:
        print(f"  ! {msg}", file=sys.stderr)

    print_table(openings, args.players)

    if args.open:
        HTML_OUT.write_text(build_html(openings, args.players), encoding="utf-8")
        print(f"Wrote {HTML_OUT}")
        if sys.platform == "darwin":
            subprocess.run(["open", str(HTML_OUT)], check=False)

    return 0


if __name__ == "__main__":
    sys.exit(main())
