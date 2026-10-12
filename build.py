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
import math
import re
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


def collect_contacts(max_drive: int) -> list[dict]:
    """Public clubs we can't scan, so the page can at least hand you the phone.

    A club qualifies when it's open to visitors, is still operating, and has a
    phone number or a booking link. `enabled` is irrelevant here — these are
    precisely the clubs the scanner skips.
    """
    config = yaml.safe_load((ROOT / "clubs.yaml").read_text())
    out = []

    for club in config["clubs"]:
        if not club.get("public") or club.get("status") == "closed":
            continue
        if club.get("drive_min", 0) > max_drive:
            continue
        if not (club.get("phone") or club.get("book_url")):
            continue
        out.append({
            "club": club["name"],
            "drive": club.get("drive_min", 0),
            "phone": club.get("phone"),
            "phoneText": club.get("phone_display") or club.get("phone"),
            "url": club.get("book_url"),
            "note": club.get("note", ""),
        })

    out.sort(key=lambda c: (c["drive"], c["club"]))
    return out


def repo_slug() -> str:
    """owner/repo, so the page can commit played.yaml back to this repo."""
    try:
        url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""
    m = re.search(r"github\.com[:/]([^/]+/[^/.]+?)(?:\.git)?$", url)
    return m.group(1) if m else ""


def collect_courses() -> list[dict]:
    """Every real course in the registry, and whether the pair has played it.

    Includes clubs the scanner can't reach — Southport books by phone, and
    Tamborine is walking-only — because you can still go and play them. Only
    courses that don't exist or aren't confirmed to are left out.
    """
    clubs = yaml.safe_load((ROOT / "clubs.yaml").read_text())["clubs"]

    played_file = ROOT / "played.yaml"
    record = yaml.safe_load(played_file.read_text()) if played_file.exists() else {}
    played = set(record.get("played") or [])

    skip = {"closed", "unverified"}
    # Why a club isn't scanned, in words the page can just print.
    reasons = {"walking-only": "walking only", "chronogolf": "book by phone"}

    courses = [
        {
            "club": club["name"],
            "drive": club.get("drive_min", 0),
            "scanned": bool(club.get("enabled", True)),
            "note": "" if club.get("enabled", True)
                    else reasons.get(club.get("status", ""), "not scanned"),
        }
        for club in clubs if club.get("status") not in skip
    ]

    # Courses we never scan but can still play — private, resort, or simply
    # outside the radius. The checklist is about rounds, not tee sheets.
    for extra in record.get("courses") or []:
        courses.append({
            "club": extra["name"],
            "drive": extra.get("drive_min", 0),
            "scanned": False,
            "note": extra.get("note", ""),
        })

    known = {c["club"] for c in courses}
    for name in sorted(played - known):
        print(f"  ! played.yaml ticks unknown course {name!r}", file=sys.stderr)

    dupes = sorted({c["club"] for c in courses if
                    [x["club"] for x in courses].count(c["club"]) > 1})
    for name in dupes:
        print(f"  ! {name!r} is listed twice", file=sys.stderr)

    for c in courses:
        c["done"] = c["club"] in played
    courses.sort(key=lambda c: (c["drive"], c["club"]))
    return courses


def collect_scoring() -> tuple[list[dict], dict, list[dict]]:
    """Rounds played, and the scorecard data needed to score them.

    Cards are merged over a generic default so a course with no entry is still
    playable; the app flags it so nobody mistakes the placeholder for the real
    card. Course Rating and Slope have no sensible default, so a round at an
    unrated course is recorded but can't count toward a handicap.
    """
    rf = ROOT / "rounds.yaml"
    rec = (yaml.safe_load(rf.read_text()) or {}) if rf.exists() else {}
    rounds = rec.get("rounds") or []
    golfers = rec.get("golfers") or []

    cf = ROOT / "courses.yaml"
    conf = (yaml.safe_load(cf.read_text()) or {}) if cf.exists() else {}
    default = conf.get("default") or {}
    cards = {}
    for name, card in (conf.get("cards") or {}).items():
        merged = dict(default)
        merged.update(card or {})
        merged["stock"] = not (card or {}).get("par")   # still on the placeholder
        cards[name] = merged

    known = {c["name"] for c in yaml.safe_load((ROOT / "clubs.yaml").read_text())["clubs"]}
    played_file = ROOT / "played.yaml"
    if played_file.exists():
        rec = yaml.safe_load(played_file.read_text()) or {}
        known |= {c["name"] for c in (rec.get("courses") or [])}

    for name in sorted({r.get("course") for r in rounds} - known - {None}):
        print(f"  ! rounds.yaml has a round at unknown course {name!r}", file=sys.stderr)
    for name in sorted(set(cards) - known):
        print(f"  ! courses.yaml has a card for unknown course {name!r}", file=sys.stderr)

    cards["__default__"] = {**default, "stock": True}
    return rounds, cards, golfers


def _metres(a, b) -> float:
    lat = math.radians((a[0] + b[0]) / 2)
    dx = math.radians(b[1] - a[1]) * math.cos(lat)
    dy = math.radians(b[0] - a[0])
    return math.hypot(dx, dy) * 6371000


def dedupe_holes(holes: list[dict]) -> list[dict]:
    """Collapse the same hole mapped more than once.

    Palmer Colonial comes back as three overlapping copies of its eighteen -
    somebody mapped it three times - and only a third of the copies carry a
    green.

    Two holes are the same hole when they share a green, which is the test
    that doesn't also merge the 7th of one course with the 7th of the other
    at a 36-hole club. Where a copy has no green, fall back to the tee, and
    make it agree on length too, so a short 7th and a long 7th stay apart.
    """
    def same(a, b):
        if a.get("hole") != b.get("hole"):
            return False
        if a.get("green") and b.get("green"):
            return _metres(a["green"], b["green"]) < 40
        if not (a.get("tee") and b.get("tee")):
            return False
        la, lb = a.get("len") or 0, b.get("len") or 0
        close = abs(la - lb) <= 0.2 * max(la, lb, 1)
        return _metres(a["tee"], b["tee"]) < 40 and close

    out = []
    for h in holes:
        twin = next((k for k in out if same(k, h)), None)
        if twin is None:
            out.append(h)
            continue
        # keep the better copy: one with a green beats one without
        better = sorted((twin, h), key=lambda r: (bool(r.get("green")), r.get("len") or 0))[-1]
        out[out.index(twin)] = better
    return out


