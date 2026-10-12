#!/usr/bin/env python3
"""
holes.py - pull hole geometry from OpenStreetMap into holes.yaml.

    python3 holes.py            fetch anything missing, write holes.yaml
    python3 holes.py --refresh  re-fetch every course
    python3 holes.py --refresh "The Glades"
                                re-fetch one, for when somebody has just
                                mapped it in OSM and you want it now
    python3 holes.py --list     just show the OSM course match-up, fetch nothing

Run it by hand, not from the workflow. Course layouts change about never, and
Overpass is heavily shared - there is no reason to hit it every few hours.
Committing the result also means the app never depends on Overpass being up
while you are standing on a tee with one bar of signal.

Two things it does that the obvious version gets wrong:

  * It asks Overpass which golf courses exist, instead of geocoding the names
    from clubs.yaml. Geocoding "The Glades" lands on a street in another
    suburb and you quietly get no holes.
  * It scopes each hole query to that course's own polygon. A bounding box
    around Palmer Colonial also contains Palmer Gold Coast, and you end up
    with a 57 hole golf course.

Progress is written after every course, so an Overpass outage mid-run costs
you that course and nothing else - just run it again.

Data (c) OpenStreetMap contributors, licensed ODbL. Attribution is required
wherever it is shown; the page carries it in the footer.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).parent
OUT = ROOT / "holes.yaml"

# Gold Coast, Logan, the Scenic Rim and the Tweed - everything in clubs.yaml.
# Split into quadrants because Overpass refuses expensive queries when busy.
BBOX = (-28.70, 152.95, -27.55, 153.62)

MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
UA = "teetime-watch/1.0 (personal golf tracker)"

# Where the courses are. Tags and a centre point only, so it stays cheap.
COURSE_Q = """
[out:json][timeout:120];
(
  way["leisure"="golf_course"]({bbox});
  relation["leisure"="golf_course"]({bbox});
);
out tags center;
"""

# Holes inside one course. map_to_area turns the course polygon into a search
# area, which is what keeps a neighbouring course out of the results.
HOLE_Q = """
[out:json][timeout:90];
{sel}; map_to_area->.a;
(
  way(area.a)["golf"="hole"];
);
out tags geom;
(
  way(area.a)["golf"="green"];
  relation(area.a)["golf"="green"];
);
out center;
"""


# Fallback for a course whose polygon map_to_area can't use - an unclosed way,
# or holes a mapper drew just outside the boundary. A box around the course
# also contains its neighbours, so every hole found this way is kept only if
# this course is the closest one to it.
BOX_Q = """
[out:json][timeout:90];
(
  way["golf"="hole"]({bbox});
);
out tags geom;
(
  way["golf"="green"]({bbox});
  relation["golf"="green"]({bbox});
);
out center;
"""


def overpass(query: str, tries: int = 5) -> dict:
    """Overpass 504s and 500s a lot under load. Back off and try the mirrors.

    These are small queries - a healthy mirror answers in seconds. Waiting
    minutes on a sick one just means every mirror is unwell by the time we
    give up, so the socket timeout is short and we come back more often.
    """
    body = urllib.parse.urlencode({"data": query}).encode()
    last = ""
    for attempt in range(tries):
        for url in MIRRORS:
            req = urllib.request.Request(url, data=body, headers={"User-Agent": UA})
            try:
                with urllib.request.urlopen(req, timeout=70) as r:
                    return json.loads(r.read().decode())
            except (urllib.error.HTTPError, urllib.error.URLError,
                    json.JSONDecodeError, TimeoutError, OSError) as e:
                last = f"{url.split('/')[2]}: {e}"
            time.sleep(1)
        if attempt + 1 < tries:
            wait = 15 * (attempt + 1)
            print(f"    all mirrors failed ({last}), waiting {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(last or "no mirrors")


def metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Equirectangular approximation - accurate to centimetres at these distances."""
    lat = math.radians((a[0] + b[0]) / 2)
    dx = math.radians(b[1] - a[1]) * math.cos(lat)
    dy = math.radians(b[0] - a[0])
    return math.hypot(dx, dy) * 6371000


