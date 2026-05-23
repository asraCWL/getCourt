#!/usr/bin/env python3
"""
Padel court watcher for Racket Club Kløver (padelmates.se).

Checks the public PadelMates availability API and sends an ntfy push when a
06:00-07:30 (90 min) slot opens up on any of courts 01-06 for the target date.

The PadelMates API returns ONLY the bookable duration options for each court +
start time. So a 90-minute option at 06:00 existing == 06:00-07:30 is free.
When 07:00-07:30 gets booked, the 90-min option disappears and only the 60-min
(06:00-07:00) one remains -- which is the situation we are waiting to clear.

All times are handled in the club's local timezone (Europe/Copenhagen) and
converted to the UTC millisecond timestamps the API expects.

Config via environment variables (sane defaults below). State is persisted to
state.json so we only push on transitions (and optionally re-ping if still open).
"""
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request

# ---- Config -----------------------------------------------------------------
CLUB_ID      = os.environ.get("CLUB_ID", "PDXpw2Hh4ZaSI6sTxslHS7tpelV2")
CLUB_SLUG    = os.environ.get("CLUB_SLUG", "racketclubklover")
BOOKING_URL  = f"https://padelmates.se/club/{CLUB_SLUG}"
API_BASE     = os.environ.get(
    "API_BASE", "https://fastapi-production-fargate.padelmates.io"
)
TZ_NAME      = os.environ.get("TZ_NAME", "Europe/Copenhagen")
TARGET_DATE  = os.environ.get("TARGET_DATE", "2026-06-10")  # YYYY-MM-DD, local
START_LOCAL  = os.environ.get("START_LOCAL", "06:00")        # HH:MM, local
DURATION_MIN = int(os.environ.get("DURATION_MIN", "90"))     # 06:00 -> 07:30
COURT_PREFIXES = tuple(
    p.strip() for p in os.environ.get("COURT_PREFIXES", "01,02,03,04,05,06").split(",")
    if p.strip()
)

NTFY_SERVER  = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC   = os.environ.get("NTFY_TOPIC", "")
STATE_FILE   = os.environ.get("STATE_FILE", "state.json")
RENOTIFY_HOURS = float(os.environ.get("RENOTIFY_HOURS", "3"))  # re-ping if still open

# Optional auth: a logged-in account sees further than the ~14-day anonymous
# horizon. Provide a Firebase refresh token + web API key (both as secrets) and
# the watcher mints a fresh id token each run.
PM_REFRESH_TOKEN = os.environ.get("PM_REFRESH_TOKEN", "")
PM_API_KEY       = os.environ.get("PM_API_KEY", "")


# ---- Timezone helpers -------------------------------------------------------
def _eu_dst(date):
    """True if European DST (CEST, UTC+2) is in effect on `date` (a date)."""
    year = date.year

    def last_sunday(month):
        d = dt.date(year, month, 31)
        return d - dt.timedelta(days=(d.weekday() + 1) % 7)

    start = last_sunday(3)   # last Sunday of March
    end = last_sunday(10)    # last Sunday of October
    return start <= date < end


def local_to_utc_ms(date_str, hhmm):
    """Convert a local (Europe/Copenhagen) date + HH:MM to UTC epoch ms."""
    y, m, d = (int(x) for x in date_str.split("-"))
    hh, mm = (int(x) for x in hhmm.split(":"))
    try:
        from zoneinfo import ZoneInfo
        local = dt.datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(TZ_NAME))
        return int(local.timestamp() * 1000)
    except Exception:
        # Fallback: assume Europe/Copenhagen (CET/CEST) without tz database.
        offset = 2 if _eu_dst(dt.date(y, m, d)) else 1
        naive = dt.datetime(y, m, d, hh, mm)
        utc = naive - dt.timedelta(hours=offset)
        epoch = dt.datetime(1970, 1, 1)
        return int((utc - epoch).total_seconds() * 1000)


def today_local():
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo(TZ_NAME)).date()
    except Exception:
        offset = 2 if _eu_dst(dt.datetime.utcnow().date()) else 1
        return (dt.datetime.utcnow() + dt.timedelta(hours=offset)).date()


def now_utc_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- API --------------------------------------------------------------------
def get_id_token():
    """Exchange a Firebase refresh token for a fresh id token, or return None."""
    if not (PM_REFRESH_TOKEN and PM_API_KEY):
        return None
    url = f"https://securetoken.googleapis.com/v1/token?key={PM_API_KEY}"
    body = urllib.parse.urlencode(
        {"grant_type": "refresh_token", "refresh_token": PM_REFRESH_TOKEN}
    ).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r).get("id_token")


def fetch_slots(start_ms, end_ms, id_token=None):
    qs = urllib.parse.urlencode(
        {"club_id": CLUB_ID, "start_datetime": start_ms, "end_datetime": end_ms}
    )
    url = f"{API_BASE}/player/player_booking/all_courts_slot_prices_v2?{qs}"
    headers = {"Content-Type": "application/json"}
    if id_token:
        headers["Authorization"] = "Bearer " + id_token
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    return data.get("allSlots", []) if isinstance(data, dict) else []


