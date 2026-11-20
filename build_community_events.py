"""
Builds community_events.js -- youth-friendly events from partner calendars, shown on the
map alongside the HARD calendar events (events.js, from build_events.py).

Sources (each one is optional: if a site is down, that host's events from the previous
run are kept so the map never goes blank):

  Hayward Public Library   hayward.librarycalendar.com/events/feed/json   (has real age groups)
  City of Hayward          manual_events.json -- the City's signature events (Light Up the
                           Season, MLK Day, Juneteenth...) live on standalone pages with no
                           feed, so they're added by hand; its main calendar is only meetings
  San Lorenzo USD          slzusd.org Edlio iCal feed
  Hayward USD              HUSD community Google Calendar (iCal)
  Castro Valley USD        cv.k12.ca.us calendar API

School-district calendars are mostly board meetings, breaks and minimum days, so those are
filtered out by title (SCHOOL_EXCLUDE); concerts, plays, festivals and family events stay.

Repeating events (e.g. weekly Teen Time) become one map pin with a list of upcoming dates.

Standard library only, so it runs as-is in the daily GitHub Action (update-events.yml).
"""
import datetime as dt
import html
import json
import os
import re
import time
import urllib.parse
from zoneinfo import ZoneInfo

import build_events as be  # shared helpers: fetch, strip_tags, age_group, geocode

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(HERE, "community_events.js")
MANUAL_PATH = os.path.join(HERE, "manual_events.json")
GEOCODE_CACHE_PATH = os.path.join(HERE, "community_events_geocode_cache.json")

DAYS_AHEAD = 90
PACIFIC = ZoneInfo("America/Los_Angeles")

LIBRARY_FEED = "https://hayward.librarycalendar.com/events/feed/json"
SLZUSD_ICS = "https://www.slzusd.org/apps/events/ical/?id=0"
HUSD_ICS = ("https://calendar.google.com/calendar/ical/"
            "husd.k12.ca.us_e524uoda2saef56lpuo7nv4lk8%40group.calendar.google.com/public/basic.ics")
CVUSD_API = "https://www.cv.k12.ca.us/api/calendars/137799/events"

# Library branch -> street address (Community Hub, Makerspace and Digital Media Lab are
# all inside the Downtown Library).
LIBRARY_BRANCHES = {
    "Downtown Library": "888 C St, Hayward, CA 94541",
    "Community Hub": "888 C St, Hayward, CA 94541",
    "Makerspace": "888 C St, Hayward, CA 94541",
    "Digital Media Lab": "888 C St, Hayward, CA 94541",
    "Weekes Branch": "27300 Patrick Ave, Hayward, CA 94544",
    "City Hall": "777 B St, Hayward, CA 94541",
    "Offsite": "777 B St, Hayward, CA 94541",  # offsite events so far are by City Hall
}
LIBRARY_YOUTH_AGES = {"Babies & Toddlers", "Preschoolers", "Kids", "Teens", "Families"}
LIBRARY_SKIP_TYPES = {"Boards & Commissions"}

SCHOOL_EXCLUDE = re.compile(
    r"board|committee|council|commission|meeting|closed session|session|recess|break|"
    r"no school|minimum day|trimester|quarter ends|semester|work day|staff development|"
    r"enrollment|registration|transfer|conference|delac|elac|lcap|aasai|apici|"
    r"day observed|memorial day|veterans day|labor day|presidents|king jr\. day$|"
    r"shake ?out|school ends|school begins|first day|last day|holiday|closure|closed|"
    r"listening session|workshop for staff|deadline|applications",
    re.I)
NOT_A_PLACE = re.compile(r"zoom|teams|google meet|virtual|online|all school sites|^https?:", re.I)

# a rough box around the HARD area; geocoder hits outside it are treated as misses
BBOX = (37.55, 37.80, -122.25, -121.95)  # lat min/max, lon min/max


def in_area(coords):
    return bool(coords) and BBOX[0] <= coords[1] <= BBOX[1] and BBOX[2] <= coords[0] <= BBOX[3]


# ---------------------------------------------------------------- formatting helpers