def wanted_names() -> list[str]:
    clubs = yaml.safe_load((ROOT / "clubs.yaml").read_text())["clubs"]
    names = [c["name"] for c in clubs if c.get("status") not in {"closed", "unverified"}]
    played = ROOT / "played.yaml"
    if played.exists():
        rec = yaml.safe_load(played.read_text()) or {}
        names += [c["name"] for c in (rec.get("courses") or [])]
    return sorted(set(names))


def norm(s: str) -> str:
    drop = (" golf club", " golf course", " country club", " golf links",
            " golf resort", " golf and country", " links", " golf", " club",
            " (nsw)", " resort", " village", " the ")
    s = s.lower().replace("&", " and ").replace("—", " ").replace("-", " ")
    s = "".join(ch if ch.isalnum() or ch == " " else " " for ch in s)
    s = " " + " ".join(s.split()) + " "
    for d in drop:
        s = s.replace(d, " ")
    return " ".join(s.split())


def pair_up(courses: list[dict], wanted: list[str]) -> dict[str, dict]:
    """Match OSM course polygons to the names we care about.

    Loose both ways - a tee sheet says "Links Hope Island", the map says
    "Hope Island Golf Club". Short tokens are rejected so that a two letter
    overlap can't marry two unrelated clubs.
    """
    out: dict[str, dict] = {}
    for c in courses:
        osm_name = (c.get("tags") or {}).get("name")
        if not osm_name or not c.get("center"):
            continue
        o = norm(osm_name)
        if len(o) < 4:
            continue
        for w in wanted:
            n = norm(w)
            if len(n) < 4:
                continue
            if n == o or n in o or o in n:
                prev = out.get(w)
                # prefer the closer name match if two polygons both claim it
                if prev is None or abs(len(norm(prev["name"])) - len(n)) > abs(len(o) - len(n)):
                    out[w] = {"name": osm_name,
                              "type": "rel" if c["type"] == "relation" else "way",
                              "id": c["id"],
                              "at": [round(c["center"]["lat"], 6),
                                     round(c["center"]["lon"], 6)]}
    return out


def course_list() -> list[dict]:
    """Four quadrant queries - the whole region in one go gets refused."""
    s, w, n, e = BBOX
    mlat, mlon = (s + n) / 2, (w + e) / 2
    quads = [(s, w, mlat, mlon), (s, mlon, mlat, e),
             (mlat, w, n, mlon), (mlat, mlon, n, e)]
    seen: dict[tuple[str, int], dict] = {}
    for i, q in enumerate(quads, 1):
        bbox = ",".join(f"{v:.4f}" for v in q)
        print(f"  quadrant {i}/4", file=sys.stderr)
        data = overpass(COURSE_Q.format(bbox=bbox))
        for el in data.get("elements", []):
            seen[(el["type"], el["id"])] = el
        time.sleep(2)
    return list(seen.values())


def assign_greens(holes: list[dict], greens: list[dict]) -> dict[int, dict]:
    """One green per hole, nearest first.

    A golf=hole way runs tee to green, so the green sits at an end of the
    line - but not always the last point, since mappers draw them both ways.
    Score every hole-end against every green, take the best pairings in
    order, and never hand the same green to two holes.
    """
    cand = []
    for hi, h in enumerate(holes):
        geom = h.get("geometry") or []
        if not geom:
            continue
        ends = [(geom[0]["lat"], geom[0]["lon"]), (geom[-1]["lat"], geom[-1]["lon"])]
        for gi, g in enumerate(greens):
            gc = (g["center"]["lat"], g["center"]["lon"])
            d = min(metres(e, gc) for e in ends)
            if d < 120:
                cand.append((d, hi, gi))
    cand.sort()
    out: dict[int, dict] = {}
    used: set[int] = set()
    for d, hi, gi in cand:
        if hi in out or gi in used:
            continue
        out[hi] = greens[gi]
        used.add(gi)
    return out


