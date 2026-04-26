#!/usr/bin/env python3
"""Build a 30-year Charleston-area flood history dataset from NOAA Storm Events.

Study cities:
- Charleston, SC
- North Charleston, SC
- Summerville, SC
- Goose Creek, SC
- Hanahan, SC

Data source:
- NOAA NCEI Storm Events bulk CSV (details table), yearly files.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import math
import re
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = PROJECT_ROOT / "data" / "processed" / "charleston_floods_30y.json"

NOAA_DIR = "https://www.ncei.noaa.gov/pub/data/swdi/stormevents/csvfiles/"
NOAA_INDEX_URL = NOAA_DIR

START_YEAR = 1995
END_YEAR = 2024
CITY_RADIUS_MILES = 20.0

FLOOD_EVENT_TYPES = {
    "Flood",
    "Flash Flood",
    "Coastal Flood",
    "Lakeshore Flood",
    "Storm Surge/Tide",
}

CITIES = [
    {"key": "charleston", "name": "Charleston, SC", "lat": 32.7765, "lon": -79.9311},
    {"key": "north_charleston", "name": "North Charleston, SC", "lat": 32.8546, "lon": -79.9748},
    {"key": "summerville", "name": "Summerville, SC", "lat": 33.0185, "lon": -80.1756},
    {"key": "goose_creek", "name": "Goose Creek, SC", "lat": 32.9810, "lon": -80.0326},
    {"key": "hanahan", "name": "Hanahan, SC", "lat": 32.9185, "lon": -80.0220},
]


@dataclass
class FloodEvent:
    event_id: int
    episode_id: int
    year: int
    month: int
    date_time: str
    event_type: str
    state: str
    county: str
    begin_lat: float
    begin_lon: float
    end_lat: float
    end_lon: float
    injuries: int
    deaths: int
    property_damage_usd: float
    crops_damage_usd: float
    narrative: str


def fetch_index() -> str:
    with urllib.request.urlopen(NOAA_INDEX_URL, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="replace")


def resolve_year_file(index_html: str, year: int) -> str:
    pat = re.compile(rf"(StormEvents_details-ftp_v1\.0_d{year}_c\d+\.csv\.gz)")
    m = pat.search(index_html)
    if not m:
        raise RuntimeError(f"NOAA yearly file for {year} not found in index")
    return NOAA_DIR + m.group(1)


def parse_damage_to_usd(text: str) -> float:
    text = (text or "").strip().upper()
    if not text:
        return 0.0
    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)([KMB]?)$", text)
    if not m:
        return 0.0
    value = float(m.group(1))
    suffix = m.group(2)
    mult = {"": 1.0, "K": 1_000.0, "M": 1_000_000.0, "B": 1_000_000_000.0}[suffix]
    return value * mult


def parse_int(text: str, default: int = 0) -> int:
    try:
        return int(float(text))
    except Exception:
        return default


def parse_float(text: str, default: float = 0.0) -> float:
    try:
        return float(text)
    except Exception:
        return default


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3958.8
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lon / 2) ** 2
    )
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def event_near_city(event: FloodEvent, city: dict, radius_miles: float) -> bool:
    return (
        haversine_miles(city["lat"], city["lon"], event.begin_lat, event.begin_lon) <= radius_miles
        or haversine_miles(city["lat"], city["lon"], event.end_lat, event.end_lon) <= radius_miles
    )


def quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    i = int(q * (len(ordered) - 1))
    return ordered[i]


def build_risk_zones(city: dict, events: list[FloodEvent]) -> list[dict]:
    # ~1.4 miles grid, enough detail for neighborhood-scale context.
    step = 0.02
    # Extra-tight kernel so high-risk cells hug observed flood points.
    sigma_mi = 1.0
    # Zero contribution beyond this distance from an observed flood point.
    influence_cutoff_mi = 3.0
    # Cells farther than this from any event are forced to Low.
    low_only_cutoff_mi = 3.8
    lat_span = 0.33
    lon_span = 0.42

    lat_c = city["lat"]
    lon_c = city["lon"]
    lat_min = round(lat_c - lat_span, 6)
    lat_max = round(lat_c + lat_span, 6)
    lon_min = round(lon_c - lon_span, 6)
    lon_max = round(lon_c + lon_span, 6)

    two_sigma_sq = 2.0 * sigma_mi ** 2

    weighted_events: list[tuple[float, float, float]] = []
    for ev in events:
        # Weight severe impacts stronger in the surface.
        damage_weight = math.log1p(ev.property_damage_usd + ev.crops_damage_usd) / 8.0
        impact_weight = ev.injuries * 1.8 + ev.deaths * 4.0
        kind_weight = 1.6 if ev.event_type in {"Coastal Flood", "Storm Surge/Tide"} else 1.0
        w = 1.0 + damage_weight + impact_weight
        w *= kind_weight
        weighted_events.append((ev.begin_lat, ev.begin_lon, w))

    cells: list[dict] = []
    lat = lat_min
    while lat < lat_max - 1e-9:
        lon = lon_min
        while lon < lon_max - 1e-9:
            cx = lat + step / 2
            cy = lon + step / 2
            score = 0.0
            nearest_event_mi = float("inf")
            for elat, elon, ew in weighted_events:
                d = haversine_miles(cx, cy, elat, elon)
                if d < nearest_event_mi:
                    nearest_event_mi = d
                if d <= influence_cutoff_mi:
                    score += ew * math.exp(-(d ** 2) / two_sigma_sq)

            if nearest_event_mi > low_only_cutoff_mi:
                score = 0.0

            cells.append(
                {
                    "bbox": [round(lon, 6), round(lat, 6), round(lon + step, 6), round(lat + step, 6)],
                    "score": round(score, 4),
                }
            )
            lon = round(lon + step, 6)
        lat = round(lat + step, 6)

    scores = [c["score"] for c in cells]
    nonzero = [s for s in scores if s > 0]
    q1 = quantile(nonzero, 0.30)
    q2 = quantile(nonzero, 0.55)
    q3 = quantile(nonzero, 0.75)
    q4 = quantile(nonzero, 0.88)
    max_score = max(scores) if scores else 1.0
    log_max = math.log1p(max_score) if max_score > 0 else 1.0

    for c in cells:
        s = c["score"]
        if s == 0 or s <= q1:
            level = "Low"
        elif s <= q2:
            level = "Guarded"
        elif s <= q3:
            level = "Elevated"
        elif s <= q4:
            level = "High"
        else:
            level = "Most Affected"
        c["level"] = level
        c["scoreNorm"] = round(math.log1p(s) / log_max, 4) if log_max > 0 else 0.0
        c["city"] = city["key"]

    return cells


def read_year_events(url: str) -> list[FloodEvent]:
    with urllib.request.urlopen(url, timeout=120) as resp:
        raw = resp.read()

    out: list[FloodEvent] = []
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
        text = io.TextIOWrapper(gz, encoding="utf-8", errors="replace")
        reader = csv.DictReader(text)
        for row in reader:
            if row.get("STATE", "").strip().upper() != "SOUTH CAROLINA":
                continue
            evtype = (row.get("EVENT_TYPE", "") or "").strip()
            if evtype not in FLOOD_EVENT_TYPES:
                continue

            begin_lat = parse_float(row.get("BEGIN_LAT", "0"))
            begin_lon = parse_float(row.get("BEGIN_LON", "0"))
            if begin_lat == 0.0 and begin_lon == 0.0:
                # Skip unmappable rows; this map is coordinate-based.
                continue

            end_lat = parse_float(row.get("END_LAT", "0"), begin_lat)
            end_lon = parse_float(row.get("END_LON", "0"), begin_lon)
            if end_lat == 0.0 and end_lon == 0.0:
                end_lat, end_lon = begin_lat, begin_lon

            injuries = parse_int(row.get("INJURIES_DIRECT", "0")) + parse_int(row.get("INJURIES_INDIRECT", "0"))
            deaths = parse_int(row.get("DEATHS_DIRECT", "0")) + parse_int(row.get("DEATHS_INDIRECT", "0"))
            prop_usd = parse_damage_to_usd(row.get("DAMAGE_PROPERTY", ""))
            crop_usd = parse_damage_to_usd(row.get("DAMAGE_CROPS", ""))

            out.append(
                FloodEvent(
                    event_id=parse_int(row.get("EVENT_ID", "0")),
                    episode_id=parse_int(row.get("EPISODE_ID", "0")),
                    year=parse_int(row.get("YEAR", "0")),
                    month=parse_int(row.get("MONTH_NAME", "0"), 0),
                    date_time=row.get("BEGIN_DATE_TIME", ""),
                    event_type=evtype,
                    state=row.get("STATE", ""),
                    county=row.get("CZ_NAME", ""),
                    begin_lat=begin_lat,
                    begin_lon=begin_lon,
                    end_lat=end_lat,
                    end_lon=end_lon,
                    injuries=injuries,
                    deaths=deaths,
                    property_damage_usd=prop_usd,
                    crops_damage_usd=crop_usd,
                    narrative=(row.get("EVENT_NARRATIVE", "") or row.get("EPISODE_NARRATIVE", "") or "")[:800],
                )
            )

    return out


def month_from_datetime(text: str) -> int:
    for fmt in ("%d-%b-%y %H:%M:%S", "%d-%b-%y %H:%M:%S %z"):
        try:
            return datetime.strptime(text.strip(), fmt).month
        except Exception:
            pass
    return 0


def main() -> None:
    index_html = fetch_index()
    year_files = {y: resolve_year_file(index_html, y) for y in range(START_YEAR, END_YEAR + 1)}

    all_floods: list[FloodEvent] = []
    for y in range(START_YEAR, END_YEAR + 1):
        url = year_files[y]
        events = read_year_events(url)
        all_floods.extend(events)
        print(f"Loaded {len(events):4d} SC flood events for {y}")

    city_events: dict[str, list[FloodEvent]] = {c["key"]: [] for c in CITIES}
    for ev in all_floods:
        for city in CITIES:
            if event_near_city(ev, city, CITY_RADIUS_MILES):
                city_events[city["key"]].append(ev)

    seen: set[int] = set()
    combined: list[FloodEvent] = []
    for city in CITIES:
        for ev in city_events[city["key"]]:
            if ev.event_id not in seen:
                seen.add(ev.event_id)
                combined.append(ev)

    risk_zones = {c["key"]: build_risk_zones(c, city_events[c["key"]]) for c in CITIES}

    # Build stats used by UI decision analysis section.
    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    city_stats = {}
    for city in CITIES:
        key = city["key"]
        evs = city_events[key]
        monthly = {m: 0 for m in month_names}
        by_type: dict[str, int] = {}
        by_year: dict[int, int] = {}
        total_damage = 0.0
        injuries = 0
        deaths = 0

        for ev in evs:
            m = month_from_datetime(ev.date_time)
            if 1 <= m <= 12:
                monthly[month_names[m - 1]] += 1
            by_type[ev.event_type] = by_type.get(ev.event_type, 0) + 1
            by_year[ev.year] = by_year.get(ev.year, 0) + 1
            total_damage += ev.property_damage_usd + ev.crops_damage_usd
            injuries += ev.injuries
            deaths += ev.deaths

        peak_months = sorted(monthly.items(), key=lambda x: x[1], reverse=True)[:3]
        city_stats[key] = {
            "eventCount": len(evs),
            "avgPerYear": round(len(evs) / 30.0, 2),
            "monthlyCounts": monthly,
            "peakMonths": [{"month": m, "count": c} for m, c in peak_months],
            "eventTypeCounts": by_type,
            "yearlyCounts": [{"year": y, "count": by_year.get(y, 0)} for y in range(START_YEAR, END_YEAR + 1)],
            "totalDamageUSD": round(total_damage, 2),
            "injuries": injuries,
            "deaths": deaths,
        }

    # Regional aggregates.
    regional_monthly = {m: 0 for m in month_names}
    regional_type: dict[str, int] = {}
    regional_damage = 0.0
    regional_inj = 0
    regional_deaths = 0
    for ev in combined:
        m = month_from_datetime(ev.date_time)
        if 1 <= m <= 12:
            regional_monthly[month_names[m - 1]] += 1
        regional_type[ev.event_type] = regional_type.get(ev.event_type, 0) + 1
        regional_damage += ev.property_damage_usd + ev.crops_damage_usd
        regional_inj += ev.injuries
        regional_deaths += ev.deaths

    output = {
        "meta": {
            "generated_utc": datetime.now(UTC).isoformat(),
            "year_range": [START_YEAR, END_YEAR],
            "source": {
                "noaa_stormevents": NOAA_INDEX_URL,
                "noaa_readme": NOAA_DIR + "README",
                "flood_safety": "https://www.weather.gov/safety/flood",
                "nfip_insurance": "https://www.floodsmart.gov/get-insured/buy-a-policy",
                "naic_auto_claims": "https://content.naic.org/sites/default/files/publication-aut-pb-auto-insurance-database.pdf",
            },
            "city_radius_miles": CITY_RADIUS_MILES,
            "cities": CITIES,
            "counts": {c["key"]: len(city_events[c["key"]]) for c in CITIES},
            "total_unique_events": len(combined),
        },
        "places": CITIES,
        "floodEvents": [
            {
                "id": ev.event_id,
                "episodeId": ev.episode_id,
                "year": ev.year,
                "dateTime": ev.date_time,
                "eventType": ev.event_type,
                "county": ev.county,
                "start": {"lat": ev.begin_lat, "lon": ev.begin_lon},
                "end": {"lat": ev.end_lat, "lon": ev.end_lon},
                "injuries": ev.injuries,
                "deaths": ev.deaths,
                "propertyDamageUSD": round(ev.property_damage_usd, 2),
                "cropDamageUSD": round(ev.crops_damage_usd, 2),
                "narrative": ev.narrative,
            }
            for ev in combined
        ],
        "riskZones": risk_zones,
        "stats": {
            "regional": {
                "eventCount": len(combined),
                "avgPerYear": round(len(combined) / 30.0, 2),
                "monthlyCounts": regional_monthly,
                "eventTypeCounts": regional_type,
                "totalDamageUSD": round(regional_damage, 2),
                "injuries": regional_inj,
                "deaths": regional_deaths,
            },
            "cities": city_stats,
            "decisionAnalysis": {
                "questions": {
                    "how_often": "How often does flooding happen here?",
                    "when": "When does flooding usually happen?",
                    "home_or_car_or_stay": "What should people do if flooding threatens their home or car?",
                    "safety": "What are the key safety measures?",
                    "insurance_auto": "How often does insurance pay for flooded cars?",
                    "how_people_deal": "How do people in Charleston-area communities live with flood risk?",
                },
                "answers": {
                    "how_often": "Use city event counts and average-per-year metrics below. These are NOAA Storm Events flood-family incidents within 20 miles of each city center.",
                    "when": "Use peak-month outputs from NOAA event timing. Coastal and tropical-season months are typically the highest in this region.",
                    "home_or_car_or_stay": "NWS guidance is explicit: never drive through flooded roads (Turn Around, Don't Drown). During active flood warning, move to higher ground and avoid walking/driving in floodwater.",
                    "safety": "Before: know your zone/routes and keep emergency kit. During: follow warnings, avoid floodwater, evacuate when instructed. After: avoid contaminated water and electrical hazards.",
                    "insurance_auto": "Best available open dataset proxy found via DuckDuckGo: the NAIC Auto Insurance Database report publishes state-level comprehensive-coverage claim frequency/loss metrics (the coverage that generally handles flood vehicle damage). It is not flood-only and not city-level, so this app treats it as context while city flood frequency comes from NOAA Storm Events.",
                    "how_people_deal": "Observed adaptation pattern in flood-prone communities: flood insurance uptake, elevation/mitigation projects, route planning around recurrent street flooding, and warning-driven behavior changes during heavy rain/tidal events.",
                },
                "evidence": [
                    {
                        "source": "NOAA NCEI Storm Events Bulk CSV + README",
                        "url": NOAA_DIR,
                        "note": "Event frequency/timing/damage/injury statistics are computed directly from NOAA records for 1995-2024.",
                    },
                    {
                        "source": "National Weather Service Flood Safety",
                        "url": "https://www.weather.gov/safety/flood",
                        "note": "Provides before/during/after guidance and Turn Around Don't Drown safety rule.",
                    },
                    {
                        "source": "FloodSmart (NFIP)",
                        "url": "https://www.floodsmart.gov/get-insured/buy-a-policy",
                        "note": "States most homeowners/renters insurance does not cover flood damage; NFIP building/contents coverage details.",
                    },
                    {
                        "source": "NAIC Auto Insurance Database Report (2022/2023)",
                        "url": "https://content.naic.org/sites/default/files/publication-aut-pb-auto-insurance-database.pdf",
                        "note": "Contains state-level comprehensive coverage claim metrics (frequency/loss cost), used here as the closest public payout-frequency proxy for flood-damaged vehicles.",
                    },
                ],
                "limitations": [
                    "Storm Events reports are observational and can be revised over time by NCEI.",
                    "Some records may lack coordinates; this map excludes unmappable rows.",
                    "No single open dataset provides flood-only, city-level auto insurance payout frequency; NAIC comprehensive metrics are state-level proxies.",
                ],
            },
        },
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(output, indent=2), encoding="utf-8")

    print(f"\nWrote {OUT_JSON}")
    for c in CITIES:
        s = output["stats"]["cities"][c["key"]]
        print(f"  {c['name']}: {s['eventCount']} events, avg {s['avgPerYear']}/year")
    print(f"  Regional unique events: {output['meta']['total_unique_events']}")


if __name__ == "__main__":
    main()