def fmt_clock(t):
    """datetime.time -> '6:30p' / '8p' (the style the HARD calendar uses)."""
    h = t.hour % 12 or 12
    suffix = "a" if t.hour < 12 else "p"
    return f"{h}{suffix}" if t.minute == 0 else f"{h}:{t.minute:02d}{suffix}"


def fmt_time(start, end, all_day):
    if all_day or start is None:
        return "All day"
    return f"{fmt_clock(start)} - {fmt_clock(end)}" if end and end != start else fmt_clock(start)


def fmt_when(d):
    return f"{d:%a, %b} {d.day}"


def shorten(text, n=260):
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[:n - 3].rsplit(" ", 1)[0] + "…"


def library_age_group(groups):
    groups = set(groups)
    if "Families" in groups or groups & {"Adults", "Seniors"}:
        return "All Ages"  # open to kids and grown-ups alike
    if "Teens" in groups:
        return "Teens & Youth"
    return "Young Kids"


# ---------------------------------------------------------------- sources
# Each source returns a list of raw occurrences:
#   dict(host, name, date, time, venue, address, description, website, age_group)

def from_library(today, horizon):
    data = json.loads(be.fetch(LIBRARY_FEED, accept_json=True))
    out = []
    for e in data:
        vals = lambda x: list(x.values()) if isinstance(x, dict) else list(x or [])
        start = dt.datetime.fromisoformat(e["start_date"])
        end = dt.datetime.fromisoformat(e["end_date"]) if e.get("end_date") else None
        if not (today <= start.date() <= horizon):
            continue
        ages = vals(e.get("age_group"))
        if not set(ages) & LIBRARY_YOUTH_AGES:
            continue
        if set(vals(e.get("program_type"))) & LIBRARY_SKIP_TYPES:
            continue
        branches = [b for b in vals(e.get("branch")) if "Study Rooms" not in b]
        if not branches or len(branches) > 2 or branches[0] not in LIBRARY_BRANCHES:
            continue  # online-only, all-branch announcements, study-room bookings
        title = html.unescape(e["title"]).strip()
        if re.search(r"closed|closing|closure", title, re.I):
            continue
        branch = branches[0]
        venue = {"Community Hub": "Downtown Library – Community Hub",
                 "Weekes Branch": "Weekes Branch Library",
                 "Offsite": "Near Hayward City Hall"}.get(branch, branch)
        if branch == "Downtown Library" and len(branches) == 1:
            venue = "Downtown Hayward Library"
        out.append(dict(
            host="Hayward Public Library",
            name=title,
            date=start.date(),
            time=fmt_time(start.time(), end.time() if end else None, False),
            venue=venue,
            address=LIBRARY_BRANCHES[branch],
            description=shorten(be.strip_tags(e.get("description") or "")),
            website=e["url"],
            age_group=library_age_group(ages),
        ))
    return out


def parse_ics(text):
    """Minimal iCal reader: yields dicts of the first value of each property per VEVENT."""
    text = re.sub(r"\r?\n[ \t]", "", text)  # unfold continuation lines
    for block in re.findall(r"BEGIN:VEVENT\r?\n(.*?)END:VEVENT", text, re.S):
        ev = {}
        for line in block.splitlines():
            m = re.match(r"([A-Z-]+)((?:;[^:]*)?):(.*)", line)
            if m and m.group(1) not in ev:
                ev[m.group(1)] = (m.group(2), m.group(3))
        yield ev


def ics_unescape(s):
    return s.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\").strip()


def ics_datetime(params, value):
    """-> (date, time or None) in Pacific time."""
    if "VALUE=DATE" in params or re.fullmatch(r"\d{8}", value):
        return dt.datetime.strptime(value[:8], "%Y%m%d").date(), None
    stamp = dt.datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        stamp = stamp.replace(tzinfo=dt.timezone.utc).astimezone(PACIFIC)
    else:
        tz = re.search(r"TZID=([^;:]+)", params)
        if tz and tz.group(1) not in ("America/Los_Angeles", "US/Pacific"):
            try:
                stamp = stamp.replace(tzinfo=ZoneInfo(tz.group(1))).astimezone(PACIFIC)
            except Exception:
                pass
    return stamp.date(), stamp.time()