def collect_holes() -> dict:
    """Hole geometry from holes.yaml, for the GPS yardages.

    Only courses with a usable layout go in: whole nines, numbered 1..9 or
    1..18 with nothing missing, and a green on most of them. A course with
    three holes mapped is worse than none, because the app would offer a
    yardage view that mostly says nothing. One hole short of a green is fine -
    that hole just doesn't get a number.

    A 36-hole club comes back as two courses both numbered 1 to 18, and OSM
    gives no way to tell which is which. That is fine here: you are standing
    on one of them, so the nearest hole is your hole and its number is the
    number on the card. It only rules out the par prefill, which needs one
    unambiguous eighteen.

    holes.py writes the file by hand from OpenStreetMap. It is optional - the
    page builds fine without it and simply doesn't offer yardages.
    """
    hf = ROOT / "holes.yaml"
    if not hf.exists():
        return {}
    rec = (yaml.safe_load(hf.read_text()) or {}).get("courses") or {}

    out, skipped = {}, []
    for name, course in rec.items():
        holes = dedupe_holes(course.get("holes") or [])
        refs = [h.get("hole") for h in holes]
        greens = sum(1 for h in holes if h.get("green"))

        loops = 0
        for m in (27, 18, 9):
            if len(holes) % m == 0 and sorted(refs) == sorted(
                    list(range(1, m + 1)) * (len(holes) // m)):
                loops = len(holes) // m
                break

        if not loops:
            why = f"{len(holes)} holes, numbered " + (
                ", ".join(str(r) for r in sorted(set(refs), key=lambda x: (x is None, x))) or "nothing")
        elif greens < 0.7 * len(holes):
            why = f"only {greens} of {len(holes)} greens mapped"
        else:
            why = ""
        if why:
            skipped.append(f"{name} ({why})")
            continue

        out[name] = {
            "at": course.get("at"),
            "loops": loops,
            "holes": [{k: h[k] for k in ("hole", "par", "tee", "len", "green", "line")
                       if h.get(k) is not None} for h in holes],
        }

    if skipped:
        print(f"  {len(skipped)} courses have no usable hole map: "
              + ", ".join(skipped), file=sys.stderr)
    return out


def _cached_page() -> str:
    if not OUT_FILE.exists():
        raise SystemExit("no docs/index.html to reuse — run without --offline first")
    return OUT_FILE.read_text(encoding="utf-8")


def cached_records(max_drive: int) -> list[dict]:
    """Re-read the records embedded in the last built page.

    Lets you iterate on the template without hammering the clubs again.
    Records are re-filtered against clubs.yaml, so disabling a club and
    rebuilding offline actually drops it instead of leaving stale rows on
    the page.
    """
    m = re.search(r"const DATA = (\[.*?\]);", _cached_page(), re.S)
    if not m:
        raise SystemExit("could not find embedded data in docs/index.html")
    records = json.loads(m.group(1))

    config = yaml.safe_load((ROOT / "clubs.yaml").read_text())
    live = {c["name"] for c in config["clubs"]
            if c.get("enabled", True) and c.get("drive_min", 0) <= max_drive}

    kept = [r for r in records if r["club"] in live]
    dropped = {r["club"] for r in records} - live
    for name in sorted(dropped):
        print(f"  dropped {name} (no longer enabled)", file=sys.stderr)
    return kept


def cached_built() -> str:
    m = re.search(r'const BUILT = "([^"]*)"', _cached_page())
    return m.group(1) if m else datetime.now().astimezone().isoformat()


def cached_problems() -> list[str]:
    """Carry failures through an offline rebuild — they belong to the scan."""
    m = re.search(r"const PROBLEMS = (\[.*?\]);", _cached_page(), re.S)
    try:
        return json.loads(m.group(1)) if m else []
    except json.JSONDecodeError:
        return []


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
<meta name="color-scheme" content="light dark">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#f5f3ec" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#080d0a" media="(prefers-color-scheme: dark)">
<title>S&amp;N Go Golfing</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http%3A//www.w3.org/2000/svg' viewBox='0 0 64 64'><rect width='64' height='64' rx='15' fill='%2314653f'/><path d='M10 56.5H54' stroke='%23fbfdf8' stroke-width='2.6' stroke-linecap='round' opacity='.4'/><path d='M18 10 28.5 38' stroke='%23fbfdf8' stroke-width='4.2' stroke-linecap='round' fill='none'/><path d='M29.5 36 33 46.5 14 52.5 10.5 43Z' fill='%23fbfdf8'/><path d='M40 37H54L50.5 41H43.5Z M45.2 41H48.8L48 53H46Z' fill='%23fbfdf8'/><circle cx='47' cy='29.5' r='6.8' fill='%23fbfdf8' stroke='%2314653f' stroke-width='2.6'/></svg>">
<link rel="apple-touch-icon" href="data:image/svg+xml,<svg xmlns='http%3A//www.w3.org/2000/svg' viewBox='0 0 64 64'><rect width='64' height='64' rx='0' fill='%2314653f'/><path d='M10 56.5H54' stroke='%23fbfdf8' stroke-width='2.6' stroke-linecap='round' opacity='.4'/><path d='M18 10 28.5 38' stroke='%23fbfdf8' stroke-width='4.2' stroke-linecap='round' fill='none'/><path d='M29.5 36 33 46.5 14 52.5 10.5 43Z' fill='%23fbfdf8'/><path d='M40 37H54L50.5 41H43.5Z M45.2 41H48.8L48 53H46Z' fill='%23fbfdf8'/><circle cx='47' cy='29.5' r='6.8' fill='%23fbfdf8' stroke='%2314653f' stroke-width='2.6'/></svg>">
<script>try{var _t=localStorage.getItem('tee.theme');document.documentElement.dataset.theme=_t||(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light')}catch(e){}</script>
<style>
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}

  /* Palette borrowed from the course itself: fairway green, bunker sand,
     clubhouse paper, and a flag red used sparingly for the pick of the day. */
  :root{
    --bg:#f4f2ea; --surface:#fffefa; --surface2:#eceadd; --raise:#fffefa;
    --border:#e1ddcc; --border2:#c6bfa8;
    --text:#101711; --dim:#69705f; --faint:#989b88;
    --accent:#14653f; --accent-ink:#fbfdf8; --accent-bg:#e3efe4; --accent-line:#b2d2bb;
    --sand:#b98f3e; --sand-bg:#f6ecd6; --sand-line:#e4cf9f;
    --flag:#b6432f;
    --mow:rgba(20,101,63,.05);
    --dawn:rgba(214,158,74,.13); --dusk:rgba(58,84,132,.1);
    --shadow:0 1px 2px rgba(30,34,20,.05), 0 12px 28px -20px rgba(30,34,20,.45);
    --radius:14px;
    color-scheme:light;
  }
  @media (prefers-color-scheme:dark){
    :root:not([data-theme="light"]){
      --bg:#080d0a; --surface:#0f1611; --surface2:#161f18; --raise:#212c24;
      --border:#1e2a21; --border2:#33443a;
      --text:#e9eee6; --dim:#8b968a; --faint:#6a7568;
      --accent:#4bd486; --accent-ink:#04170d; --accent-bg:#0f2a1c; --accent-line:#1d4632;
      --sand:#d6b06a; --sand-bg:#241d10; --sand-line:#43351d;
      --flag:#e2755d;
      --mow:rgba(75,212,134,.038);
      --dawn:rgba(214,176,106,.075); --dusk:rgba(96,124,180,.085);
      --shadow:0 1px 2px rgba(0,0,0,.45), 0 14px 34px -22px rgba(0,0,0,.95);
      color-scheme:dark;
    }
  }
  :root[data-theme="dark"]{
    --bg:#080d0a; --surface:#0f1611; --surface2:#161f18; --raise:#212c24;
    --border:#1e2a21; --border2:#33443a;
    --text:#e9eee6; --dim:#8b968a; --faint:#6a7568;
    --accent:#4bd486; --accent-ink:#04170d; --accent-bg:#0f2a1c; --accent-line:#1d4632;
    --sand:#d6b06a; --sand-bg:#241d10; --sand-line:#43351d;
    --flag:#e2755d;
    --mow:rgba(75,212,134,.038);
    --dawn:rgba(214,176,106,.075); --dusk:rgba(96,124,180,.085);
    --shadow:0 1px 2px rgba(0,0,0,.45), 0 14px 34px -22px rgba(0,0,0,.95);
    color-scheme:dark;
  }

  body{
    background:var(--bg); color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif;
    font-size:14px; line-height:1.45; -webkit-font-smoothing:antialiased;
    padding-bottom:calc(40px + env(safe-area-inset-bottom));
  }
  .inner{max-width:1180px; margin:0 auto; padding:0 16px}
  @media (min-width:900px){ .inner{padding:0 28px} }
  svg{display:block}
  :focus-visible{outline:2px solid var(--accent); outline-offset:2px}
  @media (prefers-reduced-motion:reduce){ *{transition:none !important} }

  /* ---- masthead ---- */
  .bar{padding:18px 0 12px}
  .bar .inner{display:flex; align-items:center; justify-content:space-between; gap:14px}
  .brand{display:flex; align-items:center; gap:11px; min-width:0}
  /* Iron addressing a ball on a tee, on a mown green tile. Drawn as one
     cream silhouette so it survives small sizes; the ball is knocked out
     from the tee with a tile-coloured stroke so the two stay legible when
     they touch. */
  .mark{
    width:38px; height:38px; flex:none; border-radius:11px;
    background-color:var(--accent); color:var(--accent-ink);
    background-image:repeating-linear-gradient(90deg,
      rgba(255,255,255,.1) 0 5px, transparent 5px 10px);
    display:grid; place-items:center;
    box-shadow:inset 0 0 0 1px rgba(0,0,0,.07);
  }
  .mark svg{width:26px; height:26px}
  .logo .shaft{fill:none; stroke:currentColor; stroke-width:4.2; stroke-linecap:round}
  .logo .head, .logo .tee, .logo .ball{fill:currentColor}
  .logo .ball{stroke:var(--accent); stroke-width:2.6}
  .logo .turfline{fill:none; stroke:currentColor; stroke-width:2.6;
                  stroke-linecap:round; opacity:.4}

  /* a mown strip of fairway between the masthead and the filters */
  .turf{height:7px; background-color:var(--accent);
    background-image:repeating-linear-gradient(90deg,
      rgba(255,255,255,.16) 0 26px, transparent 26px 52px)}
  h1{font-size:19px; font-weight:650; letter-spacing:-.025em; line-height:1.2;
     overflow-wrap:anywhere}
  /* the initials carry the name; the tile carries the game */
  h1 .sn{font-weight:800; color:var(--accent); letter-spacing:-.04em}
  @media (max-width:360px){ h1{font-size:17px} }
  .sub{font-size:11.5px; color:var(--dim); margin-top:1px}
  .icon{
    width:36px; height:36px; flex:none; border-radius:11px; cursor:pointer;
    border:1px solid var(--border); background:var(--surface); color:var(--dim);
    display:grid; place-items:center;
  }
  .icon svg{width:17px; height:17px; fill:none; stroke:currentColor;
            stroke-width:1.8; stroke-linecap:round; stroke-linejoin:round}
  .icon:hover{color:var(--text); border-color:var(--border2)}
  :root[data-theme="dark"] .i-moon, .i-sun{display:none}
  :root[data-theme="dark"] .i-sun{display:block}


  /* ---- rounds ---- */
  .hcaps{display:grid; gap:10px; grid-template-columns:repeat(auto-fit,minmax(170px,1fr));
         margin-top:18px}
  .hcap{background:var(--surface); border:1px solid var(--border);
        border-radius:var(--radius); padding:13px 15px}
  .hcap .who{font-size:12px; font-weight:600; letter-spacing:.02em}
  .hcap .idx{font-size:30px; font-weight:700; letter-spacing:-.035em; margin-top:1px;
             font-variant-numeric:tabular-nums}
  .hcap .idx small{font-size:12.5px; font-weight:500; color:var(--faint); letter-spacing:0}
  .hcap .note{font-size:11px; color:var(--faint); margin-top:3px}

  .roundbar{display:flex; align-items:center; gap:9px; flex-wrap:wrap; margin-top:16px}
  .roundbar .btn{padding:7px 13px; font-size:12.5px}
  .roundbar .hint{font-size:12px; color:var(--dim)}

  .rnds{display:flex; flex-direction:column; gap:7px; margin-top:14px}
  .rnd{display:flex; align-items:center; gap:12px; background:var(--surface);
       border:1px solid var(--border); border-radius:11px; padding:10px 13px; min-width:0}
  .rnd .when{font-size:11px; color:var(--faint); font-variant-numeric:tabular-nums;
             white-space:nowrap; width:52px; flex:none}
  .rnd .where{flex:1; min-width:0}
  .rnd .where .c{font-size:13px; font-weight:600; letter-spacing:-.01em;
                 overflow:hidden; text-overflow:ellipsis; white-space:nowrap}
  .rnd .where .m{font-size:10.5px; color:var(--faint); font-variant-numeric:tabular-nums}
  .rnd .sc{text-align:right; flex:none; font-variant-numeric:tabular-nums}
  .rnd .sc .g{font-size:17px; font-weight:700; letter-spacing:-.02em}
  .rnd .sc .p{font-size:10.5px; color:var(--faint)}
  .rnd .sc .p.over{color:var(--flag)} .rnd .sc .p.under{color:var(--accent)}
  .rnd.uncounted{border-style:dashed; opacity:.8}

  .sheet{background:var(--surface); border:1px solid var(--border);
         border-radius:var(--radius); padding:14px; margin-top:14px}
  .sheet .row{display:flex; gap:9px; flex-wrap:wrap; margin-bottom:11px}
  .sheet label{display:flex; flex-direction:column; gap:4px; font-size:10px;
               letter-spacing:.1em; text-transform:uppercase; color:var(--faint);
               flex:1; min-width:118px}
  .sheet select, .sheet input[type=date]{
    font:inherit; font-size:13px; padding:7px 9px; color:var(--text);
    background:var(--bg); border:1px solid var(--border2); border-radius:9px; width:100%}
  .nine{margin-top:4px}
  .nine .lbl{font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:9.5px;
             letter-spacing:.1em; text-transform:uppercase; color:var(--faint); margin-bottom:4px}
  .holes{display:grid; grid-template-columns:repeat(10,1fr); gap:3px}
  .hole{display:flex; flex-direction:column; align-items:center; gap:2px; min-width:0}
  .hole .n{font-size:9.5px; color:var(--faint); font-variant-numeric:tabular-nums}
  .hole .pr{font-size:9px; color:var(--faint); opacity:.75}
  .hole input{
    width:100%; min-width:0; text-align:center; font:inherit; font-size:14px; font-weight:600;
    padding:6px 0; color:var(--text); background:var(--bg);
    border:1px solid var(--border2); border-radius:7px;
    font-variant-numeric:tabular-nums; -moz-appearance:textfield;
  }
  .hole input::-webkit-outer-spin-button,
  .hole input::-webkit-inner-spin-button{-webkit-appearance:none; margin:0}
  .hole input:focus{border-color:var(--accent); outline:none}
  .hole.sub input{border-color:var(--accent-line); background:var(--accent-bg); color:var(--accent)}
  .hole.over input{border-color:var(--flag)}
  .hole.tot .v{width:100%; text-align:center; font-size:14px; font-weight:700;
               padding:6px 0; font-variant-numeric:tabular-nums}
  .sheet .foot{display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-top:13px}
  .sheet .foot .run{font-size:13px; color:var(--dim); font-variant-numeric:tabular-nums}
  .sheet .foot .run b{color:var(--text); font-weight:650; font-size:15px}
  .sheet .foot .btn{margin-left:auto}
  .sheet .ghost{background:transparent; color:var(--dim); border:1px solid var(--border2)}
  .warnrow{background:var(--sand-bg); border:1px solid var(--sand-line); color:var(--sand);
           border-radius:10px; padding:9px 12px; font-size:12px; margin-top:11px}
  .warnrow b{color:var(--text); font-weight:650}

  .gps{border:1px solid var(--border2); border-radius:12px; padding:11px 13px;
       margin-bottom:11px; background:var(--bg)}
  .gps.live{border-color:var(--accent-line); background:var(--accent-bg)}
  .gps .top{display:flex; align-items:center; gap:9px}
  .gps .ttl{font-size:9.5px; letter-spacing:.11em; text-transform:uppercase;
            color:var(--faint); font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
  .gps .top .btn{margin-left:auto; font-size:12px; padding:5px 11px}
  .gps .read{display:flex; align-items:baseline; gap:10px; margin-top:7px}
  .gps .m{font-size:34px; font-weight:700; letter-spacing:-.03em; line-height:1;
          color:var(--accent); font-variant-numeric:tabular-nums}
  .gps .m small{font-size:15px; font-weight:600; margin-left:2px}
  .gps .where{font-size:12px; color:var(--dim); line-height:1.35}
  .gps .where b{color:var(--text); font-weight:650}
  .gps .note{font-size:11.5px; color:var(--faint); margin-top:6px; line-height:1.4}
  .gps .step{display:flex; gap:6px; margin-top:9px}
  .gps .step button{font:inherit; font-size:12px; cursor:pointer; color:var(--dim);
    background:transparent; border:1px solid var(--border2); border-radius:8px;
    padding:5px 11px}
  .gps .step button:disabled{opacity:.4; cursor:default}
  .gps .step .auto{margin-left:auto}

  .play .phead{display:flex; align-items:baseline; gap:9px; margin-bottom:12px}
  .play .hn{font-size:19px; font-weight:700; letter-spacing:-.02em}
  .play .hm{font-size:12px; color:var(--dim); margin-left:7px}
  .play .phead .btn{margin-left:auto}
  .play .pyds{text-align:center; padding:14px 0 16px}
  .play .big{font-size:76px; line-height:.9; font-weight:700; letter-spacing:-.045em;
             color:var(--accent); font-variant-numeric:tabular-nums}
  .play .big small{font-size:26px; font-weight:600; letter-spacing:-.02em}
  .play .cap{font-size:12.5px; color:var(--dim); margin-top:7px}
  .play .note{font-size:11.5px; color:var(--faint); margin-top:8px; line-height:1.45}
  .play .note button{font:inherit; font-size:11.5px; color:var(--accent); cursor:pointer;
                     background:none; border:0; padding:0; text-decoration:underline}
  .play .pscore{display:flex; align-items:center; gap:12px}
  .play .pscore button{flex:none; width:68px; height:68px; border-radius:50%; cursor:pointer;
    font:inherit; font-size:30px; font-weight:400; color:var(--text);
    background:var(--bg); border:1px solid var(--border2)}
  .play .pscore button:active{background:var(--accent-bg); border-color:var(--accent-line)}
  .play .pscore .val{flex:1; text-align:center; min-width:0}
  .play .pscore .val b{display:block; font-size:44px; font-weight:700; line-height:1;
                       letter-spacing:-.03em; font-variant-numeric:tabular-nums}
  .play .pscore .val span{display:block; font-size:11px; color:var(--faint); margin-top:5px;
                          letter-spacing:.08em; text-transform:uppercase}
  .play .pnav{display:flex; align-items:center; gap:9px; margin-top:16px}
  .play .pnav button{font:inherit; font-size:12.5px; cursor:pointer; color:var(--dim);
    background:transparent; border:1px solid var(--border2); border-radius:9px; padding:7px 12px}
  .play .pnav button:disabled{opacity:.35; cursor:default}
  .play .thru{flex:1; text-align:center; font-size:12px; color:var(--dim);
              font-variant-numeric:tabular-nums}
  .play .thru b{color:var(--text); font-weight:650; font-size:15px}


  .tees{display:flex; flex-direction:column; gap:6px; margin-top:6px}
  .teerow{display:flex; gap:6px; align-items:center}
  .teerow input{font:inherit; font-size:13px; padding:6px 8px; color:var(--text);
    background:var(--bg); border:1px solid var(--border2); border-radius:8px; min-width:0}
  .teerow .nm{flex:2} .teerow .num{flex:1; text-align:center; font-variant-numeric:tabular-nums}
  .teerow button{font:inherit; font-size:12px; cursor:pointer; background:none;
    border:1px solid var(--border2); color:var(--flag); border-radius:8px; padding:5px 9px}
  .srow{display:grid; grid-template-columns:repeat(10,1fr); gap:3px; margin-top:4px}
  .srow .h{display:flex; flex-direction:column; align-items:center; gap:2px; min-width:0}
  .srow .h span{font-size:9.5px; color:var(--faint); font-variant-numeric:tabular-nums}
  .srow .h input{width:100%; min-width:0; text-align:center; font:inherit; font-size:13px;
    padding:5px 0; color:var(--text); background:var(--bg);
    border:1px solid var(--border2); border-radius:6px; font-variant-numeric:tabular-nums;
    -moz-appearance:textfield}
  .srow .h input::-webkit-outer-spin-button,
  .srow .h input::-webkit-inner-spin-button{-webkit-appearance:none; margin:0}
  .srow .h.bad input{border-color:var(--flag)}

  /* ---- view tabs ---- */
  .tabs{display:flex; gap:4px; padding:12px 0 0}
  .tabs button{
    font:inherit; font-size:13px; font-weight:600; cursor:pointer;
    border:1px solid transparent; background:transparent; color:var(--dim);
    border-radius:10px; padding:7px 13px;
  }
  .tabs button[aria-pressed="true"]{
    background:var(--surface); color:var(--text); border-color:var(--border);
  }
  .tabs .tally{font-weight:500; color:var(--faint); margin-left:6px;
               font-variant-numeric:tabular-nums}

  /* ---- courses checklist ---- */
  .scoreboard{display:grid; gap:10px; grid-template-columns:repeat(auto-fit,minmax(190px,1fr));
              margin:22px 0 4px}
  .score{
    background:var(--surface); border:1px solid var(--border);
    border-radius:var(--radius); padding:12px 14px;
  }
  .score .who{font-size:12px; font-weight:600; letter-spacing:.02em}
  .score .num{font-size:24px; font-weight:700; letter-spacing:-.03em; margin-top:2px;
              font-variant-numeric:tabular-nums}
  .score .num small{font-size:13px; font-weight:500; color:var(--faint)}
  .meter{height:5px; border-radius:99px; background:var(--surface2); margin-top:8px; overflow:hidden}
  .meter i{display:block; height:100%; background:var(--accent); border-radius:99px}

  .syncbar{
    display:flex; align-items:center; gap:9px; flex-wrap:wrap;
    background:var(--surface); border:1px solid var(--border);
    border-radius:12px; padding:10px 13px; margin-top:18px;
    font-size:12.5px; color:var(--dim);
  }
  .syncbar .dot{width:7px; height:7px; border-radius:50%; background:var(--border2); flex:none}
  .syncbar.good .dot{background:var(--accent)}
  .syncbar.bad{border-color:var(--flag); color:var(--flag)}
  .syncbar.bad .dot{background:var(--flag)}
  .syncbar > button{
    margin-left:auto; font:inherit; font-size:12px; font-weight:600; cursor:pointer;
    background:transparent; color:var(--accent); border:1px solid var(--accent-line);
    border-radius:9px; padding:5px 11px;
  }
  .syncpanel{flex-basis:100%; border-top:1px solid var(--border); padding-top:11px; margin-top:2px}
  .syncpanel p{font-size:12px; color:var(--dim); margin-bottom:8px; line-height:1.5}
  .syncpanel p b{color:var(--text); font-weight:600}
  .syncpanel input{
    width:100%; font:inherit; font-size:12.5px; padding:8px 10px;
    background:var(--bg); color:var(--text);
    border:1px solid var(--border2); border-radius:9px;
    font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  }
  .syncacts{display:flex; align-items:center; gap:8px; margin-top:9px; flex-wrap:wrap}
  .syncacts button{
    font:inherit; font-size:12px; font-weight:600; cursor:pointer;
    background:var(--accent); color:var(--accent-ink); border:0;
    border-radius:9px; padding:7px 13px;
  }
  .syncacts button.ghost{background:transparent; color:var(--flag);
                          border:1px solid var(--border2)}
  .syncacts a{font-size:12px; color:var(--dim); margin-left:auto}

  .onlys{display:flex; gap:6px; margin-top:16px; flex-wrap:wrap}
  .chip{
    font:inherit; font-size:12.5px; font-weight:600; cursor:pointer;
    border:1px solid var(--border); background:var(--surface); color:var(--dim);
    border-radius:999px; padding:6px 12px;
  }
  .chip[aria-pressed="true"]{background:var(--accent); border-color:var(--accent);
                              color:var(--accent-ink)}
  .chip .tally{margin-left:6px; opacity:.6; font-weight:500;
               font-variant-numeric:tabular-nums}

  .courses{display:flex; flex-direction:column; gap:8px; margin-top:14px}
  .course{
    display:flex; align-items:center; gap:12px; min-width:0;
    background:var(--surface); border:1px solid var(--border);
    border-radius:12px; padding:11px 13px;
  }
  .course.done{border-color:var(--accent-line); background:var(--accent-bg)}
  .course .info{flex:1; min-width:0}
  .course .nm{font-size:13.5px; font-weight:600; letter-spacing:-.01em;
              overflow-wrap:anywhere}
  .course .sub2{font-size:11px; color:var(--faint); margin-top:1px;
                font-variant-numeric:tabular-nums}
  .ticks{display:flex; gap:6px; flex:none}
  .tick{
    font:inherit; font-size:12px; font-weight:600; cursor:pointer;
    display:inline-flex; align-items:center; gap:5px; white-space:nowrap;
    border:1px solid var(--border2); background:transparent; color:var(--dim);
    border-radius:999px; padding:5px 10px 5px 7px;
    transition:background .12s ease, border-color .12s ease, color .12s ease;
  }
  .tick .box{
    width:14px; height:14px; border-radius:4px; flex:none;
    border:1.5px solid currentColor; display:grid; place-items:center;
  }
  .tick .box svg{width:9px; height:9px; stroke:currentColor; stroke-width:3;
                 fill:none; stroke-linecap:round; stroke-linejoin:round; opacity:0}
  .tick[aria-pressed="true"]{background:var(--accent); border-color:var(--accent);
                             color:var(--accent-ink)}
  .tick[aria-pressed="true"] .box{background:transparent}
  .tick[aria-pressed="true"] .box svg{opacity:1}

  .unsaved{
    display:flex; align-items:center; gap:10px; flex-wrap:wrap;
    background:var(--sand-bg); border:1px solid var(--sand-line);
    border-radius:12px; padding:11px 13px; margin-top:16px;
    font-size:12px; color:var(--sand);
  }
  .unsaved b{color:var(--text); font-weight:600}
  .unsaved button{
    font:inherit; font-size:12px; font-weight:600; cursor:pointer; margin-left:auto;
    background:var(--sand); color:var(--bg); border:0; border-radius:9px; padding:6px 12px;
  }
  .yaml{
    width:100%; margin-top:10px; font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
    font-size:11.5px; line-height:1.5; color:var(--text);
    background:var(--surface); border:1px solid var(--border);
    border-radius:9px; padding:10px; resize:vertical; min-height:96px;
  }

  /* ---- filters ---- */
  .filters{
    position:sticky; top:0; z-index:20;
    background:var(--bg); border-bottom:1px solid var(--border);
    padding:10px 0 12px;
  }
  @supports (backdrop-filter:blur(1px)){
    .filters{background:color-mix(in srgb, var(--bg) 84%, transparent); backdrop-filter:blur(12px)}
  }
  .days{display:flex; gap:7px; overflow-x:auto; scrollbar-width:none; padding-bottom:1px}
  .days::-webkit-scrollbar{display:none}
  .day{
    flex:none; min-width:62px; font:inherit; cursor:pointer; text-align:center;
    border:1px solid var(--border); background:var(--surface); color:var(--text);
    border-radius:11px; padding:6px 10px 7px; line-height:1.2;
    transition:background .12s ease, border-color .12s ease, color .12s ease;
  }
  .day .dw{font-size:10px; letter-spacing:.09em; text-transform:uppercase; color:var(--dim)}
  .day .dn{font-size:15px; font-weight:650; letter-spacing:-.02em; margin-top:1px;
           font-variant-numeric:tabular-nums}
  .day .dc{font-size:10px; color:var(--faint); font-variant-numeric:tabular-nums}
  .day[aria-pressed="true"]{background:var(--accent); border-color:var(--accent); color:var(--accent-ink)}
  .day[aria-pressed="true"] .dw,
  .day[aria-pressed="true"] .dc{color:var(--accent-ink); opacity:.7}
  .day.zero{opacity:.42}

  .ftoggle{
    display:none; margin-top:10px; width:100%; font:inherit; font-size:12.5px; font-weight:600;
    border:1px solid var(--border); background:var(--surface); color:var(--text);
    border-radius:10px; padding:8px 12px; cursor:pointer;
    align-items:center; justify-content:center; gap:7px;
  }
  .ftoggle .badge{
    background:var(--accent); color:var(--accent-ink); border-radius:999px;
    font-size:10.5px; font-weight:700; padding:1px 6px; font-variant-numeric:tabular-nums;
  }
  .row{display:flex; gap:20px; align-items:flex-end; margin-top:12px; flex-wrap:wrap}
  @media (max-width:899px){
    .ftoggle{display:flex}
    .row{display:none; flex-direction:column; align-items:stretch; gap:15px; margin-top:14px}
    .row.open{display:flex}
  }
  .ctl{display:flex; flex-direction:column; gap:7px; min-width:0}
  .ctl.grow{flex:1; min-width:180px}
  .ctl > span{
    font-size:10px; letter-spacing:.11em; text-transform:uppercase; color:var(--faint);
    display:flex; justify-content:space-between; align-items:baseline; gap:8px;
  }
  .ctl > span b{
    color:var(--text); font-weight:600; font-size:12px;
    letter-spacing:0; text-transform:none; font-variant-numeric:tabular-nums;
  }

  .seg{display:flex; gap:2px; padding:2px; border-radius:10px;
       background:var(--surface2); border:1px solid var(--border)}
  .seg button{
    flex:1; font:inherit; font-size:12.5px; font-weight:550; cursor:pointer;
    border:0; background:transparent; color:var(--dim); border-radius:8px;
    padding:5px 9px; white-space:nowrap;
  }
  .seg button[aria-pressed="true"]{background:var(--raise); color:var(--text);
                                   box-shadow:0 1px 2px rgba(0,0,0,.09)}

  .slider{position:relative; height:20px}
  .slider .trk{position:absolute; inset:8px 0 auto; height:4px; border-radius:99px; background:var(--border2)}
  .slider .fill{position:absolute; top:8px; height:4px; border-radius:99px; background:var(--accent)}
  .slider input{
    position:absolute; left:0; top:0; width:100%; height:20px; margin:0;
    -webkit-appearance:none; appearance:none; background:none; pointer-events:none;
  }
  .slider input::-webkit-slider-runnable-track{height:4px; background:transparent}
  .slider input::-webkit-slider-thumb{
    -webkit-appearance:none; pointer-events:auto; cursor:pointer;
    width:16px; height:16px; margin-top:-6px; border-radius:50%;
    background:var(--raise); border:2px solid var(--accent);
    box-shadow:0 1px 3px rgba(0,0,0,.25);
  }
  .slider input::-moz-range-track{height:4px; background:transparent}
  .slider input::-moz-range-thumb{
    pointer-events:auto; cursor:pointer;
    width:14px; height:14px; border-radius:50%;
    background:var(--raise); border:2px solid var(--accent);
  }

  /* ---- results ---- */
  .stats{display:flex; align-items:baseline; gap:8px; flex-wrap:wrap;
         margin:22px 0 2px; font-size:12.5px; color:var(--dim)}
  .stats b{color:var(--text); font-weight:600; font-variant-numeric:tabular-nums}
  .reset{margin-left:auto; font:inherit; font-size:12px; font-weight:550;
         background:none; border:0; color:var(--accent); cursor:pointer; padding:3px 0}

  /* Both halves were nowrap, which overflowed a narrow phone. The heading may
     wrap; the count is the first thing to go when there is no room. */
  .dayhead{display:flex; align-items:center; gap:12px; margin:26px 0 12px}
  .dayhead h2{font-size:14px; font-weight:650; letter-spacing:-.015em; min-width:0}
  .dayhead .ln{flex:1; min-width:12px; height:1px; background:var(--border)}
  .dayhead .cnt{font-size:11px; color:var(--faint); font-variant-numeric:tabular-nums;
                white-space:nowrap; flex:none}
  @media (max-width:400px){ .dayhead .cnt{display:none} }

  /* minmax(0,…) not 1fr: a bare 1fr track is floored at the item's min-content,
     so one long fee label or club name would push the card wider than the
     phone and scroll the whole page sideways. */
  .grid{display:grid; gap:12px; grid-template-columns:minmax(0,1fr)}
  @media (min-width:680px){ .grid{grid-template-columns:repeat(2,minmax(0,1fr))} }
  @media (min-width:1040px){ .grid{grid-template-columns:repeat(3,minmax(0,1fr))} }

  .card{
    background:var(--surface); border:1px solid var(--border); border-radius:var(--radius);
    padding:13px 14px 12px; box-shadow:var(--shadow);
    display:flex; flex-direction:column; gap:10px;
    min-width:0; overflow:hidden;
    transition:border-color .15s ease, box-shadow .15s ease;
  }
  @media (hover:hover){ .card:hover{border-color:var(--accent-line)} }
  .chead{display:flex; align-items:flex-start; justify-content:space-between; gap:10px}
  /* min-width:0 so the nowrap fee line can ellipsis instead of widening the card */
  .cinfo{min-width:0}
  .cname{display:block; font-size:14.5px; font-weight:650; letter-spacing:-.015em;
         line-height:1.25; color:inherit; text-decoration:none; overflow-wrap:anywhere}
  .cname:hover{color:var(--accent)}
  .cmeta{font-size:11.5px; color:var(--dim); margin-top:2px; font-variant-numeric:tabular-nums}
  .fees{font-size:10.5px; color:var(--faint); margin-top:2px;
        overflow:hidden; text-overflow:ellipsis; white-space:nowrap}
  .drive{
    flex:none; font-size:11px; font-weight:600; white-space:nowrap;
    color:var(--dim); background:var(--surface2); border:1px solid var(--border);
    border-radius:999px; padding:3px 8px; font-variant-numeric:tabular-nums;
  }
  .drive.near{color:var(--accent); background:var(--accent-bg); border-color:var(--accent-line)}

  /* The timeline is the fairway: mown stripes across the day, warm at dawn,
     cool at dusk, with each open slot standing up out of the grass. */
  .tl{position:relative; height:30px; border-radius:8px; overflow:hidden;
      background:var(--surface2); border:1px solid var(--border)}
  .tl::before{
    content:""; position:absolute; inset:0;
    background:
      linear-gradient(90deg, var(--dawn), transparent 34%, transparent 62%, var(--dusk)),
      repeating-linear-gradient(90deg, var(--mow) 0 22px, transparent 22px 44px);
  }
  .tl .hr{position:absolute; top:0; bottom:0; width:1px; background:var(--border)}
  .tl .m{position:absolute; top:5px; bottom:5px; width:3px; border-radius:2px;
         background:var(--accent); transform:translateX(-1.5px)}
  .tl .m.pin{background:var(--flag); top:3px; box-shadow:0 0 0 2px var(--surface2)}
  .axis{position:relative; height:11px; margin-top:3px; overflow:hidden}
  .axis span{position:absolute; top:0; font-size:9.5px; color:var(--faint);
             transform:translateX(-50%); white-space:nowrap; letter-spacing:.03em}

  .times{display:flex; flex-wrap:wrap; gap:5px}
  .t{
    transition:border-color .12s ease, color .12s ease, background .12s ease;
    font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    font-size:11.5px; text-decoration:none; color:var(--text);
    background:var(--surface2); border:1px solid var(--border);
    border-radius:7px; padding:4px 7px; white-space:nowrap; font-variant-numeric:tabular-nums;
  }
  .t:hover{border-color:var(--accent); color:var(--accent); background:var(--accent-bg)}
  .t.hot{background:var(--accent); border-color:var(--accent); color:var(--accent-ink); font-weight:600}
  .t.hot:hover{background:var(--accent); color:var(--accent-ink); opacity:.88}
  .more{
    font:inherit; font-size:11.5px; font-weight:550; cursor:pointer;
    color:var(--sand); background:var(--sand-bg);
    border:1px solid var(--sand-line); border-radius:7px; padding:4px 8px;
  }
  .more:hover{border-color:var(--sand)}

  /* Public clubs with no scrapable sheet. Deliberately quieter than a real
     result — this is "we can't see inside", not "a slot is free". */
  .ring .card{background:transparent; border-style:dashed; box-shadow:none}
  .ring .cmeta{margin-top:3px}
  .acts{display:flex; flex-wrap:wrap; gap:6px}
  .call{
    display:inline-flex; align-items:center; gap:6px;
    font:inherit; font-size:12.5px; font-weight:600; text-decoration:none;
    background:var(--accent); color:var(--accent-ink);
    border:1px solid var(--accent); border-radius:9px; padding:6px 11px;
    font-variant-numeric:tabular-nums;
  }
  .call:hover{opacity:.88}
  .call svg{width:13px; height:13px; fill:none; stroke:currentColor;
            stroke-width:2; stroke-linecap:round; stroke-linejoin:round}
  .online{
    display:inline-flex; align-items:center;
    font:inherit; font-size:12.5px; font-weight:550; text-decoration:none;
    color:var(--sand); background:var(--sand-bg);
    border:1px solid var(--sand-line); border-radius:9px; padding:6px 11px;
  }
  .online:hover{border-color:var(--sand)}

  .empty{
    text-align:center; padding:60px 20px; margin-top:22px;
    border:1px dashed var(--border2); border-radius:var(--radius);
  }
  .empty h3{font-size:16px; font-weight:650; letter-spacing:-.02em; margin-bottom:6px}
  .empty p{color:var(--dim); font-size:13px; max-width:330px; margin:0 auto 16px}
  .btn{
    font:inherit; font-size:13px; font-weight:600; cursor:pointer;
    background:var(--accent); color:var(--accent-ink); border:0;
    border-radius:10px; padding:9px 16px;
  }

  footer{
    margin-top:40px; padding-top:16px; border-top:1px solid var(--border);
    font-size:11px; color:var(--faint);
    display:flex; justify-content:space-between; gap:12px; flex-wrap:wrap;
  }

  /* A club that errors just disappears from the list, which is how The Glades
     stayed broken for weeks. Say so instead. */
  .failed{
    flex-basis:100%; order:-1; margin-bottom:12px;
    border:1px solid var(--flag); border-radius:11px;
    padding:10px 13px; color:var(--flag); font-size:12px;
  }
  .failed b{font-weight:650}
  .failed ul{list-style:none; margin-top:5px; display:grid; gap:2px}
  .failed li{font-size:11.5px; opacity:.85}
</style>
</head><body>

<header class="bar"><div class="inner">
  <div class="brand">
    <div class="mark" aria-hidden="true"><svg class="logo" viewBox="0 0 64 64">
      <path class="turfline" d="M10 56.5H54"/>
      <path class="shaft" d="M18 10 28.5 38"/>
      <path class="head" d="M29.5 36 33 46.5 14 52.5 10.5 43Z"/>
      <path class="tee" d="M40 37H54L50.5 41H43.5Z M45.2 41H48.8L48 53H46Z"/>
      <circle class="ball" cx="47" cy="29.5" r="6.8"/>
    </svg></div>
    <div>
      <h1><span class="sn">S&amp;N</span> Go Golfing</h1>
      <div class="sub" id="sub">__NCLUB__ clubs within __DRIVE__ min</div>
    </div>
  </div>
  <button class="icon" id="theme" title="Toggle theme" aria-label="Toggle theme">
    <svg class="i-moon" viewBox="0 0 24 24"><path d="M20 14.5A8.5 8.5 0 1 1 9.5 4a7 7 0 0 0 10.5 10.5Z"/></svg>
    <svg class="i-sun" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4.2"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>
  </button>
</div></header>
<div class="turf"></div>

<div class="inner"><div class="tabs" id="tabs"></div></div>

<section class="filters" id="filterbar"><div class="inner">
  <div class="days" id="days"></div>

  <button class="ftoggle" id="ftoggle" aria-expanded="false">
    <span>Filters</span><span class="badge" id="fcount" hidden></span>
  </button>

  <div class="row" id="row">
    <div class="ctl">
      <span>Group size</span>
      <div class="seg" id="players"></div>
    </div>
    <div class="ctl grow">
      <span>Tee window <b id="tLabel"></b></span>
      <div class="slider" id="tSlider">
        <div class="trk"></div><div class="fill" id="tFill"></div>
        <input type="range" id="tMin"><input type="range" id="tMax">
      </div>
    </div>
    <div class="ctl grow">
      <span>Max drive <b id="dLabel"></b></span>
      <div class="slider" id="dSlider">
        <div class="trk"></div><div class="fill" id="dFill"></div>
        <input type="range" id="drive">
      </div>
    </div>
    <div class="ctl">
      <span>Sort by</span>
      <div class="seg" id="sort"></div>
    </div>
  </div>
</div></section>

<main class="inner">
  <div id="timesview">
    <div class="stats" id="stats"></div>
    <div id="list"></div>
    <div id="ring"></div>
  </div>
  <div id="coursesview" hidden></div>
  <div id="roundsview" hidden></div>
  <footer>
    <div class="failed" id="failed" hidden></div>
    <span>Public tee sheets · tap a time to book</span>
    <span id="osmattr" hidden>Hole maps © OpenStreetMap contributors (ODbL)</span>
    <span id="built"></span>
  </footer>
</main>

<script>
const DATA = __DATA__;
const CONTACTS = __CONTACTS__;
const COURSES = __COURSES__;
const PROBLEMS = __PROBLEMS__;
const ROUNDS = __ROUNDS__;
const CARDS = __CARDS__;
const GOLFERS = __GOLFERS__;
const HOLES = __HOLES__;
const REPO = "__REPO__";
const BUILT = "__BUILT__";

/* ---------- helpers ---------- */
const ENT = {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'};
const esc = s => String(s).replace(/[&<>"']/g, c => ENT[c]);
const mins = t => (+t.slice(0,2)) * 60 + (+t.slice(3,5));
const pad2 = n => (n < 10 ? '0' : '') + n;

function clock(v){
  const h = Math.floor(v / 60), m = v % 60;
  return ((h % 12) || 12) + ':' + pad2(m) + (h < 12 ? 'am' : 'pm');
}
function clockShort(v){
  const h = Math.floor(v / 60), m = v % 60;
  return ((h % 12) || 12) + (m ? ':' + pad2(m) : '') + (h < 12 ? 'am' : 'pm');
}
function dayDiff(iso){
  const d = new Date(iso + 'T00:00:00');
  const t = new Date(); t.setHours(0, 0, 0, 0);
  return Math.round((d - t) / 86400000);
}
function ago(iso){
  const t = new Date(iso);
  if (isNaN(t)) return '';
  const s = (Date.now() - t) / 1000;
  if (s < 90) return 'updated just now';
  if (s < 5400) return 'updated ' + Math.round(s / 60) + ' min ago';
  if (s < 86400) return 'updated ' + Math.round(s / 3600) + ' hr ago';
  return 'updated ' + t.toLocaleDateString('en-AU', { day:'numeric', month:'short' });
}
const groupBy = (rows, fn) => rows.reduce((acc, r) => {
  (acc[fn(r)] = acc[fn(r)] || []).push(r); return acc;
}, {});

/* ---------- bounds derived from the data ---------- */
const ALL_DAYS = [...new Set(DATA.map(r => r.day))].sort();
const T_LO = DATA.length ? Math.floor(Math.min(...DATA.map(r => mins(r.time))) / 30) * 30 : 360;
const T_HI = DATA.length ? Math.ceil(Math.max(...DATA.map(r => mins(r.time))) / 30) * 30 : 1080;
const D_LO = DATA.length ? Math.max(5, Math.floor(Math.min(...DATA.map(r => r.drive)) / 5) * 5) : 5;
const D_HI = DATA.length ? Math.ceil(Math.max(...DATA.map(r => r.drive)) / 5) * 5 : 60;
const SPAN = Math.max(1, T_HI - T_LO);
const PLAYERS = [1, 2, 3, 4];
const SORTS = [['time','Earliest'], ['drive','Closest'], ['count','Most open']];
const CHIP_LIMIT = 8;

const DEFAULTS = { day:'all', players:4, tmin:T_LO, tmax:T_HI, drive:D_HI, sort:'time',
                   view:'times', only:'all' };
const state = Object.assign({}, DEFAULTS, restore());
const expanded = new Set();

function restore(){
  try {
    const s = JSON.parse(localStorage.getItem('tee.filters') || '{}');
    const out = {};
    if (s.day === 'all' || ALL_DAYS.indexOf(s.day) > -1) out.day = s.day;
    if (PLAYERS.indexOf(s.players) > -1) out.players = s.players;
    if (SORTS.some(x => x[0] === s.sort)) out.sort = s.sort;
    if (['times','courses','rounds'].indexOf(s.view) > -1) out.view = s.view;
    if (['all','todo','done'].indexOf(s.only) > -1) out.only = s.only;
    if (typeof s.tmin === 'number') out.tmin = Math.min(Math.max(s.tmin, T_LO), T_HI);
    if (typeof s.tmax === 'number') out.tmax = Math.min(Math.max(s.tmax, T_LO), T_HI);
    if (out.tmin > out.tmax) { delete out.tmin; delete out.tmax; }
    if (typeof s.drive === 'number') out.drive = Math.min(Math.max(s.drive, D_LO), D_HI);
    return out;
  } catch (e) { return {}; }
}
const save = () => { try { localStorage.setItem('tee.filters', JSON.stringify(state)); } catch (e) {} };

/* ---------- filtering ---------- */
function matches(r, ignoreDay){
  if (!ignoreDay && state.day !== 'all' && r.day !== state.day) return false;
  if (r.free < state.players) return false;
  if (r.drive > state.drive) return false;
  const m = mins(r.time);
  return m >= state.tmin && m <= state.tmax;
}
const activeCount = () =>
  (state.day !== DEFAULTS.day ? 1 : 0) + (state.players !== DEFAULTS.players ? 1 : 0) +
  (state.tmin !== DEFAULTS.tmin || state.tmax !== DEFAULTS.tmax ? 1 : 0) +
  (state.drive !== DEFAULTS.drive ? 1 : 0) + (state.sort !== DEFAULTS.sort ? 1 : 0);

/* ---------- static bits of the timeline ---------- */
const pct = v => ((v - T_LO) / SPAN) * 100;
const TICKS = [];
for (let v = Math.ceil(T_LO / 180) * 180; v < T_HI; v += 180) TICKS.push(v);
const GRID = TICKS.map(v => '<i class="hr" style="left:' + pct(v).toFixed(2) + '%"></i>').join('');
const AXIS = TICKS.filter(v => pct(v) > 7 && pct(v) < 93)
  .map(v => '<span style="left:' + pct(v).toFixed(2) + '%">' + clockShort(v) + '</span>').join('');

/* ---------- controls ---------- */
function buildSeg(el, items, get, set){
  el.innerHTML = '';
  items.forEach(([val, label]) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = label;
    b.setAttribute('aria-pressed', String(get() === val));
    b.onclick = () => { set(val); render(); };
    el.appendChild(b);
  });
}

const $ = id => document.getElementById(id);
const tMin = $('tMin'), tMax = $('tMax'), drive = $('drive');

[tMin, tMax].forEach(el => { el.min = T_LO; el.max = T_HI; el.step = 15; });
drive.min = D_LO; drive.max = D_HI; drive.step = 5;

tMin.oninput = () => {
  state.tmin = Math.min(+tMin.value, state.tmax - 15);
  tMin.value = state.tmin; render();
};
tMax.oninput = () => {
  state.tmax = Math.max(+tMax.value, state.tmin + 15);
  tMax.value = state.tmax; render();
};
drive.oninput = () => { state.drive = +drive.value; render(); };

$('theme').onclick = () => {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem('tee.theme', next); } catch (e) {}
};

$('ftoggle').onclick = function(){
  const open = $('row').classList.toggle('open');
  this.setAttribute('aria-expanded', String(open));
};

$('list').onclick = e => {
  const b = e.target.closest('.more');
  if (!b) return;
  const k = b.dataset.k;
  expanded.has(k) ? expanded.delete(k) : expanded.add(k);
  render();
};

$('coursesview').onclick = e => {
  const t = e.target.closest('.tick');
  if (t) { togglePlayed(t.dataset.club); return; }
  const c = e.target.closest('.chip');
  if (c) { state.only = c.dataset.only; render(); }
};

document.addEventListener('keydown', e => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (state.view !== 'times') return;
  if (/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
  if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
  const seq = ['all'].concat(ALL_DAYS);
  const i = seq.indexOf(state.day);
  const j = i + (e.key === 'ArrowRight' ? 1 : -1);
  if (j < 0 || j >= seq.length) return;
  e.preventDefault();
  state.day = seq[j];
  render();
});

function reset(){
  Object.assign(state, DEFAULTS);
  expanded.clear();
  render();
}

/* ---------- render ---------- */
function syncControls(){
  buildSeg($('players'), PLAYERS.map(n => [n, String(n)]),
    () => state.players, v => { state.players = v; });
  buildSeg($('sort'), SORTS, () => state.sort, v => { state.sort = v; });

  tMin.value = state.tmin; tMax.value = state.tmax; drive.value = state.drive;
  $('tFill').style.left = pct(state.tmin) + '%';
  $('tFill').style.width = (pct(state.tmax) - pct(state.tmin)) + '%';
  $('dFill').style.width = ((state.drive - D_LO) / Math.max(1, D_HI - D_LO)) * 100 + '%';
  $('tLabel').textContent = (state.tmin === T_LO && state.tmax === T_HI)
    ? 'any' : clockShort(state.tmin) + ' – ' + clockShort(state.tmax);
  $('dLabel').textContent = state.drive >= D_HI ? 'any' : 'under ' + state.drive + ' min';

  const n = activeCount();
  const badge = $('fcount');
  badge.hidden = !n;
  badge.textContent = n;
}

function renderDays(){
  const el = $('days');
  el.innerHTML = '';
  const total = DATA.filter(r => matches(r, true)).length;

  const pill = (value, top, big, count) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'day' + (count ? '' : ' zero');
    b.setAttribute('aria-pressed', String(state.day === value));
    b.innerHTML = '<div class="dw">' + top + '</div><div class="dn">' + big +
                  '</div><div class="dc">' + count + '</div>';
    b.onclick = () => { state.day = value; render(); };
    return b;
  };

  el.appendChild(pill('all', 'Next', ALL_DAYS.length + 'd', total));
  ALL_DAYS.forEach(d => {
    const diff = dayDiff(d);
    const date = new Date(d + 'T00:00:00');
    const top = diff === 0 ? 'Today' : diff === 1 ? 'Tmrw'
      : date.toLocaleDateString('en-AU', { weekday:'short' });
    const count = DATA.filter(r => r.day === d && matches(r, true)).length;
    el.appendChild(pill(d, top, String(date.getDate()), count));
  });
}

function card(day, club, g){
  g.sort((a, b) => a.time.localeCompare(b.time));
  const key = day + '|' + club;
  const open = expanded.has(key);
  const shown = open ? g : g.slice(0, CHIP_LIMIT);
  const fees = [...new Set(g.flatMap(r => r.fees))];
  const seats = Math.max(...g.map(r => r.free));

  // Height of the grass = how many spots are free; the pin marks the earliest.
  const marks = g.map((r, i) => {
    const o = r.free >= 4 ? 1 : r.free === 3 ? .7 : r.free === 2 ? .48 : .3;
    return '<i class="m' + (i === 0 ? ' pin' : '') + '" style="left:' +
      pct(mins(r.time)).toFixed(2) + '%;opacity:' + (i === 0 ? 1 : o) + '"></i>';
  }).join('');

  const chips = shown.map((r, i) =>
    '<a class="t' + (i === 0 ? ' hot' : '') + '" href="' + esc(r.url) + '" target="_blank" rel="noopener"' +
    ' title="' + r.free + (r.free === 1 ? ' spot' : ' spots') + ' open">' + clock(mins(r.time)) + '</a>'
  ).join('');

  const more = g.length > CHIP_LIMIT
    ? '<button class="more" data-k="' + esc(key) + '">' +
      (open ? 'Show less' : '+' + (g.length - CHIP_LIMIT) + ' more') + '</button>'
    : '';

  return '<article class="card">' +
    '<div class="chead"><div class="cinfo">' +
      '<a class="cname" href="' + esc(g[0].url) + '" target="_blank" rel="noopener">' + esc(club) + '</a>' +
      '<div class="cmeta">' + g.length + (g.length === 1 ? ' time' : ' times') +
        ' · from ' + clock(mins(g[0].time)) + ' · up to ' + seats + ' players</div>' +
      '<div class="fees">' + esc(fees.slice(0, 2).join(' · ')) +
        (fees.length > 2 ? ' +' + (fees.length - 2) : '') + '</div>' +
    '</div>' +
    '<span class="drive' + (g[0].drive <= 20 ? ' near' : '') + '">' + g[0].drive + ' min</span></div>' +
    '<div><div class="tl">' + GRID + marks + '</div><div class="axis">' + AXIS + '</div></div>' +
    '<div class="times">' + chips + more + '</div>' +
  '</article>';
}

function renderList(){
  const rows = DATA.filter(r => matches(r));
  const list = $('list');
  const stats = $('stats');

  if (!rows.length){
    stats.innerHTML = '';
    list.innerHTML = '<div class="empty"><h3>Nothing open</h3>' +
      '<p>No tee times match these filters. Widen the window, allow a longer drive, ' +
      'or drop the group size.</p><button class="btn" id="clr">Clear filters</button></div>';
    $('clr').onclick = reset;
    return;
  }

  const clubs = new Set(rows.map(r => r.club)).size;
  const earliest = rows.reduce((a, r) => mins(r.time) < mins(a.time) ? r : a).time;
  stats.innerHTML = '<span><b>' + rows.length.toLocaleString() + '</b> tee times · <b>' +
    clubs + '</b> clubs · earliest <b>' + clock(mins(earliest)) + '</b></span>' +
    (activeCount() ? '<button class="reset" id="rst">Clear filters</button>' : '');
  if (activeCount()) $('rst').onclick = reset;

  const byDay = groupBy(rows, r => r.day);
  let html = '';

  Object.keys(byDay).sort().forEach(day => {
    const groups = Object.entries(groupBy(byDay[day], r => r.club));
    groups.sort((a, b) => {
      if (state.sort === 'drive') return a[1][0].drive - b[1][0].drive ||
        a[1][0].time.localeCompare(b[1][0].time);
      if (state.sort === 'count') return b[1].length - a[1].length ||
        a[1][0].time.localeCompare(b[1][0].time);
      const at = Math.min(...a[1].map(r => mins(r.time)));
      const bt = Math.min(...b[1].map(r => mins(r.time)));
      return at - bt || a[1][0].drive - b[1][0].drive;
    });

    const diff = dayDiff(day);
    const nice = new Date(day + 'T00:00:00')
      .toLocaleDateString('en-AU', { weekday:'long', day:'numeric', month:'long' });
    const label = diff === 0 ? 'Today · ' + nice : diff === 1 ? 'Tomorrow · ' + nice : nice;

    html += '<div class="dayhead"><h2>' + label + '</h2><div class="ln"></div>' +
            '<span class="cnt">' + byDay[day].length + ' across ' + groups.length +
            (groups.length === 1 ? ' club' : ' clubs') + '</span></div>' +
            '<div class="grid">' + groups.map(([club, g]) => card(day, club, g)).join('') + '</div>';
  });

  list.innerHTML = html;
}

/* ---------- World Handicap System ----------
   Score Differential = (113 / Slope) x (Adjusted Gross - Course Rating).
   PCC is a daily adjustment only a governing body can issue, so it is 0.
   The index is the average of the lowest N differentials of the last 20
   rounds; N and any extra adjustment come from the short-record table. */
const WHS_TABLE = [
  [3,1,-2.0],[4,1,-1.0],[5,1,0],[6,2,-1.0],[7,2,0],[8,2,0],
  [9,3,0],[10,3,0],[11,3,0],[12,4,0],[13,4,0],[14,4,0],
  [15,5,0],[16,5,0],[17,6,0],[18,6,0],[19,7,0],[20,8,0],
];
function whsRule(n){
  if (n < 3) return null;
  if (n >= 20) return { take:8, adjust:0 };
  const row = WHS_TABLE.find(r => r[0] === n);
  return row ? { take:row[1], adjust:row[2] } : null;
}
/* Everyone gets floor(ch/holes); the hardest (ch mod holes) get one more. */
function strokesOnHole(ch, si, holes){
  ch = Math.round(ch);
  if (!si) return 0;
  if (ch >= 0) return Math.floor(ch / holes) + (si <= (ch % holes) ? 1 : 0);
  const g = Math.abs(ch);
  return -(Math.floor(g / holes) + (si > holes - (g % holes) ? 1 : 0));
}
function courseHandicap(index, slope, cr, par){
  if (index === null || slope == null || cr == null || par == null) return null;
  return index * (slope / 113) + (cr - par);
}
/* Each hole caps at net double bogey. No index yet means par + 5, which is
   what WHS says to use for a first card. */
function adjustedGross(strokes, par, si, index, slope, cr){
  const holes = strokes.length;
  const totalPar = par.slice(0, holes).reduce((a,b) => a+b, 0);
  const ch = courseHandicap(index, slope, cr, totalPar);
  let total = 0;
  for (let i = 0; i < holes; i++){
    const s = strokes[i];
    if (s == null) continue;
    const cap = ch === null ? par[i] + 5 : par[i] + 2 + strokesOnHole(ch, si[i], holes);
    total += Math.min(s, cap);
  }
  return total;
}
function differential(ags, cr, slope){
  if (cr == null || slope == null) return null;
  return Math.round(((113 / slope) * (ags - cr)) * 10) / 10;
}
/* Truncated, not rounded: 12.39 is 12.3. */
function handicapIndex(diffs){
  const recent = diffs.slice(-20);
  const rule = whsRule(recent.length);
  if (!rule) return null;
  const best = recent.slice().sort((a,b) => a-b).slice(0, rule.take);
  return Math.trunc((best.reduce((a,b)=>a+b,0) / best.length + rule.adjust) * 10) / 10;
}

/* ---------- rounds ---------- */
let localRounds = [];
try { localRounds = JSON.parse(localStorage.getItem('tee.rounds') || '[]'); } catch (e) { localRounds = []; }
const saveRounds = () => { try { localStorage.setItem('tee.rounds', JSON.stringify(localRounds)); } catch (e) {} };
const allRounds = () => ROUNDS.concat(localRounds);

const cardFor = course => CARDS[course] || CARDS.__default__;
const COURSE_NAMES = COURSES.map(c => c.club);

function roundInfo(r){
  const card = cardFor(r.course);
  const holes = (r.strokes || []).length;
  const par = (card.par || []).slice(0, holes);
  const si = (card.si || []).slice(0, holes);
  const gross = (r.strokes || []).reduce((a,b) => a + (b || 0), 0);
  const totalPar = par.reduce((a,b) => a+b, 0);
  const tee = ((card.tees || {})[r.tee]) || null;
  return { card, holes, par, si, gross, totalPar, toPar: gross - totalPar, tee };
}

/* Work forward in date order: each round is capped using the index you held
   *before* you played it, which is what WHS does. */
function playerRecord(id){
  const rs = allRounds()
    .filter(r => r.player === id && (r.strokes || []).length === 18)
    .sort((a,b) => String(a.date).localeCompare(String(b.date)));
  const diffs = [];
  let index = null;
  const perRound = {};
  rs.forEach(r => {
    const info = roundInfo(r);
    if (!info.tee || info.tee.cr == null || info.tee.slope == null){
      perRound[r.date + r.course] = { counted:false, why:'course not rated' };
      return;
    }
    const ags = adjustedGross(r.strokes, info.par, info.si, index, info.tee.slope, info.tee.cr);
    const d = differential(ags, info.tee.cr, info.tee.slope);
    diffs.push(d);
    index = handicapIndex(diffs);
    perRound[r.date + r.course] = { counted:true, ags, diff:d };
  });
  return { index, counted: diffs.length, perRound,
           played: allRounds().filter(r => r.player === id).length };
}

/* ---------- courses played ----------
   One list for the pair: either you have played it together or you have not.
   played.yaml is the shared record; ticks made here live on this device only
   until they are pasted back and committed, so local state is tracked as a
   diff against the file rather than pretending to sync. */
const BASE = {};
COURSES.forEach(c => { BASE[c.club] = !!c.done; });

let local = {};
try { local = JSON.parse(localStorage.getItem('tee.played') || '{}'); } catch (e) { local = {}; }

const saveLocal = () => { try { localStorage.setItem('tee.played', JSON.stringify(local)); } catch (e) {} };
const hasPlayed = club => (local[club] !== undefined ? local[club] : !!BASE[club]);
const playedCount = () => COURSES.filter(c => hasPlayed(c.club)).length;

function togglePlayed(club){
  const now = !hasPlayed(club);
  if (now === !!BASE[club]) delete local[club]; else local[club] = now;
  saveLocal();
  render();
  if (getToken() && REPO) push();   // optimistic: the tick is already shown
}

const drifted = () => COURSES.map(c => c.club).filter(c => local[c] !== undefined);

/* ---------- optional sync ----------
   With no token the page behaves exactly as before: ticks are this device
   only. Paste a fine-grained GitHub token and ticking commits played.yaml
   straight to the repo instead, so both phones see the same list. The token
   is kept in this browser and sent only to api.github.com. */
const FILE = 'played.yaml';
let sync = { state: REPO ? 'off' : 'unavailable', sha: null, msg: '', at: 0, pulled: false };

const getToken = () => { try { return localStorage.getItem('tee.gh') || ''; } catch (e) { return ''; } };
const setToken = v => {
  try { v ? localStorage.setItem('tee.gh', v) : localStorage.removeItem('tee.gh'); } catch (e) {}
};

function b64encode(str){
  const bytes = new TextEncoder().encode(str);
  let bin = '';
  bytes.forEach(b => { bin += String.fromCharCode(b); });
  return btoa(bin);
}
function b64decode(b64){
  const bin = atob(b64.replace(/[^A-Za-z0-9+/=]/g, ''));
  return new TextDecoder().decode(Uint8Array.from(bin, ch => ch.charCodeAt(0)));
}

function parsePlayedYaml(text){
  const out = [];
  let inList = false;
  text.split(String.fromCharCode(10)).forEach(raw => {
    const line = raw.trim();
    if (!line || line.startsWith('#')) return;
    if (line.startsWith('played:')){ inList = true; return; }
    if (!inList) return;
    if (line.startsWith('- ')){
      const v = line.slice(2).trim();
      out.push(v.startsWith('"') ? JSON.parse(v) : v);
    } else inList = false;
  });
  return out;
}

/* keep the file's comment header, replace only the played: block */
function serializePlayedYaml(original){
  const nl = String.fromCharCode(10);
  const lines = original.split(nl);
  let i = lines.length;
  for (let k = 0; k < lines.length; k++) if (lines[k].startsWith('played:')){ i = k; break; }
  return lines.slice(0, i).concat(playedYaml().split(nl)).join(nl) + nl;
}

async function gh(path, opts){
  const r = await fetch('https://api.github.com/repos/' + REPO + '/contents/' + path, Object.assign({
    headers: {
      'Authorization': 'Bearer ' + getToken(),
      'Accept': 'application/vnd.github+json',
    },
  }, opts || {}));
  if (!r.ok) throw new Error('GitHub ' + r.status + (r.status === 401 ? ' (token rejected)' : ''));
  return r.json();
}

/* Take the committed file as the new baseline. Local ticks are kept on top —
   any that the file now agrees with stop counting as a local change. */
function adoptBase(list){
  const set = {};
  list.forEach(name => { set[name] = true; });
  COURSES.forEach(c => {
    BASE[c.club] = !!set[c.club];
    if (local[c.club] !== undefined && local[c.club] === BASE[c.club]) delete local[c.club];
  });
  saveLocal();
}

/* Adopt and drop local overrides entirely — used after our own write, where
   the file is now authoritative for this device. */
function adopt(list){
  local = {};
  adoptBase(list);
}

/* played.yaml is in a public repo, so the shared list can be read with no
   token at all. Only writing one needs credentials. */
async function pullPublic(){
  if (!REPO) return;
  try {
    const r = await fetch('https://raw.githubusercontent.com/' + REPO +
                          '/main/' + FILE + '?t=' + Date.now(), { cache: 'no-store' });
    if (!r.ok) return;
    adoptBase(parsePlayedYaml(await r.text()));
    sync.pulled = true;
    render();
  } catch (e) { /* offline, or the file is not there yet — keep what we have */ }
}

async function pull(){
  if (!getToken() || !REPO) return;
  sync.state = 'busy'; renderSyncOnly();
  try {
    const data = await gh(FILE);
    sync.sha = data.sha;
    adopt(parsePlayedYaml(b64decode(data.content || '')));
    sync.state = 'ok'; sync.at = Date.now(); sync.msg = '';
  } catch (e) {
    sync.state = 'error'; sync.msg = e.message;
  }
  render();
}

async function push(){
  if (!getToken() || !REPO) return;
  sync.state = 'busy'; sync.msg = ''; renderSyncOnly();
  try {
    const current = await gh(FILE);                   // re-read for the latest sha
    const merged = serializePlayedYaml(b64decode(current.content || ''));
    const saved = await gh(FILE, {
      method: 'PUT',
      headers: {
        'Authorization': 'Bearer ' + getToken(),
        'Accept': 'application/vnd.github+json',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        message: 'played: ' + playedCount() + ' of ' + COURSES.length,
        content: b64encode(merged),
        sha: current.sha,
      }),
    });
    sync.sha = saved.content && saved.content.sha;
    adopt(parsePlayedYaml(merged));
    sync.state = 'ok'; sync.at = Date.now();
  } catch (e) {
    sync.state = 'error'; sync.msg = e.message;   // the tick survives locally
  }
  render();
}

function playedYaml(){
  const done = COURSES.filter(c => hasPlayed(c.club)).map(c => c.club);
  if (!done.length) return 'played: []';
  const nl = String.fromCharCode(10);
  return 'played:' + nl + done.map(club => {
    const safe = /^[A-Za-z][A-Za-z0-9 ()&.-]*$/.test(club) ? club : JSON.stringify(club);
    return '  - ' + safe;
  }).join(nl);
}

function renderCourses(){
  const el = $('coursesview');
  const tick = '<svg viewBox="0 0 24 24"><path d="M4 12.5 9.5 18 20 6.5"/></svg>';
  const done = playedCount(), total = COURSES.length, left = total - done;
  const pctDone = total ? Math.round((done / total) * 100) : 0;

  const summary =
    '<div class="score"><div class="who">Played</div>' +
      '<div class="num">' + done + ' <small>of ' + total + '</small></div>' +
      '<div class="meter"><i style="width:' + pctDone + '%"></i></div></div>' +
    '<div class="score"><div class="who">Still to play</div>' +
      '<div class="num">' + left + ' <small>course' + (left === 1 ? '' : 's') + ' left</small></div>' +
      '<div class="meter"><i style="width:' + (100 - pctDone) + '%"></i></div></div>';

  const shown = COURSES.filter(c =>
    state.only === 'all' || (state.only === 'done' ? hasPlayed(c.club) : !hasPlayed(c.club)));

  const chips = [['all', 'All', total], ['todo', 'To play', left], ['done', 'Played', done]]
    .map(([k, label, n]) => '<button class="chip" type="button" data-only="' + k + '"' +
      ' aria-pressed="' + (state.only === k) + '">' + label +
      '<span class="tally">' + n + '</span></button>').join('');

  const rows = shown.map(c => {
    const on = hasPlayed(c.club);
    const bits = [c.drive + ' min'];
    if (c.note) bits.push(c.note);
    return '<div class="course' + (on ? ' done' : '') + '">' +
      '<div class="info"><div class="nm">' + esc(c.club) + '</div>' +
      '<div class="sub2">' + esc(bits.join(' · ')) + '</div></div>' +
      '<div class="ticks"><button class="tick" type="button" data-club="' + esc(c.club) + '"' +
      ' aria-pressed="' + on + '"><span class="box">' + tick + '</span>' +
      (on ? 'Played' : 'Not yet') + '</button></div></div>';
  }).join('');

  // With sync on, a tick is committed for both of you, so there is nothing to
  // paste. The export block is only the fallback for the unsynced case.
  const changed = drifted();
  const banner = (changed.length && !getToken())
    ? '<div class="unsaved"><span><b>' + changed.length +
      (changed.length === 1 ? ' change' : ' changes') + ' on this device only.</b> ' +
      'Turn on sync above, or paste this into played.yaml and commit.</span>' +
      '<button type="button" id="exportbtn">Copy for played.yaml</button>' +
      '<textarea class="yaml" id="yamlout" readonly>' + esc(playedYaml()) + '</textarea></div>'
    : '';

  el.innerHTML = syncBar() +
                 '<div class="scoreboard">' + summary + '</div>' +
                 '<div class="strip onlys">' + chips + '</div>' +
                 '<div class="courses">' + rows +
                 (shown.length ? '' : '<div class="empty"><h3>Nothing here</h3></div>') +
                 '</div>' + banner;

  wireSync();
  if (changed.length && !getToken()){
    $('exportbtn').onclick = () => {
      const box = $('yamlout');
      box.select();
      try { navigator.clipboard.writeText(box.value); } catch (e) { try { document.execCommand('copy'); } catch (e2) {} }
      $('exportbtn').textContent = 'Copied';
    };
  }
}

function syncBar(){
  if (sync.state === 'unavailable') return '';
  const on = !!getToken();
  const label = sync.state === 'busy' ? 'Saving…'
    : sync.state === 'error' ? ('Sync failed — ' + esc(sync.msg))
    : on ? 'Synced — your ticks save for both of you'
    : sync.pulled ? 'Showing the shared list · your ticks stay on this device'
    : 'Ticks save on this device only';
  const cls = sync.state === 'error' ? ' bad' : (on ? ' good' : '');
  return '<div class="syncbar' + cls + '">' +
    '<span class="dot"></span><span>' + label + '</span>' +
    '<button type="button" id="syncbtn">' + (on ? 'Sync settings' : 'Turn on sync') + '</button>' +
    '<div class="syncpanel" id="syncpanel" hidden>' +
      '<p>Ticking will commit <b>played.yaml</b> to <b>' + esc(REPO) + '</b>, so both phones show the same list.</p>' +
      '<p>Create a <b>fine-grained personal access token</b> on GitHub with ' +
      '<b>Repository access: only ' + esc(REPO) + '</b> and <b>Repository permissions ' +
      'to Contents: Read and write</b>. Paste it below. It is stored in this ' +
      'browser only, is sent only to api.github.com, and you can revoke it any time.</p>' +
      '<input type="password" id="ghtoken" placeholder="github_pat_..." autocomplete="off" ' +
      'spellcheck="false" value="' + (on ? '' : '') + '">' +
      '<div class="syncacts">' +
        '<button type="button" id="ghsave">Save token</button>' +
        (on ? '<button type="button" id="ghclear" class="ghost">Remove token</button>' : '') +
        '<a href="https://github.com/settings/personal-access-tokens/new" target="_blank" rel="noopener">Create a token</a>' +
      '</div>' +
    '</div></div>';
}

function wireSync(){
  const btn = $('syncbtn');
  if (!btn) return;
  btn.onclick = () => { const p = $('syncpanel'); p.hidden = !p.hidden; };
  $('ghsave').onclick = () => {
    const v = $('ghtoken').value.trim();
    if (!v) return;
    setToken(v);
    $('ghtoken').value = '';
    pull();                       // adopt whatever is already committed
  };
  if ($('ghclear')) $('ghclear').onclick = () => {
    setToken('');
    sync.state = 'off'; sync.msg = '';
    render();
  };
}

/* repaint just the status line while a request is in flight */
function renderSyncOnly(){
  const bar = document.querySelector('.syncbar');
  if (!bar) return;
  const open = $('syncpanel') && !$('syncpanel').hidden;
  bar.outerHTML = syncBar();
  wireSync();
  if (open && $('syncpanel')) $('syncpanel').hidden = false;
}

function roundsYaml(){
  const nl = String.fromCharCode(10);
  const all = allRounds();
  if (!all.length) return 'rounds: []';
  const q = s => /^[A-Za-z][A-Za-z0-9 ()&.-]*$/.test(s) ? s : JSON.stringify(s);
  return 'rounds:' + nl + all.map(r =>
    '  - date: ' + r.date + nl +
    '    course: ' + q(r.course) + nl +
    '    player: ' + r.player + nl +
    (r.tee ? '    tee: ' + q(r.tee) + nl : '') +
    '    strokes: [' + r.strokes.join(', ') + ']'
  ).join(nl);
}

function serializeRoundsYaml(original){
  const nl = String.fromCharCode(10);
  const lines = original.split(nl);
  let i = lines.length;
  for (let k = 0; k < lines.length; k++) if (lines[k].startsWith('rounds:')){ i = k; break; }
  return lines.slice(0, i).concat(roundsYaml().split(nl)).join(nl) + nl;
}

/* Same write path as played.yaml: re-read for the current sha, merge, PUT. */
async function pushRounds(){
  if (!getToken() || !REPO) return;
  sync.state = 'busy'; sync.msg = ''; renderSyncOnly();
  try {
    const current = await gh('rounds.yaml');
    const merged = serializeRoundsYaml(b64decode(current.content || ''));
    await gh('rounds.yaml', {
      method: 'PUT',
      headers: { 'Authorization': 'Bearer ' + getToken(),
                 'Accept': 'application/vnd.github+json',
                 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: 'round: ' + allRounds().length + ' recorded',
                             content: b64encode(merged), sha: current.sha }),
    });
    // committed, so it is no longer a local-only round
    ROUNDS.push.apply(ROUNDS, localRounds);
    localRounds = []; saveRounds();
    sync.state = 'ok'; sync.at = Date.now();
  } catch (e) {
    sync.state = 'error'; sync.msg = e.message;   // the round survives locally
  }
  render();
}

/* ---------- course cards ----------
   Course Rating and Slope come off the card in the pro shop. Published
   aggregators disagree with each other, so nothing here is guessed — an
   unrated course simply doesn't count toward a handicap until you type it. */
let localCards = {};
try { localCards = JSON.parse(localStorage.getItem('tee.cards') || '{}'); } catch (e) { localCards = {}; }
const saveCards = () => { try { localStorage.setItem('tee.cards', JSON.stringify(localCards)); } catch (e) {} };
Object.keys(localCards).forEach(k => { CARDS[k] = localCards[k]; });

let editing = null;   // { course, par[18], si[18], tees:[{name,cr,slope}] }

function openEditor(course){
  const c = cardFor(course);
  editing = {
    course,
    par: (c.par || []).slice(0, 18),
    si: (c.si || []).slice(0, 18),
    tees: Object.keys(c.tees || {}).map(n => ({
      name: n, cr: c.tees[n].cr, slope: c.tees[n].slope })),
  };
  if (!editing.tees.length) editing.tees.push({ name:'', cr:'', slope:'' });
}

function siProblems(si){
  const seen = {};
  si.forEach(v => { if (v != null) seen[v] = (seen[v] || 0) + 1; });
  return si.map((v, i) => v == null || v < 1 || v > 18 || seen[v] > 1);
}

/* Par off the hole map, but only when OSM has all 18 and they add up to
   something a golf course would actually be. The stroke index is never in
   there - almost nobody tags it - so that still comes off the paper card.

   These are mappers' tags, not the card: Palm Meadows adds up to 73 when it
   is a par 72, so at least one hole there is wrong. The total is on the
   button for that reason - if it doesn't match the card, don't press it. */
function osmPar(course){
  const map = HOLES[course];
  const holes = holeMap(course);
  if (!holes || holes.length !== 18 || (map && map.loops > 1)) return null;
  const par = holes.map(h => h.par);
  if (par.some(p => !p || p < 3 || p > 6)) return null;
  const tot = par.reduce((a,b) => a+b, 0);
  return (tot >= 68 && tot <= 74) ? par : null;
}

function renderEditor(){
  if (!editing) return '';
  const bad = siProblems(editing.si);
  const row = (key, label, vals) =>
    '<div class="nine"><div class="lbl">' + label + '</div><div class="srow">' +
    vals.map((v, i) =>
      '<div class="h' + (key === 'si' && bad[i] ? ' bad' : '') + '"><span>' + (i+1) + '</span>' +
      '<input type="number" inputmode="numeric" data-k="' + key + '" data-i="' + i + '"' +
      ' value="' + (v == null ? '' : v) + '"></div>').join('') +
    '<div class="h"><span>' + (key === 'par' ? 'TOT' : '') + '</span><span style="padding:5px 0">' +
    (key === 'par' ? vals.reduce((a,b) => a + (b||0), 0) : '') + '</span></div></div>';

  const dupes = bad.some(Boolean);
  return '<div class="sheet">' +
    '<div class="row"><label>Course<select id="e-course">' +
      COURSE_NAMES.map(c => '<option value="' + esc(c) + '"' +
        (c === editing.course ? ' selected' : '') + '>' + esc(c) + '</option>').join('') +
    '</select></label></div>' +
    row('par', 'Par', editing.par) +
    (osmPar(editing.course) ? '<button class="btn ghost" type="button" id="e-osmpar" ' +
      'style="margin-top:7px">Fill par from the hole map (' +
      osmPar(editing.course).reduce((a,b) => a+b, 0) + ')</button>' : '') +
    row('si', 'Stroke index', editing.si) +
    (dupes ? '<div class="warnrow"><b>Stroke index needs 1 to 18, once each.</b> ' +
             'The flagged holes are blank, out of range or repeated.</div>' : '') +
    '<div class="nine"><div class="lbl">Tees &middot; rating and slope are printed on the card</div>' +
    '<div class="tees">' + editing.tees.map((t, i) =>
      '<div class="teerow">' +
        '<input class="nm" placeholder="white" data-t="' + i + '" data-f="name" value="' + esc(t.name || '') + '">' +
        '<input class="num" placeholder="CR 70.1" data-t="' + i + '" data-f="cr" value="' + (t.cr === '' || t.cr == null ? '' : t.cr) + '">' +
        '<input class="num" placeholder="Slope 125" data-t="' + i + '" data-f="slope" value="' + (t.slope === '' || t.slope == null ? '' : t.slope) + '">' +
        '<button type="button" data-drop="' + i + '">Remove</button>' +
      '</div>').join('') + '</div>' +
    '<button class="btn ghost" type="button" id="e-addtee" style="margin-top:7px">Add a tee</button></div>' +
    '<div class="foot">' +
      '<span class="run">' + editing.tees.filter(t => t.name && t.cr && t.slope).length +
      ' rated tee(s)</span>' +
      '<button class="btn ghost" type="button" id="e-cancel">Cancel</button>' +
      '<button class="btn" type="button" id="e-save"' + (dupes ? ' disabled' : '') + '>Save card</button>' +
    '</div></div>';
}

function wireEditor(){
  const open = $('e-open');
  if (open) open.onclick = () => { openEditor(editing ? editing.course : COURSE_NAMES[0]); render(); };
  if (!editing) return;

  const sel = $('e-course');
  if (sel) sel.onchange = e => { openEditor(e.target.value); render(); };

  const op = $('e-osmpar');
  if (op) op.onclick = () => {
    const par = osmPar(editing.course);
    if (par) editing.par = par.slice();
    render();
  };

  document.querySelectorAll('#roundsview .srow input').forEach(inp => {
    inp.oninput = () => {
      const v = parseInt(inp.value, 10);
      editing[inp.dataset.k][+inp.dataset.i] = isNaN(v) ? null : v;
      if (inp.dataset.k === 'si'){
        const bad = siProblems(editing.si);
        document.querySelectorAll('#roundsview .srow input[data-k="si"]').forEach(x => {
          const cell = x.parentNode;
          if (cell && cell.classList) cell.classList.toggle('bad', bad[+x.dataset.i]);
        });
        const save = $('e-save');
        if (save) save.disabled = bad.some(Boolean);
      }
    };
  });

  document.querySelectorAll('#roundsview .teerow input').forEach(inp => {
    inp.oninput = () => {
      const t = editing.tees[+inp.dataset.t], f = inp.dataset.f;
      t[f] = f === 'name' ? inp.value.trim() : inp.value;
    };
  });
  document.querySelectorAll('#roundsview .teerow button').forEach(b => {
    b.onclick = () => { editing.tees.splice(+b.dataset.drop, 1); render(); };
  });

  const add = $('e-addtee');
  if (add) add.onclick = () => { editing.tees.push({name:'', cr:'', slope:''}); render(); };
  const cancel = $('e-cancel');
  if (cancel) cancel.onclick = () => { editing = null; render(); };

  const save = $('e-save');
  if (save) save.onclick = () => {
    const tees = {};
    editing.tees.forEach(t => {
      const cr = parseFloat(t.cr), sl = parseInt(t.slope, 10);
      if (t.name && !isNaN(cr) && !isNaN(sl)) tees[t.name] = { cr, slope: sl };
    });
    const card = { par: editing.par.slice(), si: editing.si.slice(), tees, stock: false };
    CARDS[editing.course] = card;
    localCards[editing.course] = card;
    saveCards();
    editing = null;
    render();
    if (getToken() && REPO) pushCards();
  };
}

function cardsYaml(){
  const nl = String.fromCharCode(10);
  const names = Object.keys(CARDS).filter(n => n !== '__default__' && (CARDS[n].par || []).length);
  if (!names.length) return 'cards: {}';
  const q = s => /^[A-Za-z][A-Za-z0-9 ()&.-]*$/.test(s) ? s : JSON.stringify(s);
  return 'cards:' + nl + names.sort().map(n => {
    const c = CARDS[n];
    const tees = Object.keys(c.tees || {});
    return '  ' + q(n) + ':' + nl +
      '    par: [' + c.par.join(', ') + ']' + nl +
      '    si:  [' + c.si.join(', ') + ']' + nl +
      (tees.length
        ? '    tees:' + nl + tees.map(tn =>
            '      ' + q(tn) + ': {cr: ' + c.tees[tn].cr + ', slope: ' + c.tees[tn].slope + '}'
          ).join(nl)
        : '    tees: {}');
  }).join(nl);
}

function serializeCardsYaml(original){
  const nl = String.fromCharCode(10);
  const lines = original.split(nl);
  let i = lines.length;
  for (let k = 0; k < lines.length; k++) if (lines[k].startsWith('cards:')){ i = k; break; }
  return lines.slice(0, i).concat(cardsYaml().split(nl)).join(nl) + nl;
}

async function pushCards(){
  if (!getToken() || !REPO) return;
  sync.state = 'busy'; sync.msg = ''; renderSyncOnly();
  try {
    const current = await gh('courses.yaml');
    const merged = serializeCardsYaml(b64decode(current.content || ''));
    await gh('courses.yaml', {
      method: 'PUT',
      headers: { 'Authorization': 'Bearer ' + getToken(),
                 'Accept': 'application/vnd.github+json',
                 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: 'card: ' + Object.keys(localCards).join(', '),
                             content: b64encode(merged), sha: current.sha }),
    });
    localCards = {}; saveCards();
    sync.state = 'ok'; sync.at = Date.now();
  } catch (e) {
    sync.state = 'error'; sync.msg = e.message;
  }
  render();
}

/* ---------- rounds view ---------- */
let sheet = null;   // the card being filled in, or null

function newSheet(){
  const today = new Date().toISOString().slice(0,10);
  return { date: today, course: COURSE_NAMES[0] || '', player: (GOLFERS[0]||{}).id || '',
           tee: '', strokes: new Array(18).fill(null) };
}

function teeOptions(course){
  return Object.keys((cardFor(course).tees) || {});
}

/* ---------- yardages ----------
   Hole maps come from OpenStreetMap, pulled into holes.yaml by holes.py. We
   only have the centre of each green, so that is all this offers — no faked
   front and back numbers.

   The watch is opt-in. Nobody wants a location prompt for opening a
   scorecard, and watchPosition is the single most expensive thing a phone
   can be doing in your pocket for four hours. */
let gps = { watch: null, pos: null, hole: null, manual: false, err: '' };

function distM(a, b){
  const R = 6371000, rad = Math.PI / 180;
  const lat = (a[0] + b[0]) / 2 * rad;
  const dx = (b[1] - a[1]) * rad * Math.cos(lat);
  const dy = (b[0] - a[0]) * rad;
  return Math.sqrt(dx*dx + dy*dy) * R;
}

function holeMap(course){
  const m = HOLES[course];
  return m && m.holes && m.holes.length ? m.holes : null;
}

function segM(p, a, b){
  const R = 6371000, rad = Math.PI / 180, cos = Math.cos(a[0] * rad);
  const m = q => [(q[1] - a[1]) * rad * cos * R, (q[0] - a[0]) * rad * R];
  const [px, py] = m(p), [bx, by] = m(b);
  const L = bx*bx + by*by;
  const t = L === 0 ? 0 : Math.max(0, Math.min(1, (px*bx + py*by) / L));
  return Math.hypot(px - bx*t, py - by*t);
}

/* Which hole you are standing on.

   Nearest tee-or-green looks fine until you are 150m down the fairway, where
   the next hole's tee is closer than either end of your own - it guessed
   wrong four times out of five there. Measuring to the hole's centreline
   instead works anywhere on the hole, which is the whole point. */
function lineM(at, h){
  const line = h.line || [h.tee, h.green].filter(Boolean);
  if (line.length === 1) return distM(at, line[0]);
  let best = Infinity;
  for (let i = 0; i < line.length - 1; i++)
    best = Math.min(best, segM(at, line[i], line[i+1]));
  return best;
}

function nearestHole(holes, at){
  let best = null, bd = Infinity;
  holes.forEach((h, i) => {
    const d = lineM(at, h);
    if (d < bd){ bd = d; best = i; }
  });
  return bd < 250 ? best : null;
}

function stopGps(){
  if (gps.watch != null) navigator.geolocation.clearWatch(gps.watch);
  gps = { watch: null, pos: null, hole: null, manual: false, err: '' };
}

function startGps(){
  if (!navigator.geolocation){ gps.err = 'This browser has no location support.'; paintGps(); return; }
  gps.err = '';
  gps.watch = navigator.geolocation.watchPosition(p => {
    gps.pos = p;
    gps.err = '';
    if (!gps.manual){
      const holes = holeMap(sheet && sheet.course);
      if (holes) gps.hole = nearestHole(holes, [p.coords.latitude, p.coords.longitude]);
    }
    const play = $('s-play');
  if (play) play.onclick = enterPlay;

  paintPlay();
  paintGps();
  }, e => {
    gps.err = e.code === 1
      ? 'Location is blocked for this page. Allow it in your browser settings.'
      : 'Could not get a fix — ' + (e.message || 'no signal') + '.';
    const play = $('s-play');
  if (play) play.onclick = enterPlay;

  paintPlay();
  paintGps();
  }, { enableHighAccuracy: true, maximumAge: 4000, timeout: 25000 });
  const play = $('s-play');
  if (play) play.onclick = enterPlay;

  paintPlay();
  paintGps();
}

/* ---------- playing a hole ----------
   The scorecard grid is for writing a round up afterwards. On the course you
   want one hole, a number big enough to read in sunlight, and a button you
   can hit without taking a glove off. */
let playing = false;
let playSlot = 0;                                  // which of the 18 is on screen
let wake = null;

async function keepAwake(on){
  // The screen going dark every 30 seconds for four hours is the difference
  // between using this and not. Not every browser has it; never mind if so.
  try {
    if (on && !wake && navigator.wakeLock) wake = await navigator.wakeLock.request('screen');
    else if (!on && wake){ await wake.release(); wake = null; }
  } catch (e) { wake = null; }
}
document.addEventListener('visibilitychange', () => {
  if (wake === null && playing && document.visibilityState === 'visible') keepAwake(true);
});

function scoreName(s, pv){
  if (s === 1) return 'ace';
  if (!pv) return s === 1 ? 'stroke' : 'strokes';
  const d = s - pv;
  return d <= -3 ? 'albatross' : d === -2 ? 'eagle' : d === -1 ? 'birdie' :
         d === 0 ? 'par' : d === 1 ? 'bogey' : d === 2 ? 'double bogey' : (d + ' over');
}

function enterPlay(){
  playing = true;
  gps.manual = false;
  const done = sheet.strokes.findIndex(x => x == null);
  playSlot = done < 0 ? 17 : done;                 // pick up where you left off
  if (gps.watch == null && holeMap(sheet.course)) startGps();
  keepAwake(true);
  render();
  window.scrollTo(0, 0);
}

function leavePlay(){
  playing = false;
  keepAwake(false);
  render();
}

function paintPlay(){
  const el = $('play');
  if (!el || !sheet) return;
  const par = (cardFor(sheet.course).par) || [];
  const holes = holeMap(sheet.course);

  // follow the GPS unless somebody stepped through by hand
  if (holes && !gps.manual && gps.hole != null){
    const ref = holes[gps.hole].hole;
    if (ref >= 1 && ref <= 18) playSlot = ref - 1;
  }

  const i = playSlot, sc = sheet.strokes[i];
  // the mapped hole for this slot - the one the GPS picked when it agrees,
  // which is what tells a 36-hole club's two 7th holes apart
  const mh = !holes ? null
    : (gps.hole != null && holes[gps.hole].hole === i + 1) ? holes[gps.hole]
    : (holes.find(h => h.hole === i + 1) || null);

  // The placeholder card would have us call a 500m hole a par 3. Where there
  // is no real card yet, the map's par is the better guess of the two - and
  // the note says where it came from rather than letting you assume.
  const stock = cardFor(sheet.course).stock;
  const mapPar = stock && mh && mh.par ? mh.par : null;
  const pv = mapPar || par[i];

  let yds;
  if (!holes){
    yds = '<div class="note">No hole map for ' + esc(sheet.course) +
      ', so no yardages here. Scoring still works.</div>';
  } else if (gps.watch == null){
    yds = '<div class="note">GPS is off. ' +
      '<button type="button" id="p-gps">Turn it on</button></div>';
  } else if (gps.err){
    yds = '<div class="note">' + esc(gps.err) + '</div>';
  } else if (!gps.pos){
    yds = '<div class="note">Waiting for a fix&hellip;</div>';
  } else {
    const at = [gps.pos.coords.latitude, gps.pos.coords.longitude];
    const d = mh && mh.green ? Math.round(distM(at, mh.green)) : null;
    const tee = mh && mh.tee ? Math.round(distM(at, mh.tee)) : null;
    yds = (d == null
        ? '<div class="note">No green mapped on this hole.</div>'
        : '<div class="big">' + d + '<small>m</small></div>' +
          '<div class="cap">to the middle of the green</div>') +
      '<div class="note">' +
      (tee != null && tee > 30 ? tee + 'm from the tee &middot; ' : '') +
      '&plusmn;' + Math.round(gps.pos.coords.accuracy || 0) + 'm' +
      (mapPar ? ' &middot; par off the map, no card saved yet' : '') +
      (gps.manual ? ' &middot; picked by hand &middot; ' +
        '<button type="button" id="p-auto">follow the GPS</button>' : '') +
      '</div>';
  }

  const entered = sheet.strokes.filter(x => x != null).length;
  const gross = sheet.strokes.reduce((a, b) => a + (b || 0), 0);
  const parSoFar = par.reduce((a, pp, k) => a + (sheet.strokes[k] != null ? (pp || 0) : 0), 0);
  const rel = gross - parSoFar;

  el.innerHTML =
    '<div class="phead"><div><span class="hn">Hole ' + (i + 1) + '</span>' +
      '<span class="hm">' + (pv ? 'par ' + pv : '') +
      (mh && mh.len ? ' &middot; ' + mh.len + 'm' : '') + '</span></div>' +
      '<button class="btn ghost" type="button" id="p-card">Scorecard</button></div>' +
    '<div class="pyds">' + yds + '</div>' +
    '<div class="pscore">' +
      '<button type="button" id="p-minus" aria-label="One stroke less">&minus;</button>' +
      '<div class="val"><b>' + (sc == null ? '&mdash;' : sc) + '</b><span>' +
        (sc == null ? 'no score yet' : scoreName(sc, pv)) + '</span></div>' +
      '<button type="button" id="p-plus" aria-label="One stroke more">+</button>' +
    '</div>' +
    '<div class="pnav">' +
      '<button type="button" id="p-prev"' + (i ? '' : ' disabled') + '>&lsaquo; ' +
        (i ? 'Hole ' + i : 'back') + '</button>' +
      '<span class="thru">' + entered + ' of 18 &middot; <b>' + (gross || '&mdash;') + '</b>' +
        (entered ? ' (' + (rel > 0 ? '+' : '') + rel + ')' : '') + '</span>' +
      '<button type="button" id="p-next"' + (i < 17 ? '' : ' disabled') + '>' +
        (i < 17 ? 'Hole ' + (i + 2) : 'done') + ' &rsaquo;</button>' +
    '</div>';

  const bind = (id, fn) => { const e = $(id); if (e) e.onclick = fn; };
  bind('p-card', leavePlay);
  bind('p-gps', () => { startGps(); paintPlay(); });
  bind('p-auto', () => { gps.manual = false; paintPlay(); });
  bind('p-minus', () => {
    sheet.strokes[i] = sheet.strokes[i] == null ? (pv || 4) : Math.max(1, sheet.strokes[i] - 1);
    paintPlay();
  });
  bind('p-plus', () => {
    sheet.strokes[i] = sheet.strokes[i] == null ? (pv || 4) : Math.min(20, sheet.strokes[i] + 1);
    paintPlay();
  });
  // stepping by hand pins the hole, so a wandering fix can't move it under you
  bind('p-prev', () => { gps.manual = true; playSlot = Math.max(0, i - 1); paintPlay(); });
  bind('p-next', () => { gps.manual = true; playSlot = Math.min(17, i + 1); paintPlay(); });
}

function gpsBlock(){
  const holes = holeMap(sheet && sheet.course);
  if (!holes) return '';
  return '<div class="gps" id="gps"></div>';
}

function paintGps(){
  if (playing) return paintPlay();
  const el = $('gps');
  if (!el) return;
  const holes = holeMap(sheet && sheet.course);
  if (!holes) return;

  const on = gps.watch != null;
  el.className = 'gps' + (on && gps.pos && !gps.err ? ' live' : '');

  let body = '';
  if (!on){
    const loops = (HOLES[sheet.course] || {}).loops || 1;
    body = '<div class="note">' + holes.length + ' holes mapped for ' +
      esc(sheet.course) + (loops > 1 ? ', across its ' + loops + ' courses' : '') +
      '. Turn this on at the course to see how far you are ' +
      'from the middle of the green.</div>';
  } else if (gps.err){
    body = '<div class="note">' + esc(gps.err) + '</div>';
  } else if (!gps.pos){
    body = '<div class="note">Waiting for a fix&hellip;</div>';
  } else {
    const at = [gps.pos.coords.latitude, gps.pos.coords.longitude];
    const i = gps.hole;
    const h = i == null ? null : holes[i];
    if (!h){
      body = '<div class="note">You are not on any mapped hole at ' +
        esc(sheet.course) + '. Step through the holes below if you want a number anyway.</div>';
    } else {
      const d = h.green ? Math.round(distM(at, h.green)) : null;
      const acc = Math.round(gps.pos.coords.accuracy || 0);
      body = '<div class="read">' +
        (d == null ? '<div class="where">No green mapped on this hole.</div>'
          : '<div class="m">' + d + '<small>m</small></div>') +
        '<div class="where"><b>Hole ' + (h.hole || (i+1)) + '</b>' +
        (h.par ? ' &middot; par ' + h.par : '') +
        (h.len ? ' &middot; ' + h.len + 'm' : '') +
        '<br>to the middle of the green</div></div>' +
        '<div class="note">&plusmn;' + acc + 'm from your phone' +
        (gps.manual ? ' &middot; hole picked by hand' : '') + '</div>';
    }
  }

  const i = gps.hole;
  el.innerHTML =
    '<div class="top"><span class="ttl">Yardage</span>' +
      '<button class="btn' + (on ? ' ghost' : '') + '" type="button" id="g-toggle">' +
      (on ? 'Turn off' : 'Use GPS') + '</button></div>' + body +
    (on && !gps.err ? '<div class="step">' +
      '<button type="button" id="g-prev"' + (i ? '' : ' disabled') + '>&lsaquo; prev</button>' +
      '<button type="button" id="g-next"' +
        (i != null && i < holes.length - 1 ? '' : ' disabled') + '>next &rsaquo;</button>' +
      (gps.manual ? '<button type="button" class="auto" id="g-auto">back to auto</button>' : '') +
      '</div>' : '');

  $('g-toggle').onclick = () => {
    if (gps.watch != null) stopGps(); else startGps();   // live, not the captured state
    const play = $('s-play');
  if (play) play.onclick = enterPlay;

  paintPlay();
  paintGps();
  };
  const step = n => { gps.manual = true; gps.hole = (gps.hole || 0) + n; paintGps(); };
  if ($('g-prev')) $('g-prev').onclick = () => step(-1);
  if ($('g-next')) $('g-next').onclick = () => step(1);
  if ($('g-auto')) $('g-auto').onclick = () => { gps.manual = false; paintGps(); };
}

function renderSheet(){
  if (!sheet) return '';
  const card = cardFor(sheet.course);
  const par = card.par || [];
  const tees = teeOptions(sheet.course);
  if (sheet.tee && tees.indexOf(sheet.tee) < 0) sheet.tee = '';
  if (!sheet.tee && tees.length) sheet.tee = tees[0];

  if (playing) return '<div class="sheet play"><div id="play"></div></div>';

  const nine = (from, label) => {
    const idx = [];
    for (let i = from; i < from + 9; i++) idx.push(i);
    const sub = idx.reduce((a,i) => a + (sheet.strokes[i] || 0), 0);
    return '<div class="nine"><div class="lbl">' + label + '</div><div class="holes">' +
      idx.map(i => {
        const s = sheet.strokes[i], pv = par[i];
        const cls = s == null || pv == null ? '' : (s < pv ? ' sub' : (s > pv + 1 ? ' over' : ''));
        return '<div class="hole' + cls + '"><span class="n">' + (i+1) + '</span>' +
          '<input type="number" inputmode="numeric" min="1" max="20" data-hole="' + i + '"' +
          ' value="' + (s == null ? '' : s) + '" aria-label="Hole ' + (i+1) + '">' +
          '<span class="pr">' + (pv == null ? '' : pv) + '</span></div>';
      }).join('') +
      '<div class="hole tot"><span class="n">' + (from ? 'IN' : 'OUT') + '</span>' +
      '<span class="v" id="sub-' + from + '">' + (sub || '') + '</span><span class="pr">' +
      par.slice(from, from+9).reduce((a,b)=>a+b,0) + '</span></div></div>';
  };

  const entered = sheet.strokes.filter(s => s != null).length;
  const gross = sheet.strokes.reduce((a,b) => a + (b||0), 0);
  const totalPar = par.reduce((a,b)=>a+b,0);
  const rel = gross - totalPar;

  let warn = '';
  if (card.stock) warn += '<div class="warnrow"><b>Using the placeholder card.</b> ' +
    'Par and stroke index for ' + esc(sheet.course) + ' are a generic par 72, not that course. ' +
    'Fix them in courses.yaml so birdies and the net double bogey cap are right.</div>';
  if (!tees.length) warn += '<div class="warnrow"><b>No Course Rating for ' + esc(sheet.course) +
    '.</b> The round will be saved, but it cannot count toward a handicap until a tee with ' +
    'cr and slope is added to courses.yaml. Both are printed on the scorecard.</div>';

  return '<div class="sheet">' +
    '<div class="row">' +
      '<label>Date<input type="date" id="s-date" value="' + esc(sheet.date) + '"></label>' +
      '<label>Player<select id="s-player">' + GOLFERS.map(g =>
        '<option value="' + esc(g.id) + '"' + (g.id === sheet.player ? ' selected' : '') + '>' +
        esc(g.name) + '</option>').join('') + '</select></label>' +
    '</div>' +
    '<div class="row">' +
      '<label>Course<select id="s-course">' + COURSE_NAMES.map(c =>
        '<option value="' + esc(c) + '"' + (c === sheet.course ? ' selected' : '') + '>' +
        esc(c) + '</option>').join('') + '</select></label>' +
      '<label>Tee<select id="s-tee">' + (tees.length
        ? tees.map(x => '<option value="' + esc(x) + '"' + (x === sheet.tee ? ' selected' : '') +
            '>' + esc(x) + '</option>').join('')
        : '<option value="">none rated</option>') + '</select></label>' +
    '</div>' +
    gpsBlock() + nine(0, 'Out') + nine(9, 'In') + warn +
    '<div class="foot">' +
      '<span class="run">' + entered + ' of 18 &middot; <b>' + (gross || '—') + '</b>' +
      (entered === 18 ? ' (' + (rel > 0 ? '+' : '') + rel + ')' : '') + '</span>' +
      '<button class="btn ghost" type="button" id="s-play">Play</button>' +
      '<button class="btn ghost" type="button" id="s-cancel">Cancel</button>' +
      '<button class="btn" type="button" id="s-save"' + (entered === 18 ? '' : ' disabled') +
      '>Save round</button>' +
    '</div></div>';
}

function renderRounds(){
  const el = $('roundsview');

  const boards = GOLFERS.map(g => {
    const rec = playerRecord(g.id);
    const left = rec.index === null ? Math.max(0, 3 - rec.counted) : 0;
    return '<div class="hcap"><div class="who">' + esc(g.name) + '</div>' +
      '<div class="idx">' + (rec.index === null ? '—' : rec.index.toFixed(1)) +
      ' <small>' + (rec.index === null ? 'no index yet' : 'index') + '</small></div>' +
      '<div class="note">' + rec.played + (rec.played === 1 ? ' round' : ' rounds') +
      ' &middot; ' + rec.counted + ' counting' +
      (left ? ' &middot; ' + left + ' more to get an index' : '') + '</div></div>';
  }).join('');

  const recs = {};
  GOLFERS.forEach(g => { recs[g.id] = playerRecord(g.id); });

  const rows = allRounds()
    .slice().sort((a,b) => String(b.date).localeCompare(String(a.date)))
    .map(r => {
      const info = roundInfo(r);
      const who = (GOLFERS.find(g => g.id === r.player) || {}).name || r.player;
      const mark = (recs[r.player] || {perRound:{}}).perRound[r.date + r.course];
      const counted = mark && mark.counted;
      const rel = info.toPar;
      const d = new Date(r.date + 'T00:00:00');
      const when = isNaN(d) ? esc(String(r.date))
        : d.toLocaleDateString('en-AU', { day:'numeric', month:'short' });
      return '<div class="rnd' + (counted ? '' : ' uncounted') + '">' +
        '<span class="when">' + when + '</span>' +
        '<div class="where"><div class="c">' + esc(r.course) + '</div>' +
        '<div class="m">' + esc(who) + (r.tee ? ' &middot; ' + esc(r.tee) : '') +
        (info.holes !== 18 ? ' &middot; ' + info.holes + ' holes' : '') +
        (counted ? ' &middot; diff ' + mark.diff.toFixed(1)
                 : ' &middot; ' + ((mark && mark.why) || 'not counted')) +
        '</div></div>' +
        '<div class="sc"><div class="g">' + info.gross + '</div>' +
        '<div class="p ' + (rel > 0 ? 'over' : rel < 0 ? 'under' : '') + '">' +
        (rel > 0 ? '+' : '') + rel + '</div></div></div>';
    }).join('');

  // On the course the hole is the only thing that matters. Handicap boards and
  // a sync bar are things to read at home, and scrolling past them in the sun
  // with a glove on is not a feature.
  if (playing){
    el.innerHTML = renderSheet();
    wireRounds();
    return;
  }

  const rated = COURSE_NAMES.filter(c => Object.keys((cardFor(c).tees) || {}).length).length;
  const drift = localRounds.length;
  el.innerHTML = syncBar() +
    '<div class="hcaps">' + boards + '</div>' +
    '<div class="roundbar">' +
      (sheet || editing ? '' :
        '<button class="btn" type="button" id="r-add">Add a round</button>' +
        '<button class="btn ghost" type="button" id="e-open">Course cards</button>') +
      '<span class="hint">' + rated + ' of ' + COURSE_NAMES.length + ' courses rated. ' +
      'Handicap uses the World Handicap System: best differentials of your last 20 ' +
      'rounds, each hole capped at net double bogey. 18-hole rounds only.</span>' +
    '</div>' +
    renderSheet() + renderEditor() +
    (rows ? '<div class="rnds">' + rows + '</div>'
          : '<div class="empty"><h3>No rounds yet</h3><p>Add one and the handicap starts ' +
            'building. It needs three rounds before it can show an index.</p></div>') +
    (Object.keys(localCards).length && !getToken()
      ? '<div class="unsaved"><span><b>' + Object.keys(localCards).length +
        ' course card(s) on this device only.</b> Paste into courses.yaml and commit.</span>' +
        '<button type="button" id="c-export">Copy for courses.yaml</button>' +
        '<textarea class="yaml" id="c-yaml" readonly>' + esc(cardsYaml()) + '</textarea></div>' : '') +
    (drift && !getToken() ? '<div class="unsaved"><span><b>' + drift +
      (drift === 1 ? ' round' : ' rounds') + ' on this device only.</b> ' +
      'Turn on sync above, or paste this into rounds.yaml and commit.</span>' +
      '<button type="button" id="r-export">Copy for rounds.yaml</button>' +
      '<textarea class="yaml" id="r-yaml" readonly>' + esc(roundsYaml()) + '</textarea></div>' : '');

  wireSync();
  wireRounds();
  wireEditor();
}

function wireRounds(){
  const add = $('r-add');
  if (add) add.onclick = () => { sheet = newSheet(); render(); };

  const bind = (id, fn) => { const e = $(id); if (e) e.onchange = fn; };
  bind('s-date',   e => { sheet.date = e.target.value; });
  bind('s-player', e => { sheet.player = e.target.value; });
  bind('s-tee',    e => { sheet.tee = e.target.value; });
  bind('s-course', e => {
    sheet.course = e.target.value; sheet.tee = '';
    gps.hole = null; gps.manual = false;          // different course, different holes
    render();
  });

  const play = $('s-play');
  if (play) play.onclick = enterPlay;

  paintPlay();
  paintGps();

  document.querySelectorAll('#roundsview .hole input').forEach(inp => {
    inp.oninput = () => {
      const v = parseInt(inp.value, 10);
      const h = +inp.dataset.hole;
      const s = (isNaN(v) || v < 1) ? null : Math.min(v, 20);
      sheet.strokes[h] = s;

      // recolour this hole, and refresh the nine it belongs to
      const par = cardFor(sheet.course).par || [];
      const cell = inp.parentNode;
      if (cell && cell.classList){
        cell.classList.remove('sub', 'over');
        if (s != null && par[h] != null){
          if (s < par[h]) cell.classList.add('sub');
          else if (s > par[h] + 1) cell.classList.add('over');
        }
      }
      [0, 9].forEach(from => {
        const box = $('sub-' + from);
        if (!box) return;
        let n = 0;
        for (let i = from; i < from + 9; i++) n += sheet.strokes[i] || 0;
        box.textContent = n || '';
      });

      const foot = document.querySelector('#roundsview .sheet .foot');
      if (foot){
        const entered = sheet.strokes.filter(s => s != null).length;
        const gross = sheet.strokes.reduce((a,b) => a + (b||0), 0);
        const totalPar = par.reduce((a,b)=>a+b,0);
        const rel = gross - totalPar;
        foot.querySelector('.run').innerHTML = entered + ' of 18 &middot; <b>' +
          (gross || '—') + '</b>' + (entered === 18 ? ' (' + (rel>0?'+':'') + rel + ')' : '');
        const save = $('s-save');
        if (save) save.disabled = entered !== 18;
      }
    };
    // advance to the next hole as soon as a sensible score is in
    inp.onkeyup = ev => {
      if (ev.key !== 'Enter') return;
      const all = [...document.querySelectorAll('#roundsview .hole input')];
      const next = all[all.indexOf(inp) + 1];
      if (next) next.focus();
    };
  });

  const cancel = $('s-cancel');
  if (cancel) cancel.onclick = () => { stopGps(); keepAwake(false); playing = false; sheet = null; render(); };

  const save = $('s-save');
  if (save) save.onclick = () => {
    localRounds.push({ date: sheet.date, course: sheet.course, player: sheet.player,
                       tee: sheet.tee || null, strokes: sheet.strokes.slice() });
    saveRounds();
    stopGps();
    keepAwake(false);
    playing = false;
    sheet = null;
    render();
    if (getToken() && REPO) pushRounds();
  };

  const cex = $('c-export');
  if (cex) cex.onclick = () => {
    const box = $('c-yaml');
    box.select();
    try { navigator.clipboard.writeText(box.value); } catch (e) {
      try { document.execCommand('copy'); } catch (e2) {} }
    cex.textContent = 'Copied';
  };

  const ex = $('r-export');
  if (ex) ex.onclick = () => {
    const box = $('r-yaml');
    box.select();
    try { navigator.clipboard.writeText(box.value); } catch (e) {
      try { document.execCommand('copy'); } catch (e2) {} }
    ex.textContent = 'Copied';
  };
}

function renderTabs(){
  const el = $('tabs');
  const left = COURSES.filter(c => !hasPlayed(c.club)).length;
  const rounds = allRounds().length;
  const defs = [['times', 'Tee times', DATA.length.toLocaleString()],
                ['courses', 'Courses', left + ' to play'],
                ['rounds', 'Rounds', rounds ? String(rounds) : 'new']];
  el.innerHTML = '';
  defs.forEach(([k, label, tally]) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.setAttribute('aria-pressed', String(state.view === k));
    b.innerHTML = esc(label) + '<span class="tally">' + esc(tally) + '</span>';
    b.onclick = () => { state.view = k; render(); };
    el.appendChild(b);
  });
}

