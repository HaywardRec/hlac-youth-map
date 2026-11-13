"""
Builds classes.js -- HARD classes from the current Recreation Guide PDF(s), placed on the map.

Source: https://www.haywardrec.org/407/Current-Recreation-Guides links the current guides
(e.g. "Winter 2027", "Fall 2026") as DocumentCenter PDFs. This script finds those links,
downloads each PDF, and parses every class:

    <Title>
    <description ...>
    42180 7-14 yr Sa 11:15am-12:15pm Jan 9-Mar 27      <- course #, ages, days, time, dates
    11 classes SLCC CN Staff $275                       <- # meetings, LOCATION CODE, instructor, fee

Location codes (SLCC, KASCC, ...) are resolved with the guide's own Facilities Directory,
then matched to a facility already on the map (or geocoded from the directory address).
Interest comes from the guide section the class is listed in; age groups come from each
session's age range, so the map's age filter is exact.

Runs monthly via .github/workflows/update-classes.yml, so a new season's guide is picked up
automatically once it's posted. Classes whose last session has ended are left out (and the
map also hides them client-side between runs).

Requires: pypdf  (pip install pypdf)
"""
import datetime as dt
import html
import io
import json
import os
import re
import time
import urllib.parse
import urllib.request
from collections import OrderedDict

from pypdf import PdfReader

HERE = os.path.dirname(os.path.abspath(__file__))
MAP_DATA_PATH = os.path.join(HERE, "map_data.js")
OUT_PATH = os.path.join(HERE, "classes.js")
GEOCODE_CACHE_PATH = os.path.join(HERE, "classes_geocode_cache.json")

BASE = "https://www.haywardrec.org"
GUIDES_PAGE = BASE + "/407/Current-Recreation-Guides"
REGISTER_SEARCH = "https://anc.apm.activecommunities.com/haywardrec/activity/search?activity_keyword="
USER_AGENT = "hlac-youth-map/1.0 (+https://github.com/HaywardRec/hlac-youth-map)"

SEASON_RE = r"(Winter|Spring|Summer|Fall)\s+(\d{4})"
MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}

# Guide section (as printed in its table of contents) -> Interest filter on the map.
# Keys are matched with spaces/punctuation removed and lower-cased.
SECTION_INTEREST = {
    "art": "Arts & Crafts",
    "photography": "Arts & Crafts",
    "theater": "Theater & Performing",
    "theatre": "Theater & Performing",
    "dancemusic": "Music & Dance",
    "computerstechnology": "Tech, Coding & STEM",
    "stem": "Tech, Coding & STEM",
    "bakingcooking": "Cooking & Baking",
    "sports": "Sports",
    "tennis": "Sports",
    "gymnastics": "Sports",
    "martialarts": "Martial Arts",
    "healthfitness": "Fitness & Wellness",
    "learntoswim": "Swimming",
    "dropinprograms": "Swimming",
    "nature": "Nature & Outdoors",
    "outdooreducation": "Nature & Outdoors",
    "languages": "Languages",
    "specialinteresthobbies": "Hobbies & Special Interest",
    "prekprograms": "Preschool",
    "accessforall": "Adaptive & Inclusive",
    "specialevents": "Special Events",
    "socialservices": "Senior Services",
    "imagecentral": "Arts & Crafts",
    "volunteersinparksvip": "Volunteering",
    "adaptiverecreation": "Adaptive & Inclusive",
    "specialneedsdayprogram": "Adaptive & Inclusive",
    "winterscamps": "Camps", "wintercamps": "Camps", "springcamps": "Camps",
    "summercamps": "Camps", "fallcamps": "Camps", "camps": "Camps",
}

