# getCourt — Padel court watcher 🎾

Watches **[Racket Club Kløver](https://padelmates.se/club/racketclubklover)** on
PadelMates and sends a phone push (via [ntfy](https://ntfy.sh)) the moment a
**06:00–07:30 (90 min)** slot opens up on **courts 01–06** for a target date.

A scheduled GitHub Action runs every 10 minutes — no laptop needed — and
publishes a live status page: **https://asracwl.github.io/getCourt/**

## How it works

PadelMates exposes a public availability API (no login required):

```
GET https://fastapi-production-fargate.padelmates.io/player/player_booking/all_courts_slot_prices_v2
    ?club_id=PDXpw2Hh4ZaSI6sTxslHS7tpelV2
    &start_datetime=<UTC ms>&end_datetime=<UTC ms>
```

It returns **only the bookable duration options** for each court + start time.
So if a **90-minute** option exists at **06:00**, then **06:00–07:30 is free**.
When someone books 07:00–07:30, the 90-min option disappears and only the
60-min (06:00–07:00) one remains — which is the state we wait to clear.

[`check_court.py`](check_court.py) queries the target day, looks for a 90-min
06:00 slot on courts 01–06, and pushes to ntfy on transition (closed → open).
State + a rolling signal log are kept in [`docs/status.json`](docs/status.json)
(which also feeds the [status page](docs/index.html)) so you aren't spammed; while a slot
stays open it re-pings every `RENOTIFY_HOURS` (default 3h) so you don't miss it.

## Booking horizon

The club opens bookings ~**14 days** ahead. A future date returns **zero slots**
until its window opens, then fills up. The watcher stays quiet until the date
opens, then alerts as soon as 06:00–07:30 is free (usually right when it opens).

## Get notified

1. Install the **ntfy** app ([iOS](https://apps.apple.com/app/ntfy/id1625396347) /
   [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)) or open
   [ntfy.sh](https://ntfy.sh) in a browser.
2. Subscribe to the topic name stored in the repo's `NTFY_TOPIC` secret
   (the one set up for you — keep it private; anyone with the topic can read pushes).
3. Done. You'll get a push titled **"🎾 06:00-07:30 court is OPEN!"** with a tap
   action to open the booking page.

## Configure

Edit the `env:` block in [`.github/workflows/watch.yml`](.github/workflows/watch.yml):

| Var | Default | Meaning |
|-----|---------|---------|
| `TARGET_DATE` | `2026-06-10` | Date to watch (YYYY-MM-DD, club local time) |
| `START_LOCAL` | `06:00` | Slot start (local) |
| `DURATION_MIN` | `90` | 90 = 06:00–07:30 |
| `COURT_PREFIXES` | `01,02,03,04,05,06` | Court number prefixes to accept |
| `RENOTIFY_HOURS` | `3` | Re-ping interval while still open (0 = once only) |

The `NTFY_TOPIC` is a repo **Actions secret**, not in the code.

## Run / test manually

- GitHub → **Actions → Watch padel court → Run workflow** → `mode: test`
  sends a test push to confirm your subscription works.
- Locally: `NTFY_TOPIC=your-topic TARGET_DATE=2026-06-10 python3 check_court.py`
  (omit `NTFY_TOPIC` for a dry run that only prints availability).

## Stop it

Disable the schedule in **Actions → Watch padel court → ⋯ → Disable workflow**,
or delete the repo, once you've booked.
