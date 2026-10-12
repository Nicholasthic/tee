# S&N Go Golfing

Tells you when a four-ball opens up within ~45 minutes of the Gold Coast, over the next 7 days.

(The repo and scripts are still named `teetime-watch`; `S&N Go Golfing` is what the page calls itself.)

Scrapes the public MiClub timesheets that nearly every club in the region uses. No login, no booking automation — it finds the slot and hands you a link.

---

## First run (do this once, takes 10 minutes)

```bash
pip install -r requirements.txt
python scan.py --discover
```

This hits each club's public calendar and reports whether the host exists and what booking IDs it exposes. Expect roughly half the `candidate` entries in `clubs.yaml` to come back `DEAD` — I seeded those from the standard `{club}.miclub.com.au` pattern without being able to verify each one. Set `enabled: false` on anything that fails, or correct the host if you know the real URL.

Then check the parser is reading cells correctly:

```bash
python scan.py --dry-run --days 2
```

If the counts look wrong — every row showing 4 free, or none — dump the raw HTML and look at it:

```bash
python scan.py --debug "Palmer Gold Coast" --days 1
open debug/*.html
```

Find the `class` on a booked cell versus a free one, and add those strings to `TAKEN_HINTS` / `FREE_HINTS` near the top of `scan.py`. That's the one thing I couldn't verify from the outside — every live timesheet I could reach was for a past date and came back empty.

---

## Alerts

**Phone.** Install the ntfy app (iOS/Android, free, no account). Subscribe to a topic — pick something nobody would guess, like `nm-golf-4b-x7k2`. Then:

```bash
export NTFY_TOPIC="nm-golf-4b-x7k2"
```

Topics are public to anyone who knows the name, so treat it like a password. The notification deep-links straight to the club's booking page.

**Email.** Set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `ALERT_EMAIL`. Gmail needs an app password, not your account password.

Both fire only on *new* slots. `seen.json` tracks what's already been reported so you don't get pinged about the same 7:40am four times a day.

---

## Running it on a schedule

Push to GitHub, add the env vars above as repository secrets, and `.github/workflows/watch.yml` runs it every 2 hours between 6am and 8pm Brisbane time.

Budget note: with ~10 live clubs it's about 4 minutes per run, so roughly 1,400 minutes a month. A private repo gets 2,000 free, so it fits — but only just. Make the repo public and it's unlimited. Nothing in here is sensitive as long as your secrets stay in Settings → Secrets.

---

## Flags

```
--days 7          how far ahead to look
--players 4       minimum consecutive free spots
--max-drive 45    filter clubs by drive_min in clubs.yaml
--dry-run         print, don't notify, don't write state
--discover        verify hosts and list booking IDs
--debug "Club"    dump raw timesheet HTML
```

---

## Adding a club

Every club declares a `source`. It defaults to `miclub`, so existing entries need
no change.

```yaml
  - name: Some Golf Club            # miclub — the club's own timesheet
    host: https://someclub.miclub.com.au
    booking_resource_id: null       # null is fine; falls back to 3000000
    drive_min: 25

  - name: The Glades                # teeitup — the club's own booking site
    source: teeitup
    alias: thegladesgolfclub        # the subdomain of *.book.teeitup.com
    facility_id: 15365
    drive_min: 10
```

Then verify before switching it on:

```bash
python scan.py --discover
```

`--discover` checks every club in the file, including disabled ones — the whole
point is to test a club *before* you enable it.

**Which source?** A lot of clubs aren't on MiClub. If the MiClub host is `DEAD`
or `NO-PUB`, open the club's website and click "Book a tee time":

- Lands on `<something>.book.teeitup.com/?course=NNNNN` → `source: teeitup`,
  with `alias` from the subdomain and `facility_id` from `course`.
- Lands on `golfnow.com.au/tee-times/facility/NNNNN-...` → `source: golfnow`,
  `facility_id: NNNNN`.

Tee It Up and GolfNow are both NBC Sports Next products and share facility ids,
so the same number works for either.

**Use `golfnow`.** `teeitup` is the nicer API on paper — a plain GET, ~50x
smaller response, explicit group sizes, and links that book direct with the
club — but as of September 2026 its endpoint sits behind a Cloudflare bot
challenge and returns a "Just a moment..." page to anything that isn't a
browser. The adapter is still there in case that lifts; until it does,
`golfnow` reads the same facility ids and works.

Clubs outside Queensland should set `timezone:` (e.g. `Australia/Sydney`), since
NSW observes daylight saving and Queensland doesn't.

### Clubs you can't scan

Some public courses have no tee sheet worth scraping — phone-only, or on a
platform with no adapter. Rather than vanish from the page, they get a
call-to-book card under **No online sheet**:

```yaml
  - name: Southport Golf Club
    drive_min: 25
    enabled: false                  # nothing to scan
    public: true                    # required — private clubs are never listed
    phone: "+61755711444"           # becomes a tap-to-dial link
    phone_display: (07) 5571 1444
    book_url: https://...           # optional "Book online" button
    note: Books through Chronogolf
```

