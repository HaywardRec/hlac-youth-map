"""
Builds map_data.js for the HARD Facilities map.

Sources:
  - HARD_CommunityCenter.shp  (36 pts) -> enriched with amenity/rental data from HARD_Facilities.xlsx
  - CoH_CommCenter.shp        (15 pts) -> City of Hayward / partner facilities
  - HUSD_Center.shp           (51 pts) -> Hayward Unified School District sites
  - HARD_Boundaries.shp       (1 polygon) -> district outline, drawn as a reference layer

Local businesses (e.g. boba shops) are NOT built here -- see build_businesses.py, which
fetches them from Google Places and merges them into this same map_data.js under "business".

A handful of HARD facilities from the spreadsheet have no matching point in
HARD_CommunityCenter.shp (different official name, or genuinely missing from
that layer). Those are geocoded from their street address via Nominatim
(OpenStreetMap) as a one-time fallback.

Output: map_data.js (a single `const MAP_DATA = {...}` for the Leaflet page)
"""
import json
import os
import time
import urllib.parse
import urllib.request

import geopandas as gpd
import openpyxl

SHP_DIR = r"C:\Users\Thomas\OneDrive - Hayward Area Recreation and Park District\Documents\ArcGIS\Projects\FacilityRentals"
XLSX_PATH = r"C:\Users\Thomas\AI\HARD_Facilities.xlsx"
BOUNDARY_SHP = r"C:\Users\Thomas\OneDrive - Hayward Area Recreation and Park District\Documents\ArcGIS\Projects\Random Shapefiles\HARD_Boundaries.shp"
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "map_data.js")

# Park polygons -- used to place spreadsheet parks that aren't in HARD_CommunityCenter.shp
# (most neighborhood parks). Each polygon is reduced to a point guaranteed inside it.
PARKS_SHP = r"C:\Users\Thomas\OneDrive - Hayward Area Recreation and Park District\Documents\ArcGIS\Projects\Random Shapefiles\HARD_Parks_9_20_26.shp"

# Excel "Facility" name -> HARD_Parks_9_20_26.shp "Name_1" value, where they differ
PARK_NAME_OVERRIDES = {
    "Weekes Community Park": "Weekes Park",
    "Eden Greenway Dog Park": "Eden Greenway",
    "Eden Greenway Fitness Court": "Eden Greenway",
    "Greenbelt Trails": "Greenbelt Riding Hiking Trails",
    "La Placita Mini Park": "La Placita Park",
    "Children's Park at Giuliani Plaza": "Childrens Park at Giuliani Plaza",
    "Tennyson Park Fitness Court": "Tennyson Park",
}

# Excel "Facility" name -> HARD_CommunityCenter.shp "Name_1" value
NAME_OVERRIDES = {
    "Adobe Art Center": "Adobe Art Center and Gallery",
    "Kenneth C. Aitken Senior & Community Center": "Kenneth C. Aitken Senior Community Center",
    "Weekes Community Center": "Weekes Park Community Center",
    "Douglas Morrisson Theatre": "Douglas Morrisson Theater",
    "Hayward Shoreline Interpretive Center": "Shoreline Interpretive Center",
    "Mission Hills of Hayward Golf Course": "Mission Hills Of Hayward Golf Course",
    "McConaghy House": "McConaghy Park",  # historic house sits inside McConaghy Park
    "Matt Jimenez Gymnasium": "Matt Jimenez Community Center",  # gym is part of the CC complex
    "Old Creek Dog Park of Cherryland": "Old Creek Dog Park",
    "Alden E. Oliver Sports Park Fitness Court": "Alden E. Oliver Sports Park",  # sub-feature of the sports park
}

# Excel facilities with no usable match anywhere in HARD_CommunityCenter.shp;
# geocoded from their spreadsheet address instead.
GEOCODE_FALLBACK = {
    "Mt. Eden Mansion", "PhotoCentral", "Arroyo Swim Center", "Castro Valley Swim Center",
    "Laurel Park", "Martin Luther King Field",  # not in the parks layer either
}

