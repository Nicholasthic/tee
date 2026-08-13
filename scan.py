#!/usr/bin/env python3
"""
teetime-watch
Finds 4-ball openings across MiClub golf clubs and pings you when one appears.

Usage:
    python scan.py --discover          # verify hosts, find resource + fee group IDs
    python scan.py                     # scan and notify on new 4-balls
    python scan.py --dry-run           # scan and print, no notifications, no state write
    python scan.py --debug "Palmer Gold Coast"   # dump raw timesheet HTML for tuning
"""

from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "seen.json"
DEBUG_DIR = ROOT / "debug"

CALENDAR_PATH = "/guests/bookings/ViewPublicCalendar.msp"
TIMESHEET_PATH = "/guests/bookings/ViewPublicTimesheet.msp"

# Tee It Up and GolfNow are both NBC Sports Next products and share facility
# ids, so one `facility_id` in clubs.yaml serves either adapter. Prefer
# teeitup: it is a plain GET, the response is ~50x smaller, it states allowed
# player counts explicitly, and it books direct with the club.
TEEITUP_API = "https://phx-api-be-east-1b.kenna.io/v2/tee-times"
TEEITUP_BOOK = "https://{alias}.book.teeitup.com/?course={facility}&date={day}"
GOLFNOW_API = "https://www.golfnow.com.au/api/tee-times/tee-time-search-results"
GOLFNOW_SUMMARY = ("https://www.golfnow.com.au/api/tee-times/tee-times"
                   "/facility/{facility}/summaries/from/{start}/to/{end}")
GOLFNOW_BOOK = "https://www.golfnow.com.au"

DEFAULT_TZ = "Australia/Brisbane"

# GolfNow states availability as an enum rather than a count.
PLAYER_RULES = {
    "one": 1, "two": 2, "three": 3, "four": 4,
    "onetwo": 2, "onetwothree": 3, "onetwothreefour": 4,
    "twothree": 3, "twothreefour": 4, "threefour": 4, "twofour": 4,
    "any": 4,
}

HEADERS = {
    "User-Agent": "teetime-watch/1.0 (personal tee time checker)",
    "Accept": "text/html,application/xhtml+xml",
}
JSON_HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}

# Be a good citizen. These pages are small; there is no reason to hammer them.
REQUEST_DELAY = 1.5
TIMEOUT = 25

TIME_RE = re.compile(r"(\d{1,2})[:.](\d{2})\s*([AaPp][Mm])?")
FEE_GROUP_RE = re.compile(r"feeGroupId-(\d+)")
ROW_TIME_RE = re.compile(r"\brow-time\b")
ROW_HEADING_RE = re.compile(r"\brow-heading\b")
CELL_RE = re.compile(r"\bcell\b")

# Confirmed against a live Palmer Gold Coast timesheet.
FREE_CLASS = "cell-available"
TAKEN_CLASS = "cell-taken"

# Nearly every MiClub club uses this for its main course.
DEFAULT_RESOURCE_ID = "3000000"

# A four-ball means a full round. Skip 9-hole and twilight products by default:
# they roughly triple the request count for rounds you probably won't play.
SKIP_FEE_WORDS = ("9 hole", "9-hole", "nine hole", "twilight", "par 3", "par-3",
                  "sunset", "3pm", "pre-twilight")


# ----------------------------------------------------------------------------
# models
# ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Opening:
    club: str
    drive_min: int
    day: str          # ISO date
    tee_time: str
    free: int
    fee_label: str
    url: str
    total: int = 0

    @property
    def key(self) -> str:
        return f"{self.club}|{self.day}|{self.tee_time}|{self.fee_label}"

    @property
    def is_empty_row(self) -> bool:
        """Nobody on the row at all — you won't be paired with strangers."""
        return self.total > 0 and self.free == self.total

    def line(self) -> str:
        tag = "" if self.is_empty_row else f" of {self.total}"
        return (f"{self.club} — {self.day} {self.tee_time} "
                f"({self.free}{tag} spots, {self.fee_label}, ~{self.drive_min}min)")


# ----------------------------------------------------------------------------
# http
# ----------------------------------------------------------------------------

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def get(session: requests.Session, url: str) -> str | None:
    try:
        r = session.get(url, timeout=TIMEOUT)
        time.sleep(REQUEST_DELAY)
        if r.status_code != 200:
            return None
        return r.text
    except requests.RequestException:
        return None