`public: true` is the gate, so members-only clubs never appear. The card is
suppressed for `status: closed`, respects the max-drive filter, and ignores the
day/time/group filters — there are no times to filter on. It still shows when
nothing else is open, which is exactly when you want a phone number.

Don't add a club here without checking the number: a wrong one is worse than
no card at all.

---

## Courses played

The page has two tabs: **Tee times** and **Courses**. The Courses tab is a
checklist of every real course in `clubs.yaml` — including the ones the
scanner can't reach, since you can still go and play them — with a single
tick per course, counts of played and still-to-play, and All / To play /
Played filters.

It's one list for the pair: either you've both played a course or you
haven't. `played.yaml` is the shared record:

```yaml
played:
  - Palmer Colonial
  - Coolangatta & Tweed Heads
```

The checklist is deliberately wider than the scan list. `clubs.yaml` only
holds places we can pull tee times from, so private, resort and out-of-radius
courses would never appear — but you can still play them. Add those under
`courses:` in `played.yaml`:

```yaml
courses:
  - name: Lakelands Golf Club
    drive_min: 10
    note: members and guests
```

They show on the checklist and are never scanned. Names ticked in `played:`
must match a course exactly; the build warns on unknown names and on any
course listed twice. Clubs marked `closed` or `unverified` are left off.

### Sharing the list between devices

**Reading is free.** `played.yaml` lives in a public repo, so every device
pulls the shared list on load with no token and no setup. Open the page on any
phone and you see the same ticks. Local ticks sit on top of it, and any the
file has since caught up with stop counting as local.

**Writing needs a token.** To have your ticks save for both of you, turn on
sync from the Courses tab:

1. Create a **fine-grained personal access token** at
   <https://github.com/settings/personal-access-tokens/new>
2. *Repository access* → **Only select repositories** → this repo
3. *Repository permissions* → **Contents: Read and write**
4. Paste it into the page once, on that device

Ticking then commits `played.yaml` directly, so the other phone picks it up on
its next load. Every round ends up in git history.

Only one of you strictly needs a token — whoever does the ticking. For both to
tick, **the second person must be a collaborator on the repo** (Settings →
Collaborators) and use a token from their own GitHub account. Don't share one
token between you.

The token is stored in that browser's `localStorage` and is sent only to
`api.github.com`. It never goes in the repo or the page. Revoke it any time and
the page falls back to read-only sharing.

Ticks are optimistic: the box flips immediately and the commit happens after.
If the commit fails the tick is kept locally and the bar shows why, so you
don't lose it.

There's no free no-auth JSON store worth relying on any more — jsonblob,
textdb, kvdb and extendsclass were all either Cloudflare-blocked, dead, or
require an account. ntfy works and has CORS, but free topics only retain
messages for 12 hours, so it can't hold state.

---

## Rounds and handicap

A third tab, **Rounds**. Enter a card hole by hole and it keeps a running
handicap for each of you.

The maths is the real World Handicap System, not an approximation:

- **Score Differential** = `(113 / Slope) x (Adjusted Gross - Course Rating)`
- **Adjusted Gross** caps every hole at **net double bogey** — par + 2 + any
  strokes you get there. With no index yet the cap is par + 5, which is what
  WHS says to use for a first card.
- **Handicap Index** is the average of the lowest differentials of your last
  20 rounds, using the short-record table below 20 (3 rounds: lowest 1 less
  2.0; 6 rounds: lowest 2 less 1.0; 20 rounds: lowest 8). Truncated, never
  rounded — 12.39 is 12.3.
- Rounds are processed in date order, and each is capped using the index you
  held **before** you played it, which is what the rules require.

It is not an official Golf Australia handicap. It is the same arithmetic on
your own scores.

### Course data

`courses.yaml` holds the scorecard:

```yaml
cards:
  Palmer Colonial:
    par: [4,4,3,5,4,4,3,4,5, 4,4,3,5,4,4,3,4,5]
    si:  [5,1,17,11,3,7,15,9,13, 6,2,18,12,4,8,16,10,14]
    tees:
      white: {cr: 70.1, slope: 125}
```

Everything there is printed on the card in the pro shop. A course with no
entry falls back to a generic par 72 and the app says so — that placeholder
makes birdie counts and the net double bogey cap wrong, so correct it.

Easiest way in is **Course cards** on the Rounds tab: pick a course, type the
par and stroke index rows off the card, add a tee with its rating and slope.
Stroke index is validated as you type — it has to be 1 to 18, once each — and
it saves to `courses.yaml` through the same sync as everything else.

Don't take ratings off the golf aggregator sites. They disagree with each
other: for Palmer Colonial one lists Course Rating 71.0 and another 73.2 for
the same tee, and they don't agree on par either. A wrong rating shifts every
differential, so the number on the physical card is the one to use.