# Typos in the source shapefile's Name_1 / SubCategor attributes, corrected on output.
NAME_1_FIXES = {"Bohannon Athletoc Fields": "Bohannon Athletic Fields"}
SUBCATEGORY_FIXES = {"Sporst": "Sports"}

RELEVANT_SHEETS = [
    "Community Centers & Recreation ",
    "Special & Cultural Facilities",
    "Aquatics",
    "Parks - Named-Numbered Reservab",
    "Parks - Picnic Tables (Unnamed-",
    "Parks - No Picnic Tables - Pass",
]


def load_excel():
    wb = openpyxl.load_workbook(XLSX_PATH, data_only=True)
    records = {}
    for sheet in RELEVANT_SHEETS:
        ws = wb[sheet]
        rows = list(ws.iter_rows(values_only=True))
        header = [h.strip() if h else h for h in rows[0]]
        for r in rows[1:]:
            if not r[0]:
                continue
            rec = dict(zip(header, r))
            rec["_sheet"] = sheet.strip()
            records[r[0].strip()] = rec
    return records


def geocode(address):
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": address, "format": "json", "limit": 1}
    )
    req = urllib.request.Request(url, headers={"User-Agent": "hard-facilities-map/1.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read())
    time.sleep(1)  # Nominatim usage policy: max 1 req/sec
    if not data:
        return None
    return float(data[0]["lat"]), float(data[0]["lon"])


def build_hard_layer(excel_records):
    gdf = gpd.read_file(SHP_DIR + "/HARD_CommunityCenter.shp").to_crs(4326)
    by_shp_name = {row["Name_1"]: row for _, row in gdf.iterrows()}

    parks = gpd.read_file(PARKS_SHP).to_crs(2227)  # project before taking interior points
    parks["geometry"] = parks.geometry.representative_point()
    parks = parks.to_crs(4326)
    park_points = {row["Name_1"].strip(): row.geometry for _, row in parks.iterrows() if row["Name_1"]}

    used_shp_names = set()
    features = []

    for facility_name, rec in excel_records.items():
        shp_name = NAME_OVERRIDES.get(facility_name, facility_name)
        row = by_shp_name.get(shp_name)

        if row is not None:
            lat, lon = row.geometry.y, row.geometry.x
            subcategory = SUBCATEGORY_FIXES.get(row["SubCategor"], row["SubCategor"])
            used_shp_names.add(shp_name)
        elif PARK_NAME_OVERRIDES.get(facility_name, facility_name) in park_points:
            pt = park_points[PARK_NAME_OVERRIDES.get(facility_name, facility_name)]
            lat, lon = pt.y, pt.x
            subcategory = "Sports" if rec.get("Type") == "Athletic Field" else "Park"
        elif facility_name in GEOCODE_FALLBACK:
            addr = rec.get("Address", "")
            print(f"Geocoding: {facility_name} -> {addr}")
            coords = geocode(addr + ", CA")
            if not coords:
                print(f"  ! geocode failed for {facility_name}, skipping")
                continue
            lat, lon = coords
            if "Swim" in facility_name:
                subcategory = "Pool"
            elif rec["_sheet"].startswith("Parks"):
                subcategory = "Sports" if rec.get("Type") == "Athletic Field" else "Park"
            else:
                subcategory = "Center"
        else:
            print(f"  ! no shapefile match and not in geocode fallback: {facility_name}")
            continue

        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {
                    "source": "HARD",
                    "name": facility_name,
                    "category": rec["_sheet"],
                    "subcategory": subcategory,
                    "address": rec.get("Address"),
                    "amenities": rec.get("Amenities"),
                    "reservable_rooms": rec.get("Reservable Rooms"),
                    "reservable_other": rec.get("Reservable Picnic Tables")
                    or rec.get("Reservable Picnic Areas")
                    or rec.get("Reservable Picnic/Other")
                    or rec.get("Reservable Spaces")
                    or rec.get("Picnic Tables"),
                    "status": rec.get("Status"),
                    "website": rec.get("Website"),
                },
            }
        )

    # Any HARD_CommunityCenter.shp points with no spreadsheet enrichment still show on the map.
    for shp_name, row in by_shp_name.items():
        if shp_name in used_shp_names:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [row.geometry.x, row.geometry.y]},
                "properties": {
                    "source": "HARD",
                    "name": NAME_1_FIXES.get(shp_name, shp_name),
                    "category": None,
                    "subcategory": SUBCATEGORY_FIXES.get(row["SubCategor"], row["SubCategor"]),
                    "address": None,
                    "amenities": None,
                    "reservable_rooms": None,
                    "reservable_other": None,
                    "status": None,
                    "website": None,
                },
            }
        )

    return features


