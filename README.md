# teetime-watch

Tells you when a four-ball opens up within ~45 minutes of the Gold Coast, over the next 7 days.

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

## Notes

- Requests are spaced 1.5s apart. Don't lower that. These are small club servers and the whole point is to stay unremarkable in their logs.
- Some clubs sell explicit "4 Player Package" fee groups, which show up as their own entry in `--discover`. Those are the easiest wins.
- Emerald Lakes and a few others also list on GolfNow AU. If a club comes back `NO-PUB`, that's usually why — its public inventory sits on GolfNow rather than its own MiClub timesheet. Worth adding a GolfNow adapter later if it turns out to matter.
- Booking stays manual by design. Twenty-odd clubs each with their own account, payment flow and dress-code T&Cs is a lot of fragile surface area for no real gain over tapping a link.
