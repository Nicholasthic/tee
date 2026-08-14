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
so the same number works for either. **Prefer `teeitup`**: it's a plain GET, the
response is ~50x smaller, it states allowed group sizes explicitly instead of as
an enum, and its links book direct with the club rather than through the
aggregator. Use `golfnow` only for facilities with no Tee It Up front end.

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
