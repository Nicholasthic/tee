#!/usr/bin/env python3
"""
build.py — scan every club and write a self-contained web page.

    python3 build.py                 writes docs/index.html
    python3 build.py --open          also opens it locally

The page embeds all results as JSON and does its filtering in the browser,
so tapping between days and time windows is instant with no server.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import yaml

import scan as engine

ROOT = Path(__file__).parent
OUT_DIR = ROOT / "docs"
OUT_FILE = OUT_DIR / "index.html"


def collect(days: int, max_drive: int, all_fees: bool):
    config = yaml.safe_load((ROOT / "clubs.yaml").read_text())
    clubs = [c for c in config["clubs"]
             if c.get("enabled", True) and c.get("drive_min", 0) <= max_drive]

    session = engine.make_session()
    openings, problems = [], []

    for club in clubs:
        print(f"  {club['name']}…", flush=True, file=sys.stderr)
        # min_players=1 so the page can filter to any group size client-side.
        found, err = engine.scan_club(session, club, days, 1, None, all_fees)
        if err:
            problems.append(err)
        openings.extend(found)

    return openings, problems


def to_records(openings) -> list[dict]:
    """One record per club+day+time, with fee options merged."""
    merged: dict[tuple, dict] = {}
    for o in openings:
        key = (o.club, o.day, o.tee_time)
        rec = merged.get(key)
        if rec is None:
            merged[key] = {
                "club": o.club,
                "drive": o.drive_min,
                "day": o.day,
                "time": o.tee_time,
                "free": o.free,
                "fees": [o.fee_label],
                "url": o.url,
            }
        else:
            rec["free"] = max(rec["free"], o.free)
            if o.fee_label not in rec["fees"]:
                rec["fees"].append(o.fee_label)

    out = list(merged.values())
    out.sort(key=lambda r: (r["day"], r["time"], r["drive"]))
    return out


TEMPLATE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="theme-color" content="#f4f1ea">
<title>Tee</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {
    --ink:#14161a; --bone:#f4f1ea; --card:#fffdf8;
    --line:#ded7c8; --moss:#3d5a40; --clay:#a8563c; --muted:#7d766a;
  }
  * { box-sizing:border-box; margin:0; padding:0; -webkit-tap-highlight-color:transparent; }
  html { background:var(--bone); }
  body {
    background:var(--bone); color:var(--ink);
    font-family:'IBM Plex Mono',ui-monospace,SFMono-Regular,monospace;
    font-size:13px; line-height:1.45;
    padding:0 0 calc(40px + env(safe-area-inset-bottom));
    -webkit-font-smoothing:antialiased;
  }
  .wrap { max-width:620px; margin:0 auto; padding:0 18px; }

  header { padding:32px 0 0; }
  h1 {
    font-family:'Instrument Serif',Georgia,serif;
    font-size:52px; font-weight:400; line-height:.92; letter-spacing:-.01em;
  }
  h1 em { font-style:italic; color:var(--moss); }
  .stamp {
    font-size:10px; letter-spacing:.16em; text-transform:uppercase;
    color:var(--muted); margin-top:12px;
  }

  /* signature element: the ruled scorecard bar under the masthead */
  .rule { display:flex; gap:3px; margin:16px 0 0; }
  .rule span { height:6px; flex:1; border:1px solid var(--ink); border-bottom:none; }
  .rule span:nth-child(4n) { background:var(--ink); }

  .controls {
    position:sticky; top:0; z-index:10;
    background:var(--bone); border-bottom:1px solid var(--ink);
    padding:14px 0 12px; margin-bottom:6px;
  }
  .strip { display:flex; gap:6px; overflow-x:auto; scrollbar-width:none; }
  .strip::-webkit-scrollbar { display:none; }
  .strip + .strip { margin-top:8px; }
  button {
    font:inherit; font-size:11px; letter-spacing:.04em;
    background:transparent; color:var(--muted);
    border:1px solid var(--line); border-radius:0;
    padding:7px 11px; white-space:nowrap; cursor:pointer;
    transition:none;
  }
  button.on { background:var(--ink); color:var(--bone); border-color:var(--ink); }
  button .n { opacity:.55; margin-left:5px; }

  .day-h {
    font-family:'Instrument Serif',Georgia,serif;
    font-size:24px; font-weight:400;
    margin:30px 0 0; padding-bottom:6px;
    border-bottom:1px solid var(--line);
    display:flex; justify-content:space-between; align-items:baseline;
  }
  .day-h .c { font-family:'IBM Plex Mono',monospace; font-size:10px;
              color:var(--muted); letter-spacing:.12em; }

  a.club {
    display:block; text-decoration:none; color:inherit;
    background:var(--card); border:1px solid var(--line); border-top:none;
    padding:15px 15px 17px;
  }
  a.club:first-of-type { border-top:1px solid var(--line); }
  a.club:active { background:#f7f2e6; }
  .top { display:flex; justify-content:space-between; align-items:baseline; gap:10px; }
  .name { font-size:15px; font-weight:500; letter-spacing:-.01em; }
  .drive { font-size:10px; color:var(--muted); letter-spacing:.08em; white-space:nowrap; }
  .fees { font-size:10.5px; color:var(--muted); margin:3px 0 10px; }
  .times { display:flex; flex-wrap:wrap; gap:4px; }
  .t {
    border:1px solid var(--line); background:var(--bone);
    padding:4px 8px; font-size:11px; letter-spacing:.02em;
  }
  .t.first { border-color:var(--moss); color:var(--moss); font-weight:500; }
  .more { font-size:10.5px; color:var(--muted); padding:4px 2px; align-self:center; }

  .empty {
    color:var(--muted); padding:56px 4px; font-size:12.5px;
    border-top:1px solid var(--line); margin-top:8px;
  }
  .empty b { display:block; font-family:'Instrument Serif',serif;
             font-size:20px; color:var(--ink); font-weight:400; margin-bottom:6px; }
  footer {
    margin-top:44px; padding-top:14px; border-top:1px solid var(--line);
    font-size:9.5px; letter-spacing:.12em; text-transform:uppercase; color:var(--muted);
  }
  @media (min-width:560px) { h1 { font-size:64px; } }
</style>
</head><body>
<div class="wrap">
  <header>
    <h1>Tee<br><em>times</em></h1>
    <div class="rule">__RULE__</div>
    <div class="stamp">__STAMP__ · __NCLUB__ clubs · within __DRIVE__ min</div>
  </header>

  <div class="controls">
    <div class="strip" id="days"></div>
    <div class="strip" id="opts"></div>
  </div>

  <div id="list"></div>

  <footer>Tap a club to book · MiClub public sheets</footer>
</div>

<script>
const DATA = __DATA__;

const state = { day: 'all', when: 'all', players: 4, drive: 99 };

const fmt = t => {
  const h = +t.slice(0,2), m = t.slice(3);
  const s = h < 12 ? 'am' : 'pm';
  return ((h % 12) || 12) + ':' + m + s;
};
const dayLabel = iso => {
  const d = new Date(iso + 'T00:00:00');
  const today = new Date(); today.setHours(0,0,0,0);
  const diff = Math.round((d - today) / 86400000);
  if (diff === 0) return 'Today';
  if (diff === 1) return 'Tmrw';
  return d.toLocaleDateString('en-AU', { weekday:'short' });
};
const dayFull = iso => {
  const d = new Date(iso + 'T00:00:00');
  const today = new Date(); today.setHours(0,0,0,0);
  const diff = Math.round((d - today) / 86400000);
  const nice = d.toLocaleDateString('en-AU', { weekday:'long', day:'numeric', month:'short' });
  if (diff === 0) return 'Today · ' + nice;
  if (diff === 1) return 'Tomorrow · ' + nice;
  return nice;
};

function matches(r) {
  if (state.day !== 'all' && r.day !== state.day) return false;
  if (r.free < state.players) return false;
  if (r.drive > state.drive) return false;
  const h = +r.time.slice(0,2);
  if (state.when === 'early' && h >= 9) return false;
  if (state.when === 'mid' && (h < 9 || h >= 13)) return false;
  if (state.when === 'late' && h < 13) return false;
  return true;
}

function chip(label, on, count, fn) {
  const b = document.createElement('button');
  b.className = on ? 'on' : '';
  b.innerHTML = label + (count !== null ? ' <span class="n">' + count + '</span>' : '');
  b.onclick = fn;
  return b;
}

function countFor(patch) {
  const saved = { ...state };
  Object.assign(state, patch);
  const n = new Set(DATA.filter(matches).map(r => r.club + r.day + r.time)).size;
  Object.assign(state, saved);
  return n;
}

function render() {
  // day strip
  const days = [...new Set(DATA.map(r => r.day))].sort();
  const ds = document.getElementById('days');
  ds.innerHTML = '';
  ds.appendChild(chip('All', state.day === 'all', countFor({ day:'all' }),
    () => { state.day = 'all'; render(); }));
  days.forEach(d => ds.appendChild(chip(dayLabel(d), state.day === d, countFor({ day:d }),
    () => { state.day = d; render(); })));

  // options strip
  const os = document.getElementById('opts');
  os.innerHTML = '';
  [['all','Any time'],['early','Before 9'],['mid','9–1'],['late','After 1']].forEach(([k,l]) =>
    os.appendChild(chip(l, state.when === k, null, () => { state.when = k; render(); })));
  os.appendChild(chip(state.players === 4 ? '4-ball' : state.players + ' players',
    state.players === 4, null,
    () => { state.players = state.players === 4 ? 2 : 4; render(); }));
  os.appendChild(chip(state.drive === 99 ? 'Any drive' : '≤' + state.drive + 'min',
    state.drive !== 99, null,
    () => { state.drive = state.drive === 99 ? 20 : 99; render(); }));

  // list
  const rows = DATA.filter(matches);
  const list = document.getElementById('list');
  list.innerHTML = '';

  if (!rows.length) {
    list.innerHTML = '<div class="empty"><b>Nothing open.</b>' +
      'Try a wider time window, or drop to 2 players.</div>';
    return;
  }

  const byDay = {};
  rows.forEach(r => (byDay[r.day] = byDay[r.day] || []).push(r));

  Object.keys(byDay).sort().forEach(day => {
    const byClub = {};
    byDay[day].forEach(r => (byClub[r.club] = byClub[r.club] || []).push(r));

    const groups = Object.values(byClub)
      .sort((a,b) => a[0].time.localeCompare(b[0].time) || a[0].drive - b[0].drive);

    const h = document.createElement('div');
    h.className = 'day-h';
    h.innerHTML = '<span>' + dayFull(day) + '</span><span class="c">' +
                  byDay[day].length + '</span>';
    list.appendChild(h);

    groups.forEach(g => {
      g.sort((a,b) => a.time.localeCompare(b.time));
      const fees = [...new Set(g.flatMap(r => r.fees))];
      const a = document.createElement('a');
      a.className = 'club';
      a.href = g[0].url; a.target = '_blank'; a.rel = 'noopener';
      const chips = g.slice(0,16).map((r,i) =>
        '<span class="t' + (i === 0 ? ' first' : '') + '">' + fmt(r.time) + '</span>').join('');
      const more = g.length > 16 ? '<span class="more">+' + (g.length-16) + '</span>' : '';
      a.innerHTML =
        '<div class="top"><span class="name">' + g[0].club + '</span>' +
        '<span class="drive">' + g[0].drive + ' min</span></div>' +
        '<div class="fees">' + fees.slice(0,2).join(' · ') +
        (fees.length > 2 ? ' · +' + (fees.length-2) : '') + '</div>' +
        '<div class="times">' + chips + more + '</div>';
      list.appendChild(a);
    });
  });
}

render();
</script>
</body></html>
"""


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--max-drive", type=int, default=45)
    p.add_argument("--all-fees", action="store_true")
    p.add_argument("--open", action="store_true")
    args = p.parse_args()

    openings, problems = collect(args.days, args.max_drive, args.all_fees)
    for msg in problems:
        print(f"  ! {msg}", file=sys.stderr)

    records = to_records(openings)
    clubs = len({r["club"] for r in records})
    print(f"{len(records)} tee times across {clubs} clubs", file=sys.stderr)

    stamp = datetime.now().strftime("%-d %b, %-I:%M%p").lower()
    html = (TEMPLATE
            .replace("__DATA__", json.dumps(records, separators=(",", ":")))
            .replace("__STAMP__", stamp)
            .replace("__NCLUB__", str(clubs))
            .replace("__DRIVE__", str(args.max_drive))
            .replace("__RULE__", "<span></span>" * 18))

    OUT_DIR.mkdir(exist_ok=True)
    OUT_FILE.write_text(html, encoding="utf-8")
    print(f"wrote {OUT_FILE}", file=sys.stderr)

    if args.open and sys.platform == "darwin":
        subprocess.run(["open", str(OUT_FILE)], check=False)

    return 0


if __name__ == "__main__":
    sys.exit(main())