STREET = re.compile(
    r"\d{2,6}\s+(?:[NSEW]\.?\s+)?[A-Za-z0-9.' ]+?\s(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|"
    r"Dr|Drive|Way|Ln|Lane|Ct|Court|Pl|Place|Pkwy|Parkway)\b\.?", re.I)
CITY = re.compile(r"\b(Hayward|San Lorenzo|San Leandro|Castro Valley|Union City)\b", re.I)


def split_location(location, default_city):
    """'Multipurpose Room @ Arroyo High School -- 15701 Lorenzo Ave, San Lorenzo' ->
    (venue name, geocodable address or None)."""
    loc = re.sub(r"\s+", " ", location).strip(" ,")
    m = STREET.search(loc)
    if not m:
        return loc, None
    city = CITY.search(loc[m.end():]) or CITY.search(loc)
    address = f"{m.group(0).rstrip('.')}, {city.group(1) if city else default_city}, CA"
    venue = re.split(r"\s+(?:--|–|-|@)\s+|\n", loc[:m.start()].strip(" ,-–@"))
    venue = [v for v in venue if v.strip()]
    return (venue[-1].strip() if venue else address.split(",")[0]), address


def from_school_ics(url, host, default_city, today, horizon):
    out = []
    for ev in parse_ics(be.fetch(url)):
        if "RRULE" in ev:
            continue  # the district feeds only use repeat rules for old/admin series
        if "DTSTART" not in ev or "SUMMARY" not in ev:
            continue
        date, start = ics_datetime(*ev["DTSTART"])
        if not (today <= date <= horizon):
            continue
        end = ics_datetime(*ev["DTEND"])[1] if "DTEND" in ev else None
        title = ics_unescape(ev["SUMMARY"][1])
        location = ics_unescape(ev.get("LOCATION", ("", ""))[1])
        if SCHOOL_EXCLUDE.search(title) or not location or NOT_A_PLACE.search(location):
            continue
        venue, address = split_location(location, default_city)
        description = shorten(be.strip_tags(ics_unescape(ev.get("DESCRIPTION", ("", ""))[1])))
        link = ics_unescape(ev.get("URL", ("", ""))[1])
        out.append(dict(
            host=host, name=title, date=date,
            time=fmt_time(start, end, start is None),
            venue=venue, address=address, raw_location=location,
            description=description,
            website=link if link.startswith("http") else
            {"San Lorenzo USD": "https://www.slzusd.org/apps/events/",
             "Hayward USD": "https://www.husd.us/fs/pages/6998"}[host],
            age_group=be.age_group(title, description),
        ))
    return out


def from_cvusd(today, horizon):
    url = CVUSD_API + "?" + urllib.parse.urlencode(
        {"start_date": today.isoformat(), "end_date": horizon.isoformat(), "view_source": "event-slider"})
    data = json.loads(be.fetch(url, accept_json=True))
    out = []
    for e in data.get("data", {}).get("events", []):
        title = html.unescape(e.get("title") or "").strip()
        location = (e.get("address") or "").strip()
        if SCHOOL_EXCLUDE.search(title) or not location or NOT_A_PLACE.search(location):
            continue
        date = dt.date.fromisoformat(e["start_date"])
        start = None if e.get("all_day") else dt.time.fromisoformat(e["start_time"])
        end = None if e.get("all_day") else dt.time.fromisoformat(e["end_time"])
        venue, address = split_location(location, "Castro Valley")
        description = shorten(be.strip_tags(e.get("description") or ""))
        out.append(dict(
            host="Castro Valley USD", name=title, date=date, time=fmt_time(start, end, start is None),
            venue=venue, address=address, raw_location=location, description=description,
            website=e.get("event_url") or "https://www.cv.k12.ca.us/calendar",
            age_group=be.age_group(title, description),
        ))
    return out


def from_manual(today, horizon):
    if not os.path.exists(MANUAL_PATH):
        return []
    out = []
    for e in json.load(open(MANUAL_PATH, encoding="utf-8"))["events"]:
        date = dt.date.fromisoformat(e["date"])
        if today <= date <= horizon:
            out.append(dict(e, date=date, age_group=e.get("age_group") or be.age_group(e["name"], e.get("description", ""))))
    return out


# ---------------------------------------------------------------- assemble