def build_coh_layer():
    gdf = gpd.read_file(SHP_DIR + "/CoH_CommCenter.shp").to_crs(4326)
    features = []
    for _, row in gdf.iterrows():
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [row.geometry.x, row.geometry.y]},
                "properties": {
                    "source": "CoH",
                    "name": row["Name"],
                    "category": row["Category"],
                    "subcategory": row["SubCategor"],
                    "address": row["Address"],
                    "primary_operator": row["PrimaryOpe"],
                    "secondary_operator": row["SecondaryO"],
                    "status": row["Status"],
                    "notes": row["Notes"],
                },
            }
        )
    return features


def build_husd_layer():
    gdf = gpd.read_file(SHP_DIR + "/HUSD_Center.shp").to_crs(4326)
    features = []
    for _, row in gdf.iterrows():
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [row.geometry.x, row.geometry.y]},
                "properties": {
                    "source": "HUSD",
                    "name": row["Site"],
                    "category": row["Type"],
                    "subcategory": row["Org"],
                    "address": f"{row['Address']}, {row['City']}, {row['State']}",
                },
            }
        )
    return features


def build_boundary():
    gdf = gpd.read_file(BOUNDARY_SHP).to_crs(4326)
    geom = gdf.geometry.iloc[0]
    return {
        "type": "Feature",
        "geometry": json.loads(gpd.GeoSeries([geom]).to_json())["features"][0]["geometry"],
        "properties": {"name": gdf.iloc[0]["dist_name"]},
    }


def main():
    excel_records = load_excel()
    hard_features = build_hard_layer(excel_records)
    coh_features = build_coh_layer()
    husd_features = build_husd_layer()
    boundary = build_boundary()

    # Keep every layer this script doesn't build ("business" from build_businesses.py,
    # "landmark" from build_landmarks.py, ...) rather than wiping them out on each rebuild.
    existing = {}
    try:
        with open(OUT_PATH, "r", encoding="utf-8") as f:
            existing = json.loads(f.read().removeprefix("const MAP_DATA = ").strip().removesuffix(";"))
    except FileNotFoundError:
        pass
    existing_business = existing.get("business", {"type": "FeatureCollection", "features": []})

    data = dict(existing)
    data.update({
        "hard": {"type": "FeatureCollection", "features": hard_features},
        "coh": {"type": "FeatureCollection", "features": coh_features},
        "husd": {"type": "FeatureCollection", "features": husd_features},
        "boundary": boundary,
        "business": existing_business,
    })

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write("const MAP_DATA = ")
        json.dump(data, f, indent=None)
        f.write(";\n")

    print()
    print(f"HARD:      {len(hard_features)} features")
    print(f"CoH:       {len(coh_features)} features")
    print(f"HUSD:      {len(husd_features)} features")
    print(f"Business:  {len(existing_business['features'])} features (unchanged; run build_businesses.py to refresh)")
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