function renderRing(){
  const el = $('ring');
  // Only the drive filter applies — there are no times to filter on.
  const rows = CONTACTS.filter(c => c.drive <= state.drive);
  if (!rows.length){ el.innerHTML = ''; return; }

  const phoneIcon = '<svg viewBox="0 0 24 24"><path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1.9.3 1.8.7 2.6a2 2 0 0 1-.5 2.1L8.1 9.6a16 16 0 0 0 6 6l1.2-1.2a2 2 0 0 1 2.1-.5c.8.4 1.7.6 2.6.7a2 2 0 0 1 1.7 2Z"/></svg>';

  el.innerHTML =
    '<div class="dayhead"><h2>No online sheet</h2><div class="ln"></div>' +
    '<span class="cnt">' + rows.length + (rows.length === 1 ? ' club' : ' clubs') + '</span></div>' +
    '<div class="grid ring">' + rows.map(c =>
      '<article class="card">' +
        '<div class="chead"><div class="cinfo">' +
          '<span class="cname">' + esc(c.club) + '</span>' +
          '<div class="cmeta">' + esc(c.note || 'Call the pro shop to check availability') + '</div>' +
        '</div><span class="drive' + (c.drive <= 20 ? ' near' : '') + '">' + c.drive + ' min</span></div>' +
        '<div class="acts">' +
          (c.phone ? '<a class="call" href="tel:' + esc(c.phone) + '">' + phoneIcon +
                     esc(c.phoneText || c.phone) + '</a>' : '') +
          (c.url ? '<a class="online" href="' + esc(c.url) + '" target="_blank" rel="noopener">Book online</a>' : '') +
        '</div>' +
      '</article>').join('') +
    '</div>';
}