def fetch_json(session: requests.Session, url: str, *, payload: dict | None = None,
               headers: dict | None = None):
    """GET, or POST when `payload` is given. Same politeness delay as get()."""
    h = dict(JSON_HEADERS)
    if headers:
        h.update(headers)
    try:
        if payload is None:
            r = session.get(url, headers=h, timeout=TIMEOUT)
        else:
            r = session.post(url, headers=h, json=payload, timeout=TIMEOUT)
        time.sleep(REQUEST_DELAY)
        if r.status_code != 200:
            return None
        return r.json()
    except (requests.RequestException, json.JSONDecodeError):
        return None


def club_zone(club: dict) -> ZoneInfo | timezone:
    """Queensland has no daylight saving; northern NSW does. Let clubs differ."""
    try:
        return ZoneInfo(club.get("timezone", DEFAULT_TZ))
    except Exception:                       # missing tzdata on a bare runner
        return timezone(timedelta(hours=10))


def utc_to_local(iso: str, zone) -> tuple[str, str]:
    """'2026-08-14T01:56:00.000Z' -> ('2026-08-14', '11:56') in club-local time.

    Tee It Up timestamps are genuinely UTC.
    """
    stamp = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(zone)
    return stamp.date().isoformat(), stamp.strftime("%H:%M")


def wall_time(iso: str) -> tuple[str, str]:
    """'2026-08-16T10:08:00+00:00' -> ('2026-08-16', '10:08').

    GolfNow sends local wall time but stamps it '+00:00'. Converting it as if
    it were UTC would shift every tee time by the club's offset, so read the
    clock face as written and ignore the zone entirely.
    """
    stamp = datetime.fromisoformat(iso).replace(tzinfo=None)
    return stamp.date().isoformat(), stamp.strftime("%H:%M")


# ----------------------------------------------------------------------------
# parsing
# ----------------------------------------------------------------------------

def is_full_round(label: str) -> bool:
    low = label.lower()
    return not any(w in low for w in SKIP_FEE_WORDS)


def find_fee_groups(html: str) -> list[dict]:
    """MiClub encodes fee group IDs in CSS class names, not in hrefs.

    A calendar row looks like:
        <div class="row feeGroupRow feeGroupId-1501539023 eighteenHoles">
          <div class="... row-heading"><h3>18 Holes Walking</h3></div>
    """
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, dict] = {}

    for div in soup.find_all(class_=FEE_GROUP_RE):
        m = FEE_GROUP_RE.search(" ".join(div.get("class", [])))
        if not m:
            continue
        fee_id = m.group(1)
        if fee_id in found:
            continue

        heading = div.find(class_=ROW_HEADING_RE)
        label = heading.get_text(" ", strip=True) if heading else ""
        # Strip a trailing price if the heading includes one.
        label = re.sub(r"\$[\d,.]+", "", label).strip(" -–|") or "Golf"

        found[fee_id] = {"fee_group": fee_id, "label": label[:60]}

    return list(found.values())