# Title keywords that override a vague section (camps, hobbies) with a clearer interest.
TITLE_INTEREST = [
    (r"robot|cod(e|ing)|minecraft|roblox|drone|lego|stem\b|steam\b|3d|web dev|tech|engineer|python|animation", "Tech, Coding & STEM"),
    (r"music|guitar|piano|ukulele|drum|sing|choir|dance|ballet|hip.?hop|folklorico", "Music & Dance"),
    (r"art\b|paint|draw|sculpt|ceramic|pottery|craft|clay|sew|knit|bead|photo", "Arts & Crafts"),
    (r"cook|bak(e|ing)|chef|cupcake|cake", "Cooking & Baking"),
    (r"soccer|basketball|baseball|softball|volleyball|tennis|pickleball|flag football|sports|gymnast|archery|fencing", "Sports"),
    (r"karate|jiu.?jitsu|taekwondo|kung fu|martial|boxing", "Martial Arts"),
    (r"swim|aquatic", "Swimming"),
    (r"nature|hike|garden|outdoor|camp(ing)?\b.*(nature|outdoor)", "Nature & Outdoors"),
    (r"chess|game|e-?sports", "Games & Gaming"),
]
VAGUE_INTERESTS = {"Camps", "Hobbies & Special Interest", "Other"}

# Location code -> facility name on the map, where the guide's directory wording differs.
CODE_TO_FACILITY = {
    "ADOBE": "Adobe Art Center",
    "AEOSP": "Alden E. Oliver Sports Park",
    "ACC": "Ashland Community Center",
    "CVCC": "Castro Valley Community Center",
    "CHCC": "Cherryland Community Center",
    "HASC": "Hayward Area Senior Center",
    "KASCC": "Kenneth C. Aitken Senior & Community Center",
    "MJCC": "Matt Jimenez Community Center",
    "SGCC": "Southgate Community Center",
    "SFCC": "San Felipe Community Center",
    "SLCC": "San Lorenzo Community Center",
    "WPCC": "Weekes Community Center",
    "HSIC": "Hayward Shoreline Interpretive Center",
    "SCNC": "Sulphur Creek Nature Center",
    "DMT": "Douglas Morrisson Theatre",
    "HP": "Hayward Plunge",
    "MKPK": "Meek Estate Park",
    "KYPK": "Kennedy Park",
    "FCP": "Five Canyons Park",
    "CANP": "Cannery Park",
    "EAP": "East Avenue Park",
    "UTPK": "Unity Park",
    "FVH": "Fairview Park",  # The Fiber Shed is at Fairview Park (same address)
    "UNITY": "Unity Park",
}

CITY_ABBR = {"CV": "Castro Valley", "Hay": "Hayward", "SLz": "San Lorenzo", "SL": "San Lorenzo",
             "Chy": "Hayward", "Ash": "San Leandro", "Fvw": "Hayward"}

AGE_GROUPS = [  # (label, lo, hi) -- a class is in a group if its age range overlaps it
    ("Young Kids (0–5)", 0, 5.99),
    ("Kids (6–12)", 6, 12.99),
    ("Teens (13–17)", 13, 17.99),
    ("Adults (18+)", 18, 49.99),
    ("Seniors (50+)", 50, 200),
]


# ---------------------------------------------------------------- fetching