**Course Rating and Slope have no sensible default.** A round at an unrated
course is still recorded and shown, but it cannot count toward an index, and
the round is marked `course not rated` rather than quietly guessing.

### Yardages

`holes.py` pulls hole geometry from OpenStreetMap into `holes.yaml` — the
centre of each green, the hole centreline, par where a mapper tagged it. With
that in the repo, the scorecard grows a **Use GPS** button that shows how far
you are from the middle of the green.

```bash
python3 holes.py            # fetch anything missing
python3 holes.py --list     # just show which OSM course matched which club
python3 holes.py --refresh  # re-fetch everything
python3 holes.py --refresh "The Glades"      # just this one
```

A course OSM has no holes for is remembered as such, so a plain re-run
doesn't ask about it again. Map one yourself and you want the named
`--refresh` to pick it up.

Run it by hand, not from the workflow. Layouts change about never, and
Overpass is a shared service that spends a good part of the day returning
504s — the script backs off, tries three mirrors, and saves after every
course, so if it dies halfway you just run it again. Committing the result
also means the app isn't waiting on Overpass while you're standing on a tee
with one bar of signal.

What it can't give you:

- **Stroke index.** Almost nobody tags `handicap` in OSM. Still a paper-card job.
- **Course Rating and Slope.** Not in OSM at all.
- **Front and back of the green.** Only the centre is mapped, so only the
  centre is shown. No invented numbers.
- **Clubs with two courses under one name.** Coolangatta & Tweed Heads and
  Royal Pines come back as one polygon holding two sets of holes numbered
  1–18, and there is no way to tell which is which. Those are skipped.

**On the course, press Play.** The scorecard grid is for writing a round up
afterwards; Play gives you one hole at a time — distance to the middle of
the green in letters you can read in the sun, par and length, and a pair of
thumb-sized buttons for the score. It follows you round: the hole changes as
you walk to the next tee, and the arrows pin it by hand if you want to look
ahead. It also asks the browser to keep the screen on, because a screen that
dies every thirty seconds for four hours is the difference between using
this and not.

Where a course has no card saved yet, Play shows the par off the hole map
rather than the generic placeholder — otherwise it would call a 500m hole a
par 3 — and says so underneath.

The GPS watch is opt-in per round and stops the moment you leave the
scorecard — `watchPosition` is the most expensive thing a phone can do in
your pocket for four hours.

Hole maps are © OpenStreetMap contributors, licensed
[ODbL](https://opendatacommons.org/licenses/odbl/). The page carries that
credit in the footer whenever it ships with hole data.

### Limits

- **18-hole rounds only** count toward the index. Nine-hole rounds can be
  recorded but are excluded — WHS combines two nines into one differential
  and that isn't built yet.
- **PCC is 0.** The daily playing-conditions adjustment can only be issued by
  a governing body.

Rounds save to `rounds.yaml` through the same sync as `played.yaml`: on this
device until you turn sync on, then committed straight to the repo.

---

## The page

`build.py` scans every club and writes a single self-contained `docs/index.html` — no
server, no build step, no external requests. All results are embedded as JSON and
filtered in the browser, so switching days or dragging the time window is instant.

```bash
python build.py --days 7 --max-drive 45     # scan, then write the page
python build.py --offline                   # rewrite the page from the last scan
python build.py --offline --open            # ...and open it
```

Use `--offline` when you're changing the layout. It re-renders the template against
the records already embedded in `docs/index.html`, keeps the original scan timestamp,
and doesn't touch the clubs.

If a club errors during a scan it used to just vanish from the page, which is
how The Glades stayed broken for weeks. The footer now names any club that
failed and why, so a silent failure is visible on the page itself rather than
only in the workflow log.

The page groups by day, then by club. Each club gets a timeline of the whole day with
every open slot marked — clusters and gaps are obvious at a glance — plus the actual
times as links straight to that club's booking sheet. The red pin is the earliest slot.
Filters (group size, tee window, max drive, sort) persist in `localStorage`, and the
theme follows the OS unless you toggle it.

`.github/workflows/build.yml` runs the scan three times a day and commits the page,
which GitHub Pages serves from `docs/`.

---

## Notes

- Requests are spaced 1.5s apart. Don't lower that. These are small club servers and the whole point is to stay unremarkable in their logs.
- Some clubs sell explicit "4 Player Package" fee groups, which show up as their own entry in `--discover`. Those are the easiest wins.
- If a club comes back `NO-PUB`, its public inventory probably isn't on MiClub at all. Try the `teeitup` source — see above.
- The next platform worth an adapter is **Chronogolf** (Lightspeed). Southport books through it, and it's the common second listing for clubs that are also on GolfNow. Everything else nearby is now covered.
- Arundel Hills closed in May 2022. It's kept in `clubs.yaml` as a tombstone with `status: closed` so it doesn't get re-added from a stale course list.
- Booking stays manual by design. Twenty-odd clubs each with their own account, payment flow and dress-code T&Cs is a lot of fragile surface area for no real gain over tapping a link.