def parse_timesheet(html: str) -> list[tuple[str, int, int]]:
    """Return (tee_time, free_cells, total_cells) for each playable row.

    The timesheet is div-based, not a table. Rows carry `row-time`, the time
    lives in the `row-heading` child, and player slots are `cell cell-available`
    or `cell cell-taken`.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows: list[tuple[str, int, int]] = []

    for row in soup.find_all(class_=ROW_TIME_RE):
        heading = row.find(class_=ROW_HEADING_RE)
        if not heading:
            continue

        m = TIME_RE.search(heading.get_text(" ", strip=True))
        if not m:
            continue

        hour = int(m.group(1))
        minute = m.group(2)
        meridiem = (m.group(3) or "").lower()
        if meridiem == "pm" and hour != 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        tee_time = f"{hour:02d}:{minute}"

        cells = row.find_all(class_=CELL_RE)
        free = 0
        total = 0
        for c in cells:
            classes = " ".join(c.get("class", []))
            if FREE_CLASS in classes:
                free += 1
                total += 1
            elif TAKEN_CLASS in classes:
                total += 1

        if total:
            rows.append((tee_time, free, total))

    return rows


# ----------------------------------------------------------------------------
# scanning
# ----------------------------------------------------------------------------

def build_timesheet_url(host: str, resource: str, fee: str | None, day: date) -> str:
    url = f"{host.rstrip('/')}{TIMESHEET_PATH}?bookingResourceId={resource}&selectedDate={day.isoformat()}"
    if fee:
        url += f"&feeGroupId={fee}"
    return url


def scan_miclub(session, club: dict, days: int, min_players: int,
                debug_name: str | None, include_all_fees: bool = False) -> tuple[list[Opening], str | None]:
    host = club["host"].rstrip("/")
    name = club["name"]

    cal_url = f"{host}{CALENDAR_PATH}"
    if club.get("booking_resource_id"):
        cal_url += f"?bookingResourceId={club['booking_resource_id']}"

    cal_html = get(session, cal_url)
    if not cal_html:
        return [], f"{name}: calendar unreachable"

    resource = club.get("booking_resource_id") or DEFAULT_RESOURCE_ID

    combos = find_fee_groups(cal_html)
    if not combos:
        return [], f"{name}: no fee groups found on calendar"

    if not include_all_fees:
        filtered = [c for c in combos if is_full_round(c["label"])]
        if filtered:
            combos = filtered

    openings: list[Opening] = []
    today = date.today()

    for combo in combos:
        print(f"    {name}: {combo['label']}", flush=True)
        for offset in range(days + 1):
            day = today + timedelta(days=offset)
            url = build_timesheet_url(host, resource, combo["fee_group"], day)
            html = get(session, url)
            if not html:
                continue

            if debug_name and debug_name.lower() in name.lower():
                DEBUG_DIR.mkdir(exist_ok=True)
                safe = re.sub(r"[^a-z0-9]+", "-", name.lower())
                out = DEBUG_DIR / f"{safe}-{day.isoformat()}-{combo['fee_group']}.html"
                out.write_text(html, encoding="utf-8")

            for tee_time, free, total in parse_timesheet(html):
                if free >= min_players:
                    openings.append(Opening(
                        club=name,
                        drive_min=club.get("drive_min", 0),
                        day=day.isoformat(),
                        tee_time=tee_time,
                        free=free,
                        fee_label=combo["label"],
                        url=url,
                        total=total,
                    ))

    return openings, None


def scan_teeitup(session, club: dict, days: int, min_players: int,
                 debug_name: str | None, include_all_fees: bool = False) -> tuple[list[Opening], str | None]:
    """A club's own Tee It Up booking site.

    One GET per day. `allowedPlayers` is an explicit list of the group sizes
    the club will sell for that slot, so the largest entry is how many spots
    are actually free — no guessing from markup.
    """
    alias, facility = club.get("alias"), club.get("facility_id")
    name = club["name"]
    if not alias or not facility:
        return [], f"{name}: teeitup needs both `alias` and `facility_id`"

    zone = club_zone(club)
    openings: list[Opening] = []
    reached = False

    for offset in range(days + 1):
        day = date.today() + timedelta(days=offset)
        data = fetch_json(
            session,
            f"{TEEITUP_API}?date={day.isoformat()}&facilityIds={facility}",
            headers={"x-be-alias": alias},
        )
        if not data:
            continue
        reached = True

        book = TEEITUP_BOOK.format(alias=alias, facility=facility, day=day.isoformat())

        for block in data:
            for slot in block.get("teetimes") or []:
                for rate in slot.get("rates") or []:
                    label = rate.get("name") or "18 Holes"
                    if not include_all_fees and not is_full_round(label):
                        continue

                    allowed = [p for p in (rate.get("allowedPlayers") or [])
                               if isinstance(p, int)]
                    free = max(allowed) if allowed else 0
                    if free < min_players:
                        continue

                    iso_day, tee_time = utc_to_local(slot["teetime"], zone)
                    openings.append(Opening(
                        club=name,
                        drive_min=club.get("drive_min", 0),
                        day=iso_day,
                        tee_time=tee_time,
                        free=free,
                        fee_label=label,
                        url=book,
                        total=4,
                    ))

    if not reached:
        return [], f"{name}: teeitup api unreachable (alias {alias!r})"
    return openings, None


def golfnow_payload(facility: int, day: date) -> dict:
    return {
        "pageSize": 100, "pageNumber": 0,
        "date": day.strftime("%b %d %Y"),
        "sortBy": "Date", "sortDirection": 0,
        "facilityId": int(facility), "searchType": "Facility", "view": "List",
        "holes": "Any", "players": 0, "priceMin": 0, "priceMax": 10000,
        "timePeriod": "Any", "timeMin": 0, "timeMax": 48,
        "rateType": "all", "radius": 100,
    }


def scan_golfnow(session, club: dict, days: int, min_players: int,
                 debug_name: str | None, include_all_fees: bool = False) -> tuple[list[Opening], str | None]:
    """GolfNow AU — the fallback for facilities with no Tee It Up front end.

    A single summaries call reports how many tee times exist per day, so days
    with nothing available are skipped instead of fetched. Each detail response
    is heavy (~400KB), which makes that pruning worth doing.
    """
    facility = club.get("facility_id")
    name = club["name"]
    if not facility:
        return [], f"{name}: golfnow needs `facility_id`"

    today = date.today()
    end = today + timedelta(days=days)

    summary = fetch_json(session, GOLFNOW_SUMMARY.format(
        facility=facility, start=today.isoformat(), end=end.isoformat()))
    if summary is None:
        return [], f"{name}: golfnow api unreachable (facility {facility})"

    live = {
        row["playDateUtc"][:10]
        for row in summary
        if (row.get("numberOfTeeTimesAvailable") or 0) > 0
    }
    if not live:
        return [], None

    openings: list[Opening] = []

    for offset in range(days + 1):
        day = today + timedelta(days=offset)
        if day.isoformat() not in live:
            continue

        data = fetch_json(session, GOLFNOW_API, payload=golfnow_payload(facility, day))
        if not data:
            continue

        for slot in (data.get("ttResults") or {}).get("teeTimes") or []:
            label = (slot.get("teeTimeRates") or [{}])[0].get("name") or "18 Holes"
            if not include_all_fees and not is_full_round(label):
                continue

            free = PLAYER_RULES.get(str(slot.get("playerRule", "")).lower(), 0)
            if free < min_players:
                continue

            iso_day, tee_time = wall_time(slot["time"]["date"])
            openings.append(Opening(
                club=name,
                drive_min=club.get("drive_min", 0),
                day=iso_day,
                tee_time=tee_time,
                free=free,
                fee_label=label,
                url=GOLFNOW_BOOK + (slot.get("detailUrl") or ""),
                total=4,
            ))

    return openings, None


SOURCES = {
    "miclub": scan_miclub,
    "teeitup": scan_teeitup,
    "golfnow": scan_golfnow,
}


def scan_club(session, club: dict, days: int, min_players: int,
              debug_name: str | None, include_all_fees: bool = False) -> tuple[list[Opening], str | None]:
    """Dispatch to the right adapter. Clubs default to miclub."""
    source = club.get("source", "miclub")
    adapter = SOURCES.get(source)
    if adapter is None:
        return [], f"{club['name']}: unknown source {source!r} (try {'/'.join(SOURCES)})"
    return adapter(session, club, days, min_players, debug_name, include_all_fees)


# ----------------------------------------------------------------------------
# state
# ----------------------------------------------------------------------------

def load_state() -> set[str]:
    if not STATE_FILE.exists():
        return set()
    try:
        data = json.loads(STATE_FILE.read_text())
        cutoff = date.today().isoformat()
        return {k for k, d in data.items() if d >= cutoff}
    except (json.JSONDecodeError, OSError):
        return set()


def save_state(keys: dict[str, str]) -> None:
    STATE_FILE.write_text(json.dumps(keys, indent=1, sort_keys=True))


# ----------------------------------------------------------------------------
# notifications
# ----------------------------------------------------------------------------

def notify_ntfy(topic: str, openings: list[Opening]) -> None:
    if not topic:
        return
    head = openings[0]
    title = f"{len(openings)} four-ball{'s' if len(openings) > 1 else ''} open"
    body = "\n".join(o.line() for o in openings[:12])
    if len(openings) > 12:
        body += f"\n… and {len(openings) - 12} more"
    try:
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=body.encode("utf-8"),
            headers={
                "Title": title,
                "Tags": "golf",
                "Priority": "default",
                "Click": head.url,
            },
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"ntfy failed: {e}", file=sys.stderr)


def notify_email(openings: list[Opening]) -> None:
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    to_addr = os.environ.get("ALERT_EMAIL")
    if not all([host, user, password, to_addr]):
        return

    lines = []
    for o in openings:
        lines.append(f"{o.line()}\n  {o.url}\n")

    msg = EmailMessage()
    msg["Subject"] = f"Four-ball open: {openings[0].club} {openings[0].day} {openings[0].tee_time}"
    msg["From"] = user
    msg["To"] = to_addr
    msg.set_content("\n".join(lines))

    try:
        with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", 587)), timeout=30) as s:
            s.starttls()
            s.login(user, password)
            s.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        print(f"email failed: {e}", file=sys.stderr)


# ----------------------------------------------------------------------------
# discover
# ----------------------------------------------------------------------------

def discover(session, clubs: list[dict]) -> None:
    print("Verifying club hosts and finding booking IDs…\n")
    for club in clubs:
        source = club.get("source", "miclub")
        flag = "" if club.get("enabled", True) else "  [disabled]"

        if source in ("teeitup", "golfnow"):
            found, err = SOURCES[source](session, club, 2, 1, None, True)
            if err:
                print(f"  DEAD    {club['name']:<34} {err}")
            elif not found:
                print(f"  EMPTY   {club['name']:<34} reachable, nothing open in 2 days{flag}")
            else:
                labels = sorted({o.fee_label for o in found})
                print(f"  OK      {club['name']}   ({source}, {len(found)} slots){flag}")
                for lab in labels[:6]:
                    print(f"            rate: {lab}")
            continue

        host = club["host"].rstrip("/")
        html = get(session, f"{host}{CALENDAR_PATH}")
        if not html:
            print(f"  DEAD    {club['name']:<34} {host}")
            continue
        combos = find_fee_groups(html)
        if not combos:
            print(f"  NO-PUB  {club['name']:<34} reachable, but no public fee groups")
            continue
        rid = club.get("booking_resource_id") or "?? set this"
        print(f"  OK      {club['name']}   (resource {rid})")
        for c in combos:
            print(f"            feeGroupId: {c['fee_group']:<14} {c['label']}")
    print("\nSet enabled: false in clubs.yaml for anything marked DEAD or NO-PUB.")
    print("For OK clubs showing '?? set this', find the resource id in the club's")
    print("booking page URL (bookingResourceId=...) and add it to clubs.yaml.")


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="Find 4-ball tee times nearby.")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--players", type=int, default=4)
    p.add_argument("--max-drive", type=int, default=45, help="minutes")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--discover", action="store_true")
    p.add_argument("--debug", metavar="CLUB", help="dump raw timesheet HTML for a club")
    p.add_argument("--all-fees", action="store_true",
                   help="include 9-hole, twilight and sunset products too")
    args = p.parse_args()

    config = yaml.safe_load((ROOT / "clubs.yaml").read_text())
    session = make_session()

    # --discover deliberately ignores `enabled`: the whole point is to check a
    # club before you switch it on, and a disabled club is exactly the one you
    # need to verify.
    if args.discover:
        discover(session, [c for c in config["clubs"]
                           if c.get("drive_min", 0) <= args.max_drive])
        return 0

    clubs = [c for c in config["clubs"]
             if c.get("enabled", True) and c.get("drive_min", 0) <= args.max_drive]

    all_openings: list[Opening] = []
    problems: list[str] = []

    for club in clubs:
        print(f"  scanning {club['name']}…", flush=True)
        found, err = scan_club(session, club, args.days, args.players,
                               args.debug, args.all_fees)
        if err:
            problems.append(err)
        all_openings.extend(found)

    all_openings.sort(key=lambda o: (o.day, o.tee_time, o.drive_min))

    for msg in problems:
        print(f"  ! {msg}", file=sys.stderr)

    if not all_openings:
        print("No four-balls found.")
        return 0

    seen = load_state()
    fresh = [o for o in all_openings if o.key not in seen]

    print(f"{len(all_openings)} four-ball slots found, {len(fresh)} new.\n")
    for o in all_openings:
        marker = "NEW " if o.key in {f.key for f in fresh} else "    "
        print(f"{marker}{o.line()}")

    if args.dry_run:
        return 0

    if fresh:
        notify_ntfy(os.environ.get("NTFY_TOPIC", ""), fresh)
        notify_email(fresh)

    save_state({o.key: o.day for o in all_openings})
    return 0


if __name__ == "__main__":
    sys.exit(main())