def previous_features():
    """host -> features from the last successful run (used when a source is down)."""
    if not os.path.exists(OUT_PATH):
        return {}
    raw = open(OUT_PATH, encoding="utf-8").read()
    raw = raw[raw.index("{"):].strip().rstrip(";")
    prev = {}
    for f in json.loads(raw)["features"]:
        prev.setdefault(f["properties"]["host"], []).append(f)
    return prev


def group_into_features(rows, cache, unplaced):
    groups = {}
    for r in sorted(rows, key=lambda r: (r["date"], r["time"])):
        groups.setdefault((r["host"], r["name"].lower(), r["venue"].lower()), []).append(r)

    features = []
    for (_host, _name, _venue), occ in groups.items():
        first = occ[0]
        coords = None
        if first.get("coords"):
            coords = first["coords"]
        elif first.get("address"):
            coords = be.geocode(first["address"], cache)
            if not in_area(coords):
                coords = None
        if not coords:
            unplaced.append(f"{first['host']}: {first['name']} @ {first.get('raw_location') or first['venue']}")
            continue
        seen, occurrences = set(), []
        for r in occ:
            key = (r["date"], r["time"])
            if key in seen:
                continue
            seen.add(key)
            occurrences.append({"date": r["date"].isoformat(), "when": fmt_when(r["date"]), "time": r["time"]})
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": coords},
            "properties": {
                "source": "Event",
                "host": first["host"],
                "name": first["name"],
                "age_group": first["age_group"],
                "month": f"{first['date']:%B %Y}",
                "date": first["date"].isoformat(),
                "when": fmt_when(first["date"]),
                "time": first["time"],
                "venue": first["venue"],
                "address": (first.get("address") or "").removesuffix(", CA"),
                "description": first.get("description", ""),
                "website": first["website"],
                "occurrences": occurrences,
            },
        })
    return features


def main():
    today = dt.date.today()
    horizon = today + dt.timedelta(days=DAYS_AHEAD)
    cache = json.load(open(GEOCODE_CACHE_PATH, encoding="utf-8")) if os.path.exists(GEOCODE_CACHE_PATH) else {}
    prev = previous_features()

    sources = [
        ("Hayward Public Library", lambda: from_library(today, horizon)),
        ("City of Hayward", lambda: from_manual(today, horizon)),
        ("San Lorenzo USD", lambda: from_school_ics(SLZUSD_ICS, "San Lorenzo USD", "San Lorenzo", today, horizon)),
        ("Hayward USD", lambda: from_school_ics(HUSD_ICS, "Hayward USD", "Hayward", today, horizon)),
        ("Castro Valley USD", lambda: from_cvusd(today, horizon)),
    ]
    features, unplaced, counts = [], [], {}
    for host, load in sources:
        try:
            rows = load()
        except Exception as err:
            kept = [f for f in prev.get(host, []) if f["properties"]["date"] >= today.isoformat()]
            print(f"! {host}: {err} -- keeping {len(kept)} events from the last run")
            features += kept
            counts[host] = len(kept)
            continue
        # manual (City) events may list several hosts; keep their own host label
        feats = group_into_features(rows, cache, unplaced)
        print(f"{host}: {len(rows)} dates -> {len(feats)} map pins")
        features += feats
        counts[host] = len(feats)
        time.sleep(1)

    features.sort(key=lambda f: f["properties"]["date"])
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write("// Generated by build_community_events.py -- do not edit by hand.\n")
        f.write("const COMMUNITY_EVENTS_DATA = ")
        json.dump({"type": "FeatureCollection", "updated": f"{today:%b} {today.day}, {today:%Y}",
                   "hosts": counts, "features": features}, f, ensure_ascii=False)
        f.write(";\n")
    with open(GEOCODE_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=1, sort_keys=True)

    print(f"Wrote {len(features)} event pins to {OUT_PATH}")
    for ft in features:
        p = ft["properties"]
        n = len(p["occurrences"])
        print(f"  {p['date']}  {p['host'][:14]:14}  {p['age_group']:14}  {p['name'][:46]:46} x{n:<3} @ {p['venue'][:35]}")
    if unplaced:
        print(f"Could not place {len(unplaced)} event(s):")
        for u in unplaced:
            print("  -", u)


if __name__ == "__main__":
    main()