function render(){
  const onCourses = state.view === 'courses';
  const onRounds = state.view === 'rounds';
  $('filterbar').hidden = onCourses || onRounds;
  $('timesview').hidden = onCourses || onRounds;
  $('coursesview').hidden = !onCourses;
  $('roundsview').hidden = !onRounds;

  renderTabs();
  // Leaving the scorecard behind means nothing is reading the GPS, so drop
  // the watch rather than tracking the phone around in the background.
  if (!onRounds){ stopGps(); keepAwake(false); playing = false; }
  if (onRounds){
    renderRounds();
  } else if (onCourses){
    renderCourses();
  } else {
    syncControls();
    renderDays();
    renderList();
    renderRing();
  }
  save();
}

/* Name the clubs that errored on the last scan. Each message is already
   "Club: what went wrong", so lead with the count and list the detail. */
function renderFailed(){
  const el = $('failed');
  if (!PROBLEMS.length){ el.hidden = true; return; }
  const names = PROBLEMS.map(m => m.split(':')[0].trim());
  el.hidden = false;
  el.innerHTML =
    '<b>' + PROBLEMS.length + (PROBLEMS.length === 1 ? ' club' : ' clubs') +
    ' did not respond on the last scan</b> — ' + esc(names.join(', ')) +
    '. Their tee times are missing from this page, not unavailable.' +
    '<ul>' + PROBLEMS.map(m => '<li>' + esc(m) + '</li>').join('') + '</ul>';
}
renderFailed();

