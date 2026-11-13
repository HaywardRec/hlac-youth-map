"""
Fetches local businesses (e.g. boba shops) inside the HARD district boundary via the
Google Places API (New) and merges them into map_data.js under the "business" key,
alongside whatever build_map_data.py already wrote there (hard/coh/husd/boundary).

Requires an environment variable GOOGLE_PLACES_API_KEY set to a key with the
"Places API (New)" enabled in Google Cloud Console. The key is never written to disk.

To add another category later (pizza, ice cream, etc.), add an entry to SEARCH_QUERIES
and re-run -- only the categories being re-queried are replaced; other categories already
in map_data.js are left alone.

Google's Places API terms require attributing "Google" near any UI that shows this data
and refreshing/deleting cached results periodically rather than treating them as
permanent -- re-run this script every month or so. See:
https://cloud.google.com/maps-platform/terms
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

import geopandas as gpd
from shapely.geometry import Point, shape

BOUNDARY_SHP = r"C:\Users\Thomas\OneDrive - Hayward Area Recreation and Park District\Documents\ArcGIS\Projects\Random Shapefiles\HARD_Boundaries.shp"
MAP_DATA_PATH = r"C:\Users\Thomas\OneDrive - Hayward Area Recreation and Park District\Documents\Work\Misc\HLAC - Youth Map\map_data.js"

API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY")
PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = (
    "places.id,places.displayName,places.formattedAddress,places.location,"
    "places.types,places.rating,places.userRatingCount,places.priceLevel,"
    "places.businessStatus,nextPageToken"
)

# category label shown in the sidebar filter -> Places text-search query.
# Add more entries here to expand beyond boba shops.
SEARCH_QUERIES = {
    "Boba / Bubble Tea": "boba tea shop",
    "Arcade": "arcade",
    "Mall / Shopping Center": "shopping mall",
    "Movie Theater": "movie theater",
    "Coffee Shop": "coffee shop",
    "Bowling Alley": "bowling alley",
    "Dance Studio": "dance studio",
    "Art / Craft Studio": "art studio",
    "Cooking Class": "cooking class",
    "Gym / Fitness Center": "gym",
    "Martial Arts Studio": "martial arts",
    "Board Game Cafe": "board game cafe",
    "Game Shop": "tabletop game store",
}

PRICE_LEVELS = {
    "PRICE_LEVEL_FREE": "Free",
    "PRICE_LEVEL_INEXPENSIVE": "$",
    "PRICE_LEVEL_MODERATE": "$$",
    "PRICE_LEVEL_EXPENSIVE": "$$$",
    "PRICE_LEVEL_VERY_EXPENSIVE": "$$$$",
}


def load_boundary():
    gdf = gpd.read_file(BOUNDARY_SHP).to_crs(4326)
    return gdf.geometry.iloc[0]


def places_text_search(query, bounds):
    minx, miny, maxx, maxy = bounds
    results = []
    page_token = None
    for page in range(3):  # Places API caps at 20 results/page, 3 pages (60 total)
        body = {
            "textQuery": query,
            "maxResultCount": 20,
            "locationRestriction": {
                "rectangle": {
                    "low": {"latitude": miny, "longitude": minx},
                    "high": {"latitude": maxy, "longitude": maxx},
                }
            },
        }
        if page_token:
            body["pageToken"] = page_token
        req = urllib.request.Request(
            PLACES_URL,
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": API_KEY,
                "X-Goog-FieldMask": FIELD_MASK,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            print(f"  ! Places API error {e.code}: {e.read().decode()}")
            break
        results.extend(data.get("places", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
        time.sleep(2)  # next_page_token needs a moment to become valid
    return results


def to_feature(place, category):
    loc = place["location"]
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [loc["longitude"], loc["latitude"]]},
        "properties": {
            "source": "Business",
            "name": place.get("displayName", {}).get("text"),
            "category": category,
            "subcategory": category,
            "address": place.get("formattedAddress"),
            "rating": place.get("rating"),
            "user_ratings_total": place.get("userRatingCount"),
            "price_level": PRICE_LEVELS.get(place.get("priceLevel")),
            "status": place.get("businessStatus"),
        },
    }


def main():
    if not API_KEY:
        sys.exit("Set the GOOGLE_PLACES_API_KEY environment variable before running this script.")

    boundary = load_boundary()
    bounds = boundary.bounds

    with open(MAP_DATA_PATH, "r", encoding="utf-8") as f:
        data = json.loads(f.read().removeprefix("const MAP_DATA = ").removesuffix(";\n"))

    existing_features = data.get("business", {}).get("features", [])
    # drop old results for categories we're about to re-query; keep everything else
    kept = [f for f in existing_features if f["properties"]["category"] not in SEARCH_QUERIES]

    new_features = []
    for category, query in SEARCH_QUERIES.items():
        print(f"Searching: {query!r}...")
        places = places_text_search(query, bounds)
        in_district = 0
        for place in places:
            loc = place["location"]
            if boundary.contains(Point(loc["longitude"], loc["latitude"])):
                new_features.append(to_feature(place, category))
                in_district += 1
        print(f"  {len(places)} results, {in_district} inside the district boundary")

    data["business"] = {"type": "FeatureCollection", "features": kept + new_features}

    with open(MAP_DATA_PATH, "w", encoding="utf-8") as f:
        f.write("const MAP_DATA = ")
        json.dump(data, f, indent=None)
        f.write(";\n")

    print()
    print(f"Business layer: {len(kept) + len(new_features)} features ({len(new_features)} refreshed just now)")
    print(f"Wrote {MAP_DATA_PATH}")


if __name__ == "__main__":
    main()