def seg_dist(p, a, b) -> float:
    """Metres from p to the segment ab."""
    lat = math.radians(a[0])
    to_m = lambda q: (math.radians(q[1] - a[1]) * math.cos(lat) * 6371000,
                      math.radians(q[0] - a[0]) * 6371000)
    px, py = to_m(p)
    bx, by = to_m(b)
    L = bx * bx + by * by
    t = 0.0 if L == 0 else max(0.0, min(1.0, (px * bx + py * by) / L))
    return math.hypot(px - bx * t, py - by * t)


def simplify(pts: list, tol: float = 8.0) -> list:
    """Douglas-Peucker, tolerance in metres.

    A hole is stored as a line so the app can tell which hole you are on from
    the middle of the fairway. Full OSM geometry is more points than that
    needs - the shape of a dogleg survives an 8 metre tolerance fine.
    """
    if len(pts) < 3:
        return list(pts)
    worst, wi = -1.0, 0
    for i in range(1, len(pts) - 1):
        d = seg_dist(pts[i], pts[0], pts[-1])
        if d > worst:
            worst, wi = d, i
    if worst <= tol:
        return [pts[0], pts[-1]]
    return simplify(pts[:wi + 1], tol)[:-1] + simplify(pts[wi:], tol)


def el_point(el: dict) -> tuple[float, float] | None:
    geom = el.get("geometry") or []
    if geom:
        mid = geom[len(geom) // 2]
        return (mid["lat"], mid["lon"])
    if el.get("center"):
        return (el["center"]["lat"], el["center"]["lon"])
    return None


def own(els: list[dict], mine: str, centres: dict[str, list[float]]) -> list[dict]:
    """Drop anything that sits closer to some other course than to this one."""
    keep = []
    for el in els:
        pt = el_point(el)
        if pt is None:
            continue
        near = min(centres, key=lambda n: metres(pt, tuple(centres[n])))
        if near == mine:
            keep.append(el)
    return keep


def mid(el: dict):
    geom = el.get("geometry") or []
    if not geom:
        return None
    a, b = geom[0], geom[-1]
    return ((a["lat"] + b["lat"]) / 2, (a["lon"] + b["lon"]) / 2)


def collapse(holes: list[dict]) -> list[dict]:
    """Drop hole ways that are another drawing of the same hole.

    Palmer Colonial is in OSM three times over - once per tee, by the look of
    it, and one of them drawn backwards. Left alone, 54 ways compete for 21
    greens and the leftovers get handed a green belonging to a different
    hole, which is worse than having no yardage at all.

    Midpoints are the test: they survive a hole being drawn tee-to-green or
    green-to-tee and barely move between tees, while the 7th of one course
    and the 7th of the other at a 36-hole club are nowhere near each other.
    The longest drawing wins, since that is the one off the back tee.
    """
    def length(el):
        g = el.get("geometry") or []
        return sum(metres((g[i]["lat"], g[i]["lon"]), (g[i+1]["lat"], g[i+1]["lon"]))
                   for i in range(len(g) - 1))

    kept: list[dict] = []
    for h in sorted(holes, key=length, reverse=True):
        m, ref = mid(h), (h.get("tags") or {}).get("ref")
        if m is None:
            kept.append(h)
            continue
        twin = any((k.get("tags") or {}).get("ref") == ref and mid(k)
                   and metres(m, mid(k)) < 120 for k in kept)
        if not twin:
            kept.append(h)
    return kept


def hole_rows(data: dict) -> list[dict]:
    els = data.get("elements", [])
    holes = collapse([e for e in els if (e.get("tags") or {}).get("golf") == "hole"])
    greens = [e for e in els if (e.get("tags") or {}).get("golf") == "green"
              and e.get("center")]
    greens_for = assign_greens(holes, greens)

    rows = []
    for hi, h in enumerate(holes):
        tg = h.get("tags") or {}
        geom = h.get("geometry") or []
        g = greens_for.get(hi)
        row: dict = {}
        ref = tg.get("ref")
        try:
            row["hole"] = int(str(ref).strip())
        except (TypeError, ValueError):
            row["hole"] = None
        for key, tag in (("par", "par"), ("si", "handicap")):
            try:
                row[key] = int(str(tg[tag]).strip())
            except (KeyError, TypeError, ValueError):
                pass
        if geom:
            # the end furthest from the green is the tee
            if g:
                gc = (g["center"]["lat"], g["center"]["lon"])
                a, b = geom[0], geom[-1]
                tee = a if metres((a["lat"], a["lon"]), gc) > metres((b["lat"], b["lon"]), gc) else b
            else:
                tee = geom[0]
            pts = [(g["lat"], g["lon"]) for g in geom]
            if (tee["lat"], tee["lon"]) != pts[0]:
                pts.reverse()                      # always runs tee to green
            row["tee"] = [round(pts[0][0], 6), round(pts[0][1], 6)]
            row["len"] = round(sum(metres(pts[i], pts[i + 1])
                                   for i in range(len(pts) - 1)))
            row["line"] = [[round(a, 6), round(b, 6)] for a, b in simplify(pts)]
        if g:
            row["green"] = [round(g["center"]["lat"], 6), round(g["center"]["lon"], 6)]
        rows.append(row)

    rows.sort(key=lambda r: (r["hole"] is None, r["hole"] or 0))
    return rows


HEADER = (
    "# Hole geometry from OpenStreetMap, pulled by holes.py.\n"
    "#\n"
    "# Data (c) OpenStreetMap contributors, licensed ODbL. Keep the\n"
    "# attribution in the page footer wherever this is shown.\n"
    "#\n"
    "# tee / green are points, [lat, lon]; line is the hole centreline, tee\n"
    "# first, simplified to the few points that keep its shape; len is the\n"
    "# mapped hole length in metres. par and si come from the OSM 'par' and 'handicap' tags where a\n"
    "# mapper filled them in - blanks mean nobody has, not par 0. Almost\n"
    "# nobody tags handicap, so stroke index still comes off the paper card.\n"
    "#\n"
    "# osm is the course polygon the holes were read from, kept so a re-run\n"
    "# skips the lookup. Re-run holes.py to refresh; don't hand-edit.\n\n"
)


def save(courses: dict, osm: dict) -> None:
    OUT.write_text(HEADER + yaml.safe_dump({"courses": courses, "osm": osm},
                                           sort_keys=True, allow_unicode=True,
                                           default_flow_style=None))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", nargs="*", metavar="COURSE",
                    help="re-fetch: all courses, or just the ones named")
    ap.add_argument("--list", action="store_true",
                    help="show the OSM match-up and stop")
    ap.add_argument("--relist", action="store_true",
                    help="look up which courses exist again, instead of reusing the match-up")
    args = ap.parse_args()

    wanted = wanted_names()
    have: dict[str, dict] = {}
    known: dict = {}
    if OUT.exists():
        prev = yaml.safe_load(OUT.read_text()) or {}
        have = prev.get("courses") or {}
        known = prev.get("osm") or {}

    print(f"{len(wanted)} courses wanted, {len(have)} already in holes.yaml",
          file=sys.stderr)

    # The four region queries are the slowest part of a run and the answer
    # changes about as often as a course moves, so reuse it when we can.
    covered = set(known.get("matched") or {}) | set(known.get("unmatched") or [])
    if not args.relist and not set(wanted) - covered:
        matched = {k: dict(v) for k, v in known["matched"].items() if k in wanted}
        print(f"  reusing the stored match-up ({len(matched)} courses) - "
              f"--relist to look again", file=sys.stderr)
    else:
        print("finding golf courses in OSM", file=sys.stderr)
        matched = pair_up(course_list(), wanted)
        known = {"matched": matched,
                 "unmatched": [w for w in wanted if w not in matched]}
    print(f"  matched {len(matched)}/{len(wanted)}", file=sys.stderr)
    for w in sorted(matched):
        print(f"    {w:<32} <- {matched[w]['name']}", file=sys.stderr)
    unmatched = [w for w in wanted if w not in matched]
    if unmatched:
        print(f"  no OSM golf course polygon ({len(unmatched)}):", file=sys.stderr)
        for w in unmatched:
            print(f"    {w}", file=sys.stderr)
    if args.list:
        return 0

    centres = {n: m["at"] for n, m in matched.items()}

    print("\nfetching holes", file=sys.stderr)
    for name in sorted(matched):
        m = matched[name]
        prev = have.get(name, {})
        rows_before = prev.get("holes")
        done = rows_before is not None
        if done and rows_before and not any("line" in h for h in rows_before):
            done = False                           # pulled before lines existed
        # A count that isn't a whole number of nines means we got a partial
        # answer, so try again - but only until the fallback has had a go.
        # That includes finding nothing at all, or a course OSM simply hasn't
        # mapped would be refetched on every run forever.
        if done and len(rows_before) not in (9, 18, 27, 36) and not prev.get("box"):
            done = False
        if args.refresh is not None and (not args.refresh or name in args.refresh):
            done = False                           # asked for by name
        if done:
            print(f"  {name:<32} cached", file=sys.stderr)
            continue
        sel = f"{'rel' if m['type'] == 'rel' else 'way'}(id:{m['id']})"
        try:
            boxed = False
            data = overpass(HOLE_Q.format(sel=sel))
            rows = hole_rows(data)
            # Some polygons are drawn around a whole resort, so the area query
            # hands back two or three courses at once. Only then is it worth
            # second-guessing it, by keeping what is closest to this course.
            if len(rows) > 36:
                data["elements"] = own(data.get("elements", []), name, centres)
                rows = hole_rows(data)
                print(f"  {name:<32} (polygon covers neighbours, trimmed)", file=sys.stderr)
            # A partial answer usually means the polygon can't be turned into
            # a search area, or the mapper drew holes outside it.
            if len(rows) not in (9, 18, 27, 36):
                boxed = True
                lat, lon = m["at"]
                d = 0.015
                bbox = f"{lat-d:.4f},{lon-d:.4f},{lat+d:.4f},{lon+d:.4f}"
                time.sleep(2)
                data = overpass(BOX_Q.format(bbox=bbox))
                data["elements"] = own(data.get("elements", []), name, centres)
                boxrows = hole_rows(data)
                # Only take the box result if it is a believable course. It
                # reaches past the boundary by design, so at a club sharing a
                # fence with another it picks up the neighbour's holes - and
                # own() can only exclude neighbours we matched by name.
                if len(boxrows) in (9, 18, 27, 36) and len(boxrows) > len(rows):
                    rows = boxrows
                    print(f"  {name:<32} (via bounding box)", file=sys.stderr)
        except RuntimeError as e:
            print(f"  {name:<32} overpass failed: {e}", file=sys.stderr)
            continue
        have[name] = {"osm": f"{m['type']}/{m['id']}", "at": m["at"], "holes": rows}
        if boxed:
            have[name]["box"] = True               # fallback already tried
        save(have, known)                          # keep what we got
        print(f"  {name:<32} {len(rows):>2} holes "
              f"| par {sum('par' in r for r in rows)}"
              f" | si {sum('si' in r for r in rows)}"
              f" | green {sum('green' in r for r in rows)}", file=sys.stderr)
        time.sleep(2)                              # be kind to a shared service

    save(have, known)
    full = sum(1 for c in have.values() if len(c["holes"]) in (9, 18, 27, 36))
    print(f"\nwrote {OUT} - {len(have)} courses, {full} with a clean hole count",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
