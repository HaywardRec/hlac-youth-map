"""
Geocodes a small hand-picked list of specific named venues that teens called out in the
HLAC youth survey ("Gilman Music Club", "Castro Valley Bowl", etc.) -- places that are too
specific/one-off to find via a generic Google Places category search. Uses free Nominatim
(OpenStreetMap) geocoding, no API key required.

Merges results into map_data.js under a new "landmark" key, alongside hard/coh/husd/
boundary/business (built by build_map_data.py / build_businesses.py respectively).

Run this once; re-run only if LANDMARKS below changes. Prints each result's resolved
address so you can sanity-check the geocode actually found the right place before trusting it.
"""
import json
import time
import urllib.parse
import urllib.request

MAP_DATA_PATH = r"C:\Users\Thomas\OneDrive - Hayward Area Recreation and Park District\Documents\Work\Misc\HLAC - Youth Map\map_data.js"

# name -> (search query, category label for the sidebar filter, emoji icon key used in map.html)
LANDMARKS = {
    "Lake Chabot Regional Park": ("Lake Chabot Regional Park", "Regional Park / Lake"),
    "924 Gilman (All-Ages Music Venue)": ("924 Gilman Street, Berkeley, CA", "Music Venue"),
    "Castro Village Bowl": ("Castro Village Bowl, Castro Valley, CA", "Bowling Alley"),
    "Castro Valley Marketplace": ("Castro Valley Marketplace, Castro Valley, CA", "Shopping Center"),
    "Garin Regional Park (Fairmont Ridge trail access)": ("Garin Regional Park, Hayward, CA", "Hiking Trailhead"),
}


def geocode(query):
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": query, "format": "json", "limit": 1}
    )
    req = urllib.request.Request(url, headers={"User-Agent": "hard-facilities-map/1.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read())
    time.sleep(1)  # Nominatim usage policy: max 1 req/sec
    if not data:
        return None
    return data[0]


def main():
    with open(MAP_DATA_PATH, "r", encoding="utf-8") as f:
        data = json.loads(f.read().removeprefix("const MAP_DATA = ").removesuffix(";\n"))

    features = []
    for name, (query, category) in LANDMARKS.items():
        print(f"Geocoding: {name} -> {query!r}")
        hit = geocode(query)
        if not hit:
            print(f"  ! no result, skipping")
            continue
        print(f"  -> {hit['display_name']}  ({hit['lat']}, {hit['lon']})")
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [float(hit["lon"]), float(hit["lat"])]},
                "properties": {
                    "source": "Landmark",
                    "name": name,
                    "category": category,
                    "subcategory": category,
                    "address": hit["display_name"],
                },
            }
        )

    data["landmark"] = {"type": "FeatureCollection", "features": features}

    with open(MAP_DATA_PATH, "w", encoding="utf-8") as f:
        f.write("const MAP_DATA = ")
        json.dump(data, f, indent=None)
        f.write(";\n")

    print()
    print(f"Landmark layer: {len(features)} features")
    print(f"Wrote {MAP_DATA_PATH}")


if __name__ == "__main__":
    main()