def find_matching_courts(slots, target_start_ms):
    """Return sorted list of court names that have the wanted slot free."""
    courts = set()
    for s in slots:
        if s.get("startTimestamp") != target_start_ms:
            continue
        if int(s.get("duration", 0)) != DURATION_MIN:
            continue
        if s.get("reservedIntersection") is True:
            continue
        name = str(s.get("courtName", ""))
        if name[:2] in COURT_PREFIXES:
            courts.add(name)
    return sorted(courts)


# ---- ntfy -------------------------------------------------------------------
def send_ntfy(title, message, priority="urgent", tags="tennis,bell"):
    if not NTFY_TOPIC:
        print("WARN: NTFY_TOPIC not set; skipping push. Message was:")
        print(f"  {title}: {message}")
        return False
    url = f"{NTFY_SERVER}/{NTFY_TOPIC}"
    # HTTP headers must be latin-1; emojis belong in Tags (rendered by ntfy) and
    # the message body (UTF-8), never in header values like Title.
    def h(s):
        return s.encode("latin-1", "ignore").decode("latin-1")
    headers = {
        "Title": h(title),
        "Priority": h(priority),
        "Tags": h(tags),
        "Click": BOOKING_URL,
        "Actions": h(f"view, Open booking, {BOOKING_URL}"),
    }
    req = urllib.request.Request(
        url, data=message.encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
        print(f"ntfy sent to {NTFY_SERVER}/{NTFY_TOPIC}: {title}")
        return True
    except Exception as e:  # pragma: no cover - network failure
        print(f"ERROR sending ntfy: {e}")
        return False


# ---- State ------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def hours_since(iso):
    if not iso:
        return 1e9
    try:
        t = dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc
        )
        return (dt.datetime.now(dt.timezone.utc) - t).total_seconds() / 3600.0
    except Exception:
        return 1e9


# ---- Main -------------------------------------------------------------------
def main():
    if "--test" in sys.argv:
        ok = send_ntfy(
            "Padel watcher is live",
            f"Watching {CLUB_SLUG} for {START_LOCAL}-(+{DURATION_MIN}min) on "
            f"{TARGET_DATE}, courts {'/'.join(COURT_PREFIXES)}. "
            f"You'll get a push here when it opens up.",
            priority="default",
            tags="tennis,white_check_mark",
        )
        return 0 if ok else 1

    target_start_ms = local_to_utc_ms(TARGET_DATE, START_LOCAL)
    win_start_ms = local_to_utc_ms(TARGET_DATE, "00:00")
    win_end_ms = win_start_ms + 24 * 3600 * 1000  # whole target day

    # Stop quietly once the target date has passed.
    if dt.date(*map(int, TARGET_DATE.split("-"))) < today_local():
        print(f"Target date {TARGET_DATE} is in the past; nothing to watch.")
        return 0

    try:
        id_token = get_id_token()
    except Exception as e:
        print(f"WARN: auth token refresh failed ({e}); falling back to anonymous")
        id_token = None
    print("auth:", "logged-in" if id_token else "anonymous")

    try:
        slots = fetch_slots(win_start_ms, win_end_ms, id_token=id_token)
    except Exception as e:
        print(f"ERROR fetching slots: {e}")
        return 1  # transient; next cron run retries

    courts = find_matching_courts(slots, target_start_ms)
    available = len(courts) > 0

    state = load_state()
    was_available = bool(state.get("available"))
    last_notified = state.get("last_notified_utc")

    print(
        f"{now_utc_iso()} target={TARGET_DATE} {START_LOCAL}+{DURATION_MIN}min "
        f"available={available} courts={courts} (was={was_available})"
    )

    notify = False
    if available and not was_available:
        notify = True  # just opened up
    elif available and was_available and RENOTIFY_HOURS > 0 \
            and hours_since(last_notified) >= RENOTIFY_HOURS:
        notify = True  # still open, gentle reminder

    if notify:
        courts_str = "\n".join(f"  • {c}" for c in courts)
        send_ntfy(
            "06:00-07:30 court is OPEN!",
            f"Racket Club Kløver — {TARGET_DATE}\n"
            f"{START_LOCAL}-07:30 free on:\n{courts_str}\n\nBook now: {BOOKING_URL}",
            priority="urgent",
            tags="tennis,bell,rotating_light",
        )
        state["last_notified_utc"] = now_utc_iso()

    state.update(
        {
            "target_date": TARGET_DATE,
            "start_local": START_LOCAL,
            "duration_min": DURATION_MIN,
            "available": available,
            "courts": courts,
            "last_check_utc": now_utc_iso(),
        }
    )
    state.setdefault("last_notified_utc", last_notified)
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