// ODbL asks for attribution wherever the data is shown, so the credit only
// appears on a build that actually carries hole maps.
$('osmattr').hidden = !Object.keys(HOLES).length;

$('built').textContent = ago(BUILT);
render();
// The shared list reads without a token, so both phones see the same ticks.
// A token is only needed to write one.
if (getToken() && REPO) pull(); else pullPublic();
</script>
</body></html>
"""


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--max-drive", type=int, default=45)
    p.add_argument("--all-fees", action="store_true")
    p.add_argument("--open", action="store_true")
    p.add_argument("--offline", action="store_true",
                   help="rebuild the page from the last scan instead of hitting the clubs")
    args = p.parse_args()

    if args.offline:
        records = cached_records(args.max_drive)
        problems = cached_problems()
    else:
        openings, problems = collect(args.days, args.max_drive, args.all_fees)
        for msg in problems:
            print(f"  ! {msg}", file=sys.stderr)
        records = to_records(openings)

    clubs = len({r["club"] for r in records})
    print(f"{len(records)} tee times across {clubs} clubs", file=sys.stderr)

    # Offline rebuilds keep the original scan time so the page doesn't claim
    # to be fresher than the data behind it.
    built = cached_built() if args.offline else datetime.now().astimezone().isoformat()

    contacts = collect_contacts(args.max_drive)
    if contacts:
        print(f"{len(contacts)} call-to-book clubs", file=sys.stderr)

    courses = collect_courses()
    ticked = sum(1 for c in courses if c["done"])
    print(f"{len(courses)} courses on the checklist, {ticked} played", file=sys.stderr)

    rounds, cards, golfers = collect_scoring()
    rated = sum(1 for c in cards.values() if c.get("tees"))
    print(f"{len(rounds)} rounds recorded, {rated} courses rated", file=sys.stderr)

    holes = collect_holes()
    if holes:
        print(f"{len(holes)} courses with hole maps", file=sys.stderr)

    html = (TEMPLATE
            .replace("__DATA__", json.dumps(records, separators=(",", ":")))
            .replace("__CONTACTS__", json.dumps(contacts, separators=(",", ":")))
            .replace("__COURSES__", json.dumps(courses, separators=(",", ":")))
            .replace("__PROBLEMS__", json.dumps(problems, separators=(",", ":")))
            .replace("__ROUNDS__", json.dumps(rounds, separators=(",", ":"), default=str))
            .replace("__CARDS__", json.dumps(cards, separators=(",", ":")))
            .replace("__GOLFERS__", json.dumps(golfers, separators=(",", ":")))
            .replace("__HOLES__", json.dumps(holes, separators=(",", ":")))
            .replace("__REPO__", repo_slug())
            .replace("__BUILT__", built)
            .replace("__NCLUB__", str(clubs))
            .replace("__DRIVE__", str(args.max_drive)))

    OUT_DIR.mkdir(exist_ok=True)
    OUT_FILE.write_text(html, encoding="utf-8")
    print(f"wrote {OUT_FILE}", file=sys.stderr)

    if args.open and sys.platform == "darwin":
        subprocess.run(["open", str(OUT_FILE)], check=False)

    return 0


if __name__ == "__main__":
    sys.exit(main())