def fetch(url, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    return data if binary else data.decode("utf-8", errors="replace")


def find_guides():
    """Return [(label, url)] for each guide linked on the Current Recreation Guides page."""
    page = fetch(GUIDES_PAGE)
    guides = OrderedDict()
    for href, inner in re.findall(r'<a[^>]+href="([^"]*DocumentCenter/View/\d+[^"]*)"[^>]*>(.*?)</a>', page, re.S | re.I):
        text = html.unescape(re.sub(r"<[^>]+>", " ", inner))
        alt = " ".join(re.findall(r'alt="([^"]*)"', inner))
        m = re.search(SEASON_RE, f"{text} {alt}", re.I)
        if not m:
            continue
        label = f"{m.group(1).title()} {m.group(2)}"
        doc_id = re.search(r"View/(\d+)", href).group(1)
        guides.setdefault(label, f"{BASE}/DocumentCenter/View/{doc_id}")
    return list(guides.items())


# ---------------------------------------------------------------- parsing helpers

def squash(s):
    return re.sub(r"[^a-z]", "", s.lower())


def norm(s):
    s = s.lower().replace("&", "and")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_toc(pages):
    """Return [(section_name, {printed page numbers})] from the guide's table of contents."""
    entry = re.compile(r"^\s*(.*?[A-Za-z)])\s*(?:\.\s*){3,}\s*([\d][\d ,\-–]*)\s*$")
    toc = []
    for text in pages[:6]:
        for line in text.split("\n"):
            m = entry.match(line)
            if not m:
                continue
            name = re.sub(r"\s+", " ", m.group(1)).strip()
            # "15-17" is a range, "46, 73" is two separate pages
            pages_ = set()
            for tok in m.group(2).split(","):
                nums = [int(n) for n in re.findall(r"\d+", tok)]
                if len(nums) == 2:
                    pages_.update(range(nums[0], nums[1] + 1))
                elif nums:
                    pages_.add(nums[0])
            if pages_:
                toc.append((name, pages_))
        if len(toc) > 20:
            break
    return toc


def page_number_offset(pages, season):
    """Printed page number = pdf index + offset (from footers like 'WINTER 2027 | 3')."""
    s = season.split()[0].upper()
    for i, text in enumerate(pages):
        m = re.search(rf"{s} \d{{4}} \| (\d+)", text) or re.search(rf"(\d+) \| {s} \d{{4}}", text)
        if m:
            return int(m.group(1)) - i
    return 1


def parse_directory(pages):
    """Location code -> directory text (name + address) from the Facilities Directory pages."""
    codes = {}
    for text in pages:
        if "Directory Key" not in text and "FACILITIES DIRECTORY" not in text:
            continue
        lines = [ln.strip() for ln in text.split("\n")]
        for i, ln in enumerate(lines):
            m = re.match(r"^([A-Z][A-Za-z0-9]{1,6})((?:,\s*[A-Z][A-Za-z0-9]{1,6})*)\s+([A-Z].+)$", ln)
            if not m or not re.search(r"[A-Z]{2}", m.group(1)):
                continue
            rest = m.group(3)
            if not re.search(r"\d{3,}", rest) and i + 1 < len(lines):
                rest += " " + lines[i + 1]
            for code in [m.group(1)] + re.findall(r"[A-Z][A-Za-z0-9]{1,6}", m.group(2)):
                codes.setdefault(code.upper(), rest)
    return codes


def parse_ages(s):
    """'7-14 yr' -> (7, 14); '50 yr+' -> (50, 200); 'Under 5 yr' -> (0, 5); '18 mo-3 yr' -> (1.5, 3)."""
    if not s:
        return None
    t = s.replace("½", ".5")
    t = re.sub(r"(\d)\s+\.5", r"\1.5", t)
    t = re.sub(r"(?<!\d)\.5", "0.5", t)
    parts = re.findall(r"(\d+(?:\.\d+)?)\s*(mo|m(?![a-z])|yrs?|y)?", t)
    if not parts:
        return None
    last_unit = next((u for _, u in reversed(parts) if u), "yr")
    vals = [float(n) / (12 if (u or last_unit) in ("mo", "m") else 1) for n, u in parts]
    if t.lower().startswith("under"):
        return (0, vals[0])
    if "+" in t:
        return (vals[0], 200)
    return (vals[0], vals[1]) if len(vals) > 1 else (vals[0], vals[0])


def age_groups(lo, hi):
    return [label for label, a, b in AGE_GROUPS if lo <= b and hi >= a and not (label.startswith("Teens") and lo >= 17)]


def parse_dates(s, season, year):
    """'Jan 9-Mar 27' -> (date, date). Fall guides spill into Jan; Winter guides start in Dec."""
    found = re.findall(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s*(\d{1,2})", s)
    if not found:
        return None, None

    def mk(mon, day):
        m = MONTHS[mon]
        y = year
        if season == "Winter" and m >= 9:
            y -= 1
        if season == "Fall" and m <= 3:
            y += 1
        try:
            return dt.date(y, m, int(day))
        except ValueError:
            return None
    start = mk(*found[0])
    end = mk(*found[-1])
    # "Jan 9-27" (month omitted on the end date)
    m2 = re.search(r"\d{1,2}\s*[-–]\s*(\d{1,2})\b(?!:)", s)
    if len(found) == 1 and m2 and start:
        try:
            end = start.replace(day=int(m2.group(1)))
        except ValueError:
            pass
    return start, end


# course number at line start; the PDF sometimes splits it ("418 35 50 yr+ ...")
SESSION_RE = re.compile(r"(?:^|(?<=\s))\W*(\d{5}|\d{3}\s\d{2}|\d{4}\s\d)\s+((?:Under\s|\d|½).*)$", re.I)
# meeting/location info that the PDF sometimes leaves on the same line as the session
INLINE_MEET_RE = re.compile(r"\s(\d+\s+(?:class|classes|session|sessions|event|events|day|days|week|weeks)\s+[A-Z].*)$", re.I)
DAY = r"(?:M|T|W|Th|F|Sa|Su)"
SESSION_DETAIL_RE = re.compile(
    rf"^(?P<ages>.*?)\s*(?P<days>{DAY}(?:\s*[/,&-]\s*{DAY})*)\s+(?P<time>\d{{1,2}}:\d{{2}}\s*[ap]m\s*[-–]\s*\d{{1,2}}:\d{{2}}\s*[ap]m)\s*(?P<dates>.*)$")
MEET_RE = re.compile(r"^(\d+)\s+(?:class|classes|session|sessions|event|events|day|days|week|weeks)\s+([A-Z][A-Za-z0-9]{1,6})\b(.*)$", re.I)
JUNK_RE = re.compile(r"HaywardRec\.org\s*\||HA ?YWAR|\|\s*(WINTER|SPRING|SUMMER|FALL)\s+\d{4}|^\(510\)|^Register Online|^Pictured|^Photo\b|^See pages?\b|^\d+\s+class", re.I)


def merge_split_lines(lines):
    """Rejoin course numbers the PDF broke across lines: '418' + '35 50 yr+ ...' or '41882' + '3-5 yr ...'."""
    out, i = [], 0
    while i < len(lines):
        ln = lines[i].strip()
        if re.fullmatch(r"\d{1,5}", ln) and i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            cand = (ln + " " + nxt) if len(ln) == 5 else (ln + nxt)
            if SESSION_RE.match(cand):
                out.append(cand)
                i += 2
                continue
        out.append(lines[i])
        i += 1
    return out


def is_header_line(ln):
    """ALL-CAPS sub-headers like 'TEEN EVENTS' or 'SCHOOL AGE CLASSES'."""
    letters = re.sub(r"[^A-Za-z]", "", ln)
    return len(letters) >= 4 and letters.isupper() and len(ln.split()) >= 2


def is_title_line(ln):
    t = re.sub(r"^New!\s*", "", ln).strip()
    # titles are short; wrapped description lines run ~45-55 characters
    if not t or len(t) > 44 or t.endswith((".", "!", "?", ",")):
        return False
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'’]*", t) if len(w) > 3]
    if not words:
        return bool(re.match(r"^[A-Z0-9]", t))
    caps = sum(w[0].isupper() for w in words)
    return caps / len(words) >= 0.75


def split_title_description(buf):
    lines = [ln for ln in buf if ln.strip() and not JUNK_RE.search(ln)
             and not ln.strip().startswith(("*", "<", "(")) and not is_header_line(ln)]
    if not lines:
        return None, ""
    # the title is the run of title-like lines right before the description starts
    first_desc = next((i for i, ln in enumerate(lines) if not is_title_line(ln)), len(lines))
    title_start = first_desc
    while title_start > 0 and is_title_line(lines[title_start - 1]) and first_desc - title_start < 3:
        title_start -= 1
    title_lines = lines[title_start:first_desc]
    if not title_lines:  # no title-like line: fall back to the first line
        title_lines, first_desc = lines[:1], 1
    title = " ".join(re.sub(r"^New!\s*", "", ln).strip() for ln in title_lines)
    title = re.sub(r"\s+", " ", title).strip(" :-")
    desc = " ".join(ln.strip() for ln in lines[first_desc:])
    desc = re.sub(r"\s+([.,!?])", r"\1", desc)
    desc = re.sub(r"\s+", " ", desc).strip()
    return title, desc


def parse_guide(pdf_bytes, label):
    season, year = label.split()[0], int(label.split()[1])
    pages = [(p.extract_text() or "") for p in PdfReader(io.BytesIO(pdf_bytes)).pages]
    toc = parse_toc(pages)
    offset = page_number_offset(pages, label)
    directory = parse_directory(pages)
    toc_names = {squash(n): n for n, _ in toc}

    classes = []
    current = None
    for idx, text in enumerate(pages):
        printed = idx + offset
        covering = [n for n, pg in toc if printed in pg]
        section = covering[0] if covering else None
        buf = []
        lines = merge_split_lines(text.split("\n"))
        i = 0
        while i < len(lines):
            ln = lines[i].strip()
            sq = squash(ln)
            if sq in toc_names and len(ln) < 40:
                section = toc_names[sq]
                buf = []
                i += 1
                continue
            m = SESSION_RE.search(ln)
            if m and m.start() > 0:  # e.g. "Monday/Wednesday 41883 3-5 yr ..." -- keep the label
                buf.append(ln[:m.start()])
            if m:
                if buf or current is None:
                    title, desc = split_title_description(buf)
                    current = {"title": title or "(untitled)", "description": desc,
                               "section": section, "sessions": [], "guide": label}
                    classes.append(current)
                    buf = []
                course, rest = m.group(1).replace(" ", ""), m.group(2)
                inline_meet = INLINE_MEET_RE.search(rest)
                if inline_meet:
                    rest = rest[:inline_meet.start()]
                d = SESSION_DETAIL_RE.match(rest)
                sess = {"course": course, "ages": "", "days": "", "time": "", "dates": rest}
                if d:
                    sess.update(ages=d.group("ages").strip(), days=d.group("days"),
                                time=re.sub(r"\s+", "", d.group("time")), dates=d.group("dates").strip())
                elif re.match(r"^(Under\s+)?\d", rest):
                    sess["ages"] = re.split(r"\s+(?=(?:M|T|W|Th|F|Sa|Su)\b)", rest, 1)[0]
                # meeting/location line usually follows (or was on the same line)
                meet_line = inline_meet.group(1) if inline_meet else (lines[i + 1].strip() if i + 1 < len(lines) else "")
                if meet_line:
                    mm = MEET_RE.match(meet_line)
                    if mm:
                        sess["meetings"] = int(mm.group(1))
                        sess["loc"] = mm.group(2).upper()
                        price = re.search(r"\$\s?(\d+(?:\.\d\d)?)", mm.group(3))
                        sess["price"] = f"${price.group(1)}" if price else ("Free" if "free" in mm.group(3).lower() else "")
                        if not inline_meet:
                            i += 1
                start, end = parse_dates(sess["dates"], season, year)
                sess["start"], sess["end"] = start, end
                current["sessions"].append(sess)
            else:
                buf.append(ln)
            i += 1
    return classes, directory


def interest_for(cls):
    sec = squash(cls.get("section") or "")
    interest = SECTION_INTEREST.get(sec, "Other")
    if interest in VAGUE_INTERESTS or interest == "Other":
        for rx, name in TITLE_INTEREST:
            if re.search(rx, cls["title"], re.I):
                return name
    return interest


# ---------------------------------------------------------------- placing on the map

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


def geocode(query, cache):
    if query in cache:
        return cache[query]
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": query, "format": "json", "limit": 1, "countrycodes": "us"})
    try:
        result = json.loads(fetch(url))
    except Exception as e:
        print(f"  ! geocode error for {query}: {e}")
        return None
    time.sleep(1)  # Nominatim policy: max 1 request/second
    coords = [float(result[0]["lon"]), float(result[0]["lat"])] if result else None
    cache[query] = coords
    return coords


def resolve_location(code, directory, places, cache, memo):
    if code in memo:
        return memo[code]
    result = None
    if code in CODE_TO_FACILITY and norm(CODE_TO_FACILITY[code]) in places:
        result = places[norm(CODE_TO_FACILITY[code])]
    entry = directory.get(code)
    if not result and entry:
        text = norm(entry)
        best = max((k for k in places if len(k) > 6 and k in text), key=len, default=None)
        if best:
            result = places[best]
    if not result and entry:
        m = re.search(r"(\d{2,6}(?:-[A-Z])?\s+[^,]+?),\s*(" + "|".join(CITY_ABBR) + r")\b", entry)
        if m:
            name = re.split(r"\s\d{2,6}\s", " " + entry)[0].strip() or code
            street = re.sub(r"^(\d+)-[A-Z]\b", r"\1", m.group(1).replace(" .", "."))  # drop suite "-B"
            coords = geocode(f"{street}, {CITY_ABBR[m.group(2)]}, CA", cache)
            if coords:
                result = (name, coords)
    memo[code] = result
    return result


def main():
    today = dt.date.today()
    places = load_places()
    cache = json.load(open(GEOCODE_CACHE_PATH, encoding="utf-8")) if os.path.exists(GEOCODE_CACHE_PATH) else {}

    guides = find_guides()
    print("Guides on the website:", ", ".join(g for g, _ in guides) or "none found")
    if not guides:
        raise SystemExit("No guides found on the Current Recreation Guides page -- leaving classes.js unchanged.")

    groups = OrderedDict()
    unresolved, n_sessions = {}, 0
    memo = {}
    parsed_any = False
    for label, url in guides:
        try:
            classes, directory = parse_guide(fetch(url, binary=True), label)
        except Exception as err:  # one unreadable guide shouldn't block the others
            print(f"  ! couldn't read the {label} guide ({url}): {err}")
            continue
        parsed_any = True
        print(f"{label}: {len(classes)} classes, {sum(len(c['sessions']) for c in classes)} sessions, "
              f"{len(directory)} directory codes")
        for cls in classes:
            interest = interest_for(cls)
            for s in cls["sessions"]:
                if s["end"] and s["end"] < today:
                    continue
                n_sessions += 1
                loc = resolve_location(s.get("loc", ""), directory, places, cache, memo) if s.get("loc") else None
                if not loc:
                    unresolved[s.get("loc") or "(none)"] = unresolved.get(s.get("loc") or "(none)", 0) + 1
                    continue
                venue, coords = loc
                key = (cls["title"].lower(), venue)
                g = groups.get(key)
                if not g:
                    g = groups[key] = {"cls": cls, "interest": interest, "venue": venue,
                                       "coords": coords, "sessions": []}
                g["sessions"].append(s)

    if not parsed_any:
        raise SystemExit("Couldn't read any guide PDF -- leaving classes.js unchanged.")

    features = []
    for g in groups.values():
        cls, sessions = g["cls"], sorted(g["sessions"], key=lambda s: s["start"] or dt.date.max)
        ranges = [r for r in (parse_ages(s["ages"]) for s in sessions) if r]
        groups_ = sorted({a for lo, hi in ranges for a in age_groups(lo, hi)},
                         key=[x[0] for x in AGE_GROUPS].index) or ["All Ages"]
        ages_text = ", ".join(OrderedDict.fromkeys(s["ages"] for s in sessions if s["ages"]))
        starts = [s["start"] for s in sessions if s["start"]]
        ends = [s["end"] for s in sessions if s["end"]]
        desc = cls["description"]
        if len(desc) > 280:
            desc = desc[:277].rsplit(" ", 1)[0] + "…"
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": g["coords"]},
            "properties": {
                "source": "Class",
                "name": cls["title"],
                "interest": g["interest"],
                "section": cls["section"],
                "age_groups": groups_,
                "ages": ages_text,
                "venue": g["venue"],
                "description": desc,
                "guide": cls["guide"],
                "start": min(starts).isoformat() if starts else None,
                "end": max(ends).isoformat() if ends else None,
                "sessions": [{
                    "course": s["course"], "ages": s["ages"], "days": s["days"], "time": s["time"],
                    "dates": s["dates"], "price": s.get("price", ""), "meetings": s.get("meetings"),
                    "start": s["start"].isoformat() if s["start"] else None,
                    "end": s["end"].isoformat() if s["end"] else None,
                } for s in sessions],
                "website": REGISTER_SEARCH + urllib.parse.quote(cls["title"]),
            },
        })

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write("// Generated by build_classes.py from the HARD Recreation Guide -- do not edit by hand.\n")
        f.write("const CLASSES_DATA = ")
        json.dump({"type": "FeatureCollection", "guides": [g for g, _ in guides], "features": features},
                  f, ensure_ascii=False)
        f.write(";\n")
    with open(GEOCODE_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=1, sort_keys=True)

    print(f"Wrote {len(features)} classes ({n_sessions} upcoming sessions) to {OUT_PATH}")
    if unresolved:
        print("Sessions skipped -- location code not placed:",
              ", ".join(f"{k} ({v})" for k, v in sorted(unresolved.items(), key=lambda x: -x[1])))


if __name__ == "__main__":
    main()
