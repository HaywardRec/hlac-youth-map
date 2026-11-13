"""
Builds events.js -- upcoming events from the HARD website calendar, placed on the map.

Source: the JSON endpoint behind the calendar's Month view on haywardrec.org
  /m/calendar/Home/MonthEvents?startDate=YYYY-MM-DD&endDate=YYYY-MM-DD&cat=<ids>
plus each event's detail page (for the street address and description).

Each event is placed at the matching facility already on the map (by venue name);
if no facility matches, its street address is geocoded via Nominatim and cached in
events_geocode_cache.json so the same address is never looked up twice.

The calendar has no age field, so an age group is inferred from the event's title and
description (see age_group()). Events with no age wording default to "All Ages".

Standard library only, so it runs as-is in the scheduled GitHub Action
(.github/workflows/update-events.yml), which refreshes events.js every morning.
"""
import datetime as dt
import html
import json
import os
import re
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
MAP_DATA_PATH = os.path.join(HERE, "map_data.js")
OUT_PATH = os.path.join(HERE, "events.js")
GEOCODE_CACHE_PATH = os.path.join(HERE, "events_geocode_cache.json")

BASE = "https://www.haywardrec.org"
DAYS_AHEAD = 90

# Calendar category IDs (from the category picker on /m/calendar). Board of Directors'
# Meetings (23), Intranet (28) and Main Calendar (14) are left out on purpose.
CATEGORIES = {
    35: "Arts",
    43: "Capital Improvement Program",
    29: "Seniors",
    31: "Special Events",
    40: "Teens",
    27: "Volunteer Hayward Events",
}

# Calendar venue name -> facility name on the map, where the two differ.
VENUE_ALIASES = {
    "mt. eden mansion": "Mt. Eden Mansion",
    "mt eden mansion": "Mt. Eden Mansion",
    "meek mansion": "Meek Estate Park",
}

USER_AGENT = "hlac-youth-map/1.0 (+https://github.com/HaywardRec/hlac-youth-map)"

AGE_GROUPS = ["Young Kids", "Teens & Youth", "Adults & Seniors", "All Ages"]


def fetch(url, accept_json=False):
    headers = {"User-Agent": USER_AGENT}
    if accept_json:
        headers["X-Requested-With"] = "XMLHttpRequest"
        headers["Accept"] = "application/json"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        # the site's pages are UTF-8 but some pasted text (smart quotes, dashes) arrives
        # as stray Windows-1252 bytes; decode those individually instead of dropping them
        return r.read().decode("utf-8", errors="cp1252fallback")


def _cp1252_fallback(err):
    bad = err.object[err.start:err.end]
    return bad.decode("cp1252", errors="replace"), err.end


import codecs  # noqa: E402
codecs.register_error("cp1252fallback", _cp1252_fallback)


def month_ranges(start, days):
    end = start + dt.timedelta(days=days)
    cur = start.replace(day=1)
    while cur <= end:
        nxt = (cur.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        yield max(cur, start), min(nxt - dt.timedelta(days=1), end)
        cur = nxt


def fetch_calendar(today):
    cats = ",".join(str(c) for c in CATEGORIES)
    events = {}
    for a, b in month_ranges(today, DAYS_AHEAD):
        url = (f"{BASE}/m/calendar/Home/MonthEvents?startDate={a:%Y-%m-%d}"
               f"&endDate={b:%Y-%m-%d}&cat={urllib.parse.quote(cats)}")
        for e in json.loads(fetch(url, accept_json=True)):
            events[e["id"]] = e  # spanning events can appear in two months
        time.sleep(1)
    return list(events.values())


def strip_tags(s):
    s = re.sub(r"<(script|style)\b.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()


def fetch_detail(path):
    """Return (address, description) from an event's detail page."""
    page = fetch(BASE + path)
    address = None
    m = re.search(r'href="https?://maps\.google\.com/[^"]*"[^>]*>\s*(.*?)\s*</a>', page, re.S)
    if m:
        address = strip_tags(m.group(1))

    description = ""
    start = page.find('aria-label="Event location"')
    stop = page.find("Related Events")
    if start != -1:
        chunk = page[start:stop if stop > start else start + 20000]
        # skip past the location block's own links
        chunk = chunk.split("</a>", 2)[-1]
        text = strip_tags(chunk)
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        drop = {"sign up today!", "ada compliant", "register", "register now"}
        lines = [ln for ln in lines if ln.lower() not in drop]
        description = " ".join(lines)
    return address, description


AGE_RANGE = re.compile(r"\bages?\s*(\d{1,2})\s*(?:-|–|to|through)\s*(\d{1,2})", re.I)
AGE_PLUS = re.compile(r"\bages?\s*(\d{1,2})\s*\+|\b(\d{1,2})\s*(?:\+|and (?:up|older|over))", re.I)


def age_group(title, description):
    t = title.lower()
    text = f"{title} {description}".lower()

    m = AGE_RANGE.search(text)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        if hi <= 12:
            return "Young Kids"
        if lo >= 11 and hi <= 19:
            return "Teens & Youth"
        if lo >= 18:
            return "Adults & Seniors"
        return "All Ages"
    m = AGE_PLUS.search(text)
    if m:
        lo = int(m.group(1) or m.group(2))
        if lo >= 18:
            return "Adults & Seniors"
        if lo >= 11:
            return "Teens & Youth"

    if re.search(r"pre-?k\b|preschool|toddler|\btots?\b|parents'? night out|little learners", t):
        return "Young Kids"
    if re.search(r"\bteens?\b|\btweens?\b|\byouth\b|middle school|high school", t):
        return "Teens & Youth"
    if re.search(r"\bseniors?\b|\b55\+|\b50\+|adults? only|\b21\+", t):
        return "Adults & Seniors"
    if re.search(r"all ages|famil(y|ies)", text):
        return "All Ages"
    if re.search(r"pre-?k\b|preschool|toddler|potty.trained|little learners", text):
        return "Young Kids"
    if re.search(r"\bteens?\b|\btweens?\b", text):
        return "Teens & Youth"
    if re.search(r"\bseniors?\b|adults? only", text):
        return "Adults & Seniors"
    return "All Ages"


def norm(s):
    s = s.lower().replace("&", "and")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def load_places():
    raw = open(MAP_DATA_PATH, encoding="utf-8").read()
    data = json.loads(raw.removeprefix("const MAP_DATA = ").strip().rstrip(";"))
    places = {}
    for layer in ("hard", "coh", "landmark"):
        for f in data.get(layer, {}).get("features", []):
            name = f["properties"].get("name")
            if name:
                places.setdefault(norm(name), (name, f["geometry"]["coordinates"]))
    return places


def match_venue(location, places):
    if not location:
        return None
    parts = [p.strip() for p in location.split(">")]
    # "McConaghy Park > McConaghy House": try the most specific piece first
    for cand in reversed(parts):
        alias = VENUE_ALIASES.get(cand.lower())
        if alias and norm(alias) in places:
            return places[norm(alias)]
        if norm(cand) in places:
            return places[norm(cand)]
    return None


def geocode(address, cache):
    if address in cache:
        return cache[address]
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": address, "format": "json", "limit": 1, "countrycodes": "us"})
    try:
        result = json.loads(fetch(url))
    except Exception as e:  # network hiccup: skip this run, try again tomorrow
        print(f"  ! geocode error for {address}: {e}")
        return None
    time.sleep(1)  # Nominatim policy: max 1 request/second
    coords = [float(result[0]["lon"]), float(result[0]["lat"])] if result else None
    cache[address] = coords
    return coords


def main():
    today = dt.date.today()
    places = load_places()
    cache = {}
    if os.path.exists(GEOCODE_CACHE_PATH):
        cache = json.load(open(GEOCODE_CACHE_PATH, encoding="utf-8"))

    raw_events = fetch_calendar(today)
    print(f"Calendar returned {len(raw_events)} events in the next {DAYS_AHEAD} days")

    features, unplaced = [], []
    for e in sorted(raw_events, key=lambda e: e["start"]):
        x = e["extendedProps"]
        if x.get("isICalFeedEvent"):
            continue
        title = html.unescape(e["title"]).strip()
        location = html.unescape(x.get("location") or "").strip()
        detail_path = x.get("navigationUrl") or f"/m/calendar/event/detail/{e['id']}"
        try:
            address, description = fetch_detail(detail_path)
        except Exception as err:
            print(f"  ! couldn't read detail page for {title}: {err}")
            address, description = None, ""
        time.sleep(0.5)

        venue = match_venue(location, places)
        if venue:
            venue_name, coords = venue
        elif address:
            venue_name, coords = location or address, geocode(address, cache)
        else:
            venue_name, coords = location, None
        if not coords:
            unplaced.append(f"{title} @ {location or '?'}")
            continue

        date = dt.date.fromisoformat(x["date"])
        end_date = dt.date.fromisoformat(x.get("endDate") or x["date"])
        when = f"{date:%a, %b} {date.day}"
        if end_date != date:
            when += f" – {end_date:%b} {end_date.day}"

        short = description if len(description) <= 260 else description[:257].rsplit(" ", 1)[0] + "…"
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": coords},
            "properties": {
                "source": "Event",
                "name": title,
                "age_group": age_group(title, description),
                "category": x.get("category") or "",
                "month": f"{date:%B %Y}",
                "date": x["date"],
                "when": when,
                "time": "All day" if e.get("allDay") else (x.get("time") or ""),
                "venue": venue_name,
                "address": address,
                "description": short,
                "website": BASE + detail_path,
            },
        })

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write("// Generated by build_events.py -- do not edit by hand.\n")
        f.write("const EVENTS_DATA = ")
        json.dump({"type": "FeatureCollection", "updated": f"{today:%b} {today.day}, {today:%Y}",
                   "features": features}, f, ensure_ascii=False)
        f.write(";\n")
    with open(GEOCODE_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=1, sort_keys=True)

    print(f"Wrote {len(features)} events to {OUT_PATH}")
    for p in features:
        pr = p["properties"]
        print(f"  {pr['date']}  {pr['age_group']:16}  {pr['name'][:50]:50}  @ {pr['venue']}")
    if unplaced:
        print(f"Could not place {len(unplaced)} event(s):")
        for u in unplaced:
            print("  -", u)


if __name__ == "__main__":
    main()
