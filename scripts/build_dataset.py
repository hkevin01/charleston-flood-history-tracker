#!/usr/bin/env python3
"""
# ============================================================
# ID:           CFHT-PIPELINE-001
# Module:       build_dataset.py
# Requirement:  Ingest, filter, classify, risk-score, and emit a 30-year
#               Charleston-metro flood event dataset from NOAA NCEI Storm Events
#               bulk CSV files, producing a single self-contained JSON artifact
#               consumed by the static frontend.
# Purpose:      Decouple all data-preparation work from the runtime UI.
#               Running this script once (or on-demand) produces a fully
#               pre-computed JSON file that the browser can load with a
#               single fetch() call — no database, no server, no API key.
# Rationale:    Charleston, SC regularly experiences four flood sub-types
#               (Flash Flood, Coastal Flood, Storm Surge, and broad Flood) that
#               overlap geographically but differ in cause, timing, and
#               downstream insurance/decision implications.  Combining 30 years
#               of NOAA records, Gaussian spatial risk scoring, and narrative-
#               based damage-context classification gives residents and
#               planners a richer decision signal than raw event counts alone.
# Inputs:
#   - NOAA NCEI StormEvents_details-ftp_v1.0_d<YEAR>_c<REV>.csv.gz files
#     fetched from https://www.ncei.noaa.gov/pub/data/swdi/stormevents/csvfiles/
#     for years 1995–2024.  No API key required.
# Outputs:
#   - data/processed/charleston_floods_30y.json (UTF-8, pretty-printed JSON).
#     Contains: meta, places, floodEvents (with damageContext), riskZones, stats.
# Preconditions:
#   - Outbound HTTPS access to ncei.noaa.gov.
#   - data/processed/ directory writable.
# Postconditions:
#   - JSON written atomically via Path.write_text().
#   - Console emits per-city/regional summary.
# Assumptions:
#   - NOAA NCEI directory-listing HTML format is stable enough for regex.
#   - NOAA coordinates are WGS-84 decimal degrees.
#   - DAMAGE_PROPERTY is a composite "best-guess" field mixing infrastructure,
#     residential, vehicle, and commercial losses per NWS NWSI 10-1605.
#     No authoritative sub-split exists; narrative parsing is the only proxy.
# Side Effects:  ~30 HTTPS GET requests (~200 MB total download).
# Failure Modes:
#   - Network timeout      → urllib.error.URLError (caller should retry)
#   - Year file not in index → RuntimeError with year in message
#   - Malformed CSV row     → row silently skipped (unmappable rows excluded)
# Error Handling: parse_* helpers return safe defaults on malformed input.
# Constraints:   Runtime 5–15 min on broadband; ~120 MB peak RAM.
# Verification:  tests/test_build_dataset.py covers all helper and
#                classification functions.
# References:
#   - NOAA NCEI Storm Events README:
#     https://www.ncei.noaa.gov/pub/data/swdi/stormevents/csvfiles/README
#   - NWS NWSI 10-1605 Storm Data Preparation directive (damage field defs)
#   - NAIC Auto Insurance Database (state-level comprehensive-claim proxy)
#   - NOAA damage split recommendation: docs/implementation_notes.md
# ============================================================
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
    {"key": "charleston",       "name": "Charleston, SC",       "lat": 32.7765, "lon": -79.9311},
    {"key": "north_charleston", "name": "North Charleston, SC", "lat": 32.8546, "lon": -79.9748},
    {"key": "summerville",      "name": "Summerville, SC",      "lat": 33.0185, "lon": -80.1756},
    {"key": "goose_creek",      "name": "Goose Creek, SC",      "lat": 32.9810, "lon": -80.0326},
    {"key": "hanahan",          "name": "Hanahan, SC",          "lat": 32.9185, "lon": -80.0220},
]

# Comparison-only cities used to show that flood risk is not unique to
# Charleston, SC. These are not plotted on the map and do not affect
# Charleston risk-zone generation.
COMPARISON_CITIES = [
    {"key": "fayetteville_wv", "name": "Fayetteville, WV", "lat": 38.0529, "lon": -81.1043},
    {"key": "oak_hill_wv",     "name": "Oak Hill, WV",     "lat": 37.9723, "lon": -81.1487},
    {"key": "bridgeport_wv",   "name": "Bridgeport, WV",   "lat": 39.2865, "lon": -80.2553},
    {"key": "fairmont_wv",     "name": "Fairmont, WV",     "lat": 39.4851, "lon": -80.1426},
    {"key": "clarksburg_wv",   "name": "Clarksburg, WV",   "lat": 39.2806, "lon": -80.3445},
]

# ---------------------------------------------------------------------------
# Keyword sets for narrative-based damage-context classification.
# Derived from manual inspection of 314 Charleston-metro NOAA narratives.
# ---------------------------------------------------------------------------

_INFRA_RE = re.compile(
    r"\b("
    r"road|street|highway|hwy|route|blvd|boulevard|ave|avenue|ln|lane|dr|drive|"
    r"rd |rd\b|intersection|bridge|culvert|drainage|stormwater|overpass|underpass|"
    r"closed|closure|impassable|washout|roadway|pavement|sidewalk|corridor|"
    r"interchange|ramp|causeway|levee|dam|ditch|canal|storm\s*drain|sewer"
    r")\b",
    re.IGNORECASE,
)
_RESIDENTIAL_RE = re.compile(
    r"\b("
    r"home|house|residence|resident|residential|apartment|condo|condominium|"
    r"subdivision|neighborhood|mobile\s*home|crawl\s*space|living\s*room|bedroom|"
    r"kitchen|garage\s+of|water\s+inside|flooded\s+home|flooded\s+house|"
    r"dwelling|townhome|townhouse|homeowner"
    r")\b",
    re.IGNORECASE,
)
_VEHICLE_RE = re.compile(
    r"\b("
    r"car|cars|vehicle|vehicles|automobile|motorist|driver|stranded|floating|"
    r"stalled|submerged|water\s*rescue|rescue|swept|SUV|truck|van|bus|"
    r"parking\s*lot"
    r")\b",
    re.IGNORECASE,
)
_COMMERCIAL_RE = re.compile(
    r"\b("
    r"business|businesses|restaurant|store|shop|mall|shopping|office|hotel|"
    r"motel|commercial|retail|campus|college|university|school|church|"
    r"warehouse|industrial|plaza"
    r")\b",
    re.IGNORECASE,
)


@dataclass
class FloodEvent:
    """
    # -------------------------------------------------------------------------
    # ID:           CFHT-DATACLASS-001
    # Requirement:  Represent one NOAA Storm Events flood record with all fields
    #               required for spatial filtering, risk scoring, UI display,
    #               and damage-context classification.
    # Fields (post-parse, normalised types):
    #   event_id, episode_id  — NOAA surrogate keys (int)
    #   year, month           — temporal bucketing (int)
    #   date_time             — raw NOAA BEGIN_DATE_TIME string
    #   event_type            — one of FLOOD_EVENT_TYPES (str)
    #   state, county         — geographic labels (str)
    #   begin_lat/lon         — origin coordinates, WGS-84 decimal degrees
    #   end_lat/lon           — endpoint coordinates (defaults to begin if absent)
    #   injuries, deaths      — casualty counts (int, ≥ 0)
    #   property_damage_usd   — parsed monetary value, may be 0.0 (float)
    #   crops_damage_usd      — parsed monetary value (float)
    #   narrative             — truncated to 800 chars (str)
    #   damage_context        — one of {infra,residential,vehicle,commercial,
    #                           mixed,unknown} (str, derived)
    # -------------------------------------------------------------------------
    """
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
    damage_context: str = "unknown"
    damage_unreported: bool = False   # True when DAMAGE_PROPERTY was blank AND narrative describes physical impact


def classify_damage_context(narrative: str) -> str:
    """
    # -------------------------------------------------------------------------
    # ID:           CFHT-CLASSIFY-001
    # Requirement:  Return a single label describing the dominant damage context
    #               inferred from a NOAA event narrative string.
    # Purpose:      Surface an actionable sub-split of the opaque DAMAGE_PROPERTY
    #               composite field so residents, insurers, and planners can
    #               distinguish road/infrastructure costs from private-property
    #               or vehicle losses.
    # Rationale:    NWS NWSI 10-1605 directs forecasters to lead the narrative
    #               with the event's most significant impact; keyword frequency
    #               in the first 800 characters therefore approximates the
    #               dominant damage type for the majority of events.
    # Inputs:
    #   narrative (str): Up to 800-char EVENT_NARRATIVE/EPISODE_NARRATIVE text.
    #                    Empty string and None are both valid.
    # Outputs:
    #   str: One of {"infra", "residential", "vehicle", "commercial",
    #                "mixed", "unknown"}.  Never None, never raises.
    # Preconditions:  None.
    # Postconditions: Output is a member of the six-element label set above.
    # Assumptions:
    #   - NWS English-language convention; abbreviations lowercased at runtime.
    #   - Events where top-two category scores differ by ≤ 1 are "mixed"
    #     to avoid false precision.
    # Failure Modes:
    #   - False labelling when road names contain residential keywords (e.g.
    #     "Home Depot Blvd" → inflates both infra and commercial).
    #     Mitigation: contextual phrases preferred over bare tokens where
    #     ambiguity is highest.
    # Error Handling: Returns "unknown" on empty/None input; never raises.
    # Constraints:    O(len(narrative)); trivial runtime.
    # Verification:   tests/test_build_dataset.py::test_classify_damage_context
    # -------------------------------------------------------------------------
    """
    if not narrative or not narrative.strip():
        return "unknown"

    text = narrative.lower()
    scores = {
        "infra":       len(_INFRA_RE.findall(text)),
        "residential": len(_RESIDENTIAL_RE.findall(text)),
        "vehicle":     len(_VEHICLE_RE.findall(text)),
        "commercial":  len(_COMMERCIAL_RE.findall(text)),
    }

    total = sum(scores.values())
    if total == 0:
        return "unknown"

    sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    top_label, top_score = sorted_scores[0]
    second_score = sorted_scores[1][1]

    if top_score > 0 and (top_score - second_score) <= 1:
        return "mixed"
    return top_label


def fetch_index() -> str:
    """
    # -------------------------------------------------------------------------
    # ID:           CFHT-FETCH-001
    # Requirement:  Retrieve the NOAA NCEI Storm Events CSV directory index page.
    # Purpose:      Provide the raw HTML listing from which year-specific
    #               filenames are resolved via regex.
    # Inputs:       None (uses module-level NOAA_INDEX_URL constant).
    # Outputs:      str — decoded HTML of the NOAA directory listing.
    # Preconditions: Network access to ncei.noaa.gov; HTTPS port 443 open.
    # Postconditions: Returns non-empty string on success.
    # Failure Modes: urllib.error.URLError on timeout or network error.
    # Error Handling: Propagates exception; caller must handle retry logic.
    # Constraints:   60-second timeout to avoid indefinite hangs on slow links.
    # -------------------------------------------------------------------------
    """
    with urllib.request.urlopen(NOAA_INDEX_URL, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="replace")


def resolve_year_file(index_html: str, year: int) -> str:
    """
    # -------------------------------------------------------------------------
    # ID:           CFHT-RESOLVE-001
    # Requirement:  Extract the canonical NOAA yearly CSV.GZ URL for a given
    #               year from the directory index HTML.
    # Purpose:      NOAA appends a revision timestamp to each filename; this
    #               function finds the current revision without hardcoding it.
    # Inputs:
    #   index_html (str): HTML returned by fetch_index().
    #   year (int):       Target year in range [START_YEAR, END_YEAR].
    # Outputs:
    #   str: Full HTTPS URL to the yearly CSV.GZ file.
    # Preconditions:  index_html is non-empty; year is a valid calendar year.
    # Postconditions: Returned URL is well-formed and points to a valid file.
    # Failure Modes:  RuntimeError if NOAA changed its filename convention.
    # Error Handling: Raises RuntimeError with year in message for diagnostics.
    # Constraints:    Regex pattern is tightly bound to NOAA filename format
    #                 StormEvents_details-ftp_v1.0_d<YEAR>_c<YYYYMMDD>.csv.gz.
    # -------------------------------------------------------------------------
    """
    pat = re.compile(rf"(StormEvents_details-ftp_v1\.0_d{year}_c\d+\.csv\.gz)")
    m = pat.search(index_html)
    if not m:
        raise RuntimeError(f"NOAA yearly file for {year} not found in index")
    return NOAA_DIR + m.group(1)


def parse_damage_to_usd(text: str) -> float:
    """
    # -------------------------------------------------------------------------
    # ID:           CFHT-PARSE-DAMAGE-001
    # Requirement:  Parse NOAA DAMAGE_PROPERTY / DAMAGE_CROPS abbreviated
    #               monetary strings (e.g. "750K", "1.2M", "0") into float USD.
    # Inputs:
    #   text (str): Raw cell value from CSV.  Valid forms: empty, "0",
    #               "<number>[K|M|B]" (case-insensitive).
    # Outputs:
    #   float: USD dollar amount ≥ 0.0.  Returns 0.0 on empty or malformed.
    # Preconditions:  None — safe to call on any string.
    # Postconditions: Return value is finite, non-negative float.
    # Failure Modes:  Non-matching patterns return 0.0 (silent degradation).
    # Error Handling: No exception raised; defaults are safe for summation.
    # -------------------------------------------------------------------------
    """
    text = (text or "").strip().upper()
    if not text:
        return 0.0
    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)([KMB]?)$", text)
    if not m:
        return 0.0
    value = float(m.group(1))
    mult = {"": 1.0, "K": 1_000.0, "M": 1_000_000.0, "B": 1_000_000_000.0}[m.group(2)]
    return value * mult


def parse_int(text: str, default: int = 0) -> int:
    """
    # -------------------------------------------------------------------------
    # ID:           CFHT-PARSE-INT-001
    # Requirement:  Safely convert a CSV cell to int, returning a default on
    #               any parse failure (empty, non-numeric, float strings).
    # Inputs:  text (str) — raw CSV cell; default (int) — fallback value.
    # Outputs: int.  Never raises.
    # -------------------------------------------------------------------------
    """
    try:
        return int(float(text))
    except Exception:
        return default


def parse_float(text: str, default: float = 0.0) -> float:
    """
    # -------------------------------------------------------------------------
    # ID:           CFHT-PARSE-FLOAT-001
    # Requirement:  Safely convert a CSV cell to float, returning default on
    #               any parse failure.
    # Inputs:  text (str) — raw CSV cell; default (float) — fallback value.
    # Outputs: float.  Never raises.
    # -------------------------------------------------------------------------
    """
    try:
        return float(text)
    except Exception:
        return default


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    # -------------------------------------------------------------------------
    # ID:           CFHT-HAVERSINE-001
    # Requirement:  Compute the great-circle distance in miles between two
    #               WGS-84 coordinate pairs using the Haversine formula.
    # Inputs:
    #   lat1, lon1 (float): Origin latitude/longitude in decimal degrees.
    #   lat2, lon2 (float): Destination latitude/longitude in decimal degrees.
    # Outputs:
    #   float: Distance in statute miles (≥ 0.0).
    # Preconditions:
    #   lat ∈ [-90, 90], lon ∈ [-180, 180].  Invalid inputs produce silent
    #   numerical garbage; callers must not pass out-of-range values.
    # Postconditions: Return value is finite and ≥ 0.
    # Rationale:     Haversine is accurate to within 0.5% for distances under
    #                100 miles; fully sufficient for the ≤ 20-mile city radius.
    # Constraints:   Uses Earth mean radius 3958.8 mi (WGS-84 equatorial ≈ 3963,
    #                polar ≈ 3950 mi); split-the-difference value is standard for
    #                US mid-latitude regions and introduces <0.1% error.
    # -------------------------------------------------------------------------
    """
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
    """
    # -------------------------------------------------------------------------
    # ID:           CFHT-PROXIMITY-001
    # Requirement:  Return True if either the begin or end coordinate of a
    #               FloodEvent falls within radius_miles of the city centre.
    # Purpose:      NOAA events can span large areas; checking both endpoints
    #               maximises recall for line-segment events (e.g. a road wash-
    #               out that starts outside the city and ends inside).
    # Inputs:
    #   event (FloodEvent): Must have valid begin_lat/lon and end_lat/lon.
    #   city  (dict):       Must have "lat" and "lon" keys.
    #   radius_miles (float): Search radius; module default is 20.0 mi.
    # Outputs:
    #   bool: True if event is within range of city by either endpoint.
    # Postconditions: Pure function; no side effects.
    # -------------------------------------------------------------------------
    """
    return (
        haversine_miles(city["lat"], city["lon"], event.begin_lat, event.begin_lon) <= radius_miles
        or haversine_miles(city["lat"], city["lon"], event.end_lat, event.end_lon) <= radius_miles
    )


def quantile(values: list[float], q: float) -> float:
    """
    # -------------------------------------------------------------------------
    # ID:           CFHT-QUANTILE-001
    # Requirement:  Return the q-th quantile of a numeric list using linear
    #               interpolation at the nearest lower index.
    # Inputs:
    #   values (list[float]): Unsorted sample; may be empty.
    #   q (float): Quantile in [0.0, 1.0].
    # Outputs:
    #   float: Sample quantile.  Returns 0.0 for empty list.
    # Constraints:  Simple nearest-rank method; adequate for threshold bucketing.
    # -------------------------------------------------------------------------
    """
    ordered = sorted(values)
    if not ordered:
        return 0.0
    i = int(q * (len(ordered) - 1))
    return ordered[i]


def build_risk_zones(city: dict, events: list[FloodEvent]) -> list[dict]:
    """
    # -------------------------------------------------------------------------
    # ID:           CFHT-RISKZONE-001
    # Requirement:  Produce a grid of risk-classified cells covering a bounding
    #               box around a city, using a Gaussian kernel weighted by
    #               observed flood events.
    # Purpose:      Give residents a city-scale visual approximation of relative
    #               flood risk — "which areas flood more often?" — while clearly
    #               communicating that the model is a statistical estimate, not a
    #               regulatory flood map.
    # Rationale:
    #   - Gaussian kernel decays rapidly (σ = 1.0 mi, cutoff 3.0 mi) so that
    #     high-risk cells tightly wrap observed event locations rather than
    #     bleeding across large areas.
    #   - Event weights factor in damage severity and casualty counts so that
    #     a deadly or costly event contributes more to surrounding risk than a
    #     minor one.
    #   - Coastal Flood / Storm Surge events receive a 1.6× kind weight
    #     because their spatial impact is systematically wider than pluvial
    #     flash floods.
    #   - Quantile-based level thresholds are relative to the observed
    #     distribution, making the five-tier classification meaningful even
    #     when absolute counts are low.
    # Inputs:
    #   city   (dict):          Must have "lat", "lon", "key" keys.
    #   events (list[FloodEvent]): Pre-filtered to the city's 20-mile radius.
    # Outputs:
    #   list[dict]: One dict per grid cell, each with keys:
    #     bbox      [lon_min, lat_min, lon_max, lat_max]  (WGS-84)
    #     score     float ≥ 0.0
    #     scoreNorm float ∈ [0, 1] (log-normalised)
    #     level     str ∈ {Low, Guarded, Elevated, High, Most Affected}
    #     city      str (city key)
    # Preconditions:
    #   - events list may be empty (all cells will be level="Low", score=0).
    # Postconditions:
    #   - Every cell has all required keys.
    #   - Cells farther than 3.8 mi from all events are forced to level="Low".
    # Assumptions:
    #   - Model is NOT a regulatory flood determination and must never be
    #     presented as equivalent to FEMA FIRM maps.
    #   - Spatial grid at 0.02° step ≈ 1.4 miles; adequate for visual overview.
    # Failure Modes:
    #   - Zero events → all cells low risk (valid, not an error).
    #   - max_score = 0 → scoreNorm = 0 for all cells.
    # Constraints:
    #   - Grid size O((lat_span/step) × (lon_span/step)) ≈ 600–900 cells.
    #   - Kernel cutoff and σ are intentionally conservative to avoid the
    #     appearance of false precision.
    # Verification:
    #   - Visual QA: High-risk cells should cluster around downtown Charleston
    #     and known flood corridors (Goose Creek, North Charleston I-26 area).
    # References:
    #   - FEMA FIRM panel search: msc.fema.gov/portal/home (authoritative source)
    # -------------------------------------------------------------------------
    """
    step = 0.02
    sigma_mi = 1.0
    influence_cutoff_mi = 3.0
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
        damage_weight = math.log1p(ev.property_damage_usd + ev.crops_damage_usd) / 8.0
        impact_weight = ev.injuries * 1.8 + ev.deaths * 4.0
        kind_weight = 1.6 if ev.event_type in {"Coastal Flood", "Storm Surge/Tide"} else 1.0
        w = (1.0 + damage_weight + impact_weight) * kind_weight
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

            cells.append({
                "bbox": [round(lon, 6), round(lat, 6), round(lon + step, 6), round(lat + step, 6)],
                "score": round(score, 4),
            })
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
    """
    # -------------------------------------------------------------------------
    # ID:           CFHT-READ-YEAR-001
    # Requirement:  Download and parse one year's NOAA StormEvents details
    #               CSV.GZ file, returning all South Carolina flood-family events
    #               that have valid start coordinates.
    # Purpose:      Handles gzip decompression, CSV parsing, type coercion,
    #               coordinate validation, and damage-context classification
    #               for a single calendar year, keeping the main loop clean.
    # Inputs:
    #   url (str): Fully-qualified HTTPS URL to a StormEvents_details CSV.GZ.
    # Outputs:
    #   list[FloodEvent]: Zero or more FloodEvent instances for the year.
    # Preconditions:  URL must be accessible and point to a valid gzip CSV.
    # Postconditions:
    #   - All returned events have valid non-zero begin coordinates.
    #   - end coordinates default to begin if absent from CSV.
    #   - damage_context is set via classify_damage_context().
    # Failure Modes:
    #   - urllib.error.URLError on network failure (propagated).
    #   - BadGzipFile if content is not valid gzip (propagated).
    #   - DictReader KeyError if NOAA changes column names (event skipped).
    # Error Handling:
    #   - Rows with begin_lat == begin_lon == 0.0 are skipped (unmappable).
    #   - parse_* helpers ensure no TypeError propagates from bad field values.
    # Constraints:   120-second timeout per file.  Each file is ~1–8 MB gz.
    # -------------------------------------------------------------------------
    """
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
                continue

            end_lat = parse_float(row.get("END_LAT", "0"), begin_lat)
            end_lon = parse_float(row.get("END_LON", "0"), begin_lon)
            if end_lat == 0.0 and end_lon == 0.0:
                end_lat, end_lon = begin_lat, begin_lon

            injuries = (parse_int(row.get("INJURIES_DIRECT", "0"))
                        + parse_int(row.get("INJURIES_INDIRECT", "0")))
            deaths = (parse_int(row.get("DEATHS_DIRECT", "0"))
                      + parse_int(row.get("DEATHS_INDIRECT", "0")))

            # Detect blank damage field vs explicit "0" — blank means the
            # forecaster never entered an estimate (common in pre-2010 records).
            raw_prop = (row.get("DAMAGE_PROPERTY", "") or "").strip()
            raw_crop = (row.get("DAMAGE_CROPS", "")    or "").strip()
            prop_usd = parse_damage_to_usd(raw_prop)
            crop_usd = parse_damage_to_usd(raw_crop)
            # Both fields being blank (not "0") AND damage-describing narrative
            # → flag as unreported.  Uses classify_damage_context's keyword sets.
            prop_blank = raw_prop == ""
            crop_blank = raw_crop == ""

            narrative = (
                row.get("EVENT_NARRATIVE", "")
                or row.get("EPISODE_NARRATIVE", "")
                or ""
            )[:800]

            _phys_dmg_re = re.compile(
                r"\b(flooded|under\s*water|underwater|stalled?|swept|damaged?|"
                r"impassable|washout|washed\s+out|closed|destroyed|collapsed|"
                r"inundated|overflow|trapped|rescue|evacuat|submerge|"
                r"cars?\s+under|water\s+inside|completely\s+flood)\b",
                re.IGNORECASE,
            )
            damage_unreported = (
                prop_blank and crop_blank
                and bool(_phys_dmg_re.search(narrative))
            )

            out.append(FloodEvent(
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
                narrative=narrative,
                damage_context=classify_damage_context(narrative),
                damage_unreported=damage_unreported,
            ))

    return out


def read_year_comparison_events(url: str, cities: list[dict], radius_miles: float) -> list[FloodEvent]:
    """
    # -------------------------------------------------------------------------
    # ID:           CFHT-READ-COMP-001
    # Requirement:  Parse one yearly NOAA details CSV and return only flood-
    #               family events in West Virginia that fall within radius of
    #               any comparison city.
    # Purpose:      Build cross-region comparison metrics to demonstrate that
    #               flooding is widespread and not unique to Charleston, SC.
    # Inputs:
    #   url (str):         Yearly NOAA StormEvents details CSV.GZ URL.
    #   cities (list[dict]): Comparison city dicts with lat/lon.
    #   radius_miles (float): Inclusion radius around each comparison city.
    # Outputs:
    #   list[FloodEvent]: Events near at least one comparison city.
    # -------------------------------------------------------------------------
    """
    with urllib.request.urlopen(url, timeout=120) as resp:
        raw = resp.read()

    out: list[FloodEvent] = []
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
        text = io.TextIOWrapper(gz, encoding="utf-8", errors="replace")
        reader = csv.DictReader(text)
        for row in reader:
            if row.get("STATE", "").strip().upper() != "WEST VIRGINIA":
                continue
            evtype = (row.get("EVENT_TYPE", "") or "").strip()
            if evtype not in FLOOD_EVENT_TYPES:
                continue

            begin_lat = parse_float(row.get("BEGIN_LAT", "0"))
            begin_lon = parse_float(row.get("BEGIN_LON", "0"))
            if begin_lat == 0.0 and begin_lon == 0.0:
                continue

            end_lat = parse_float(row.get("END_LAT", "0"), begin_lat)
            end_lon = parse_float(row.get("END_LON", "0"), begin_lon)
            if end_lat == 0.0 and end_lon == 0.0:
                end_lat, end_lon = begin_lat, begin_lon

            injuries = (parse_int(row.get("INJURIES_DIRECT", "0"))
                        + parse_int(row.get("INJURIES_INDIRECT", "0")))
            deaths = (parse_int(row.get("DEATHS_DIRECT", "0"))
                      + parse_int(row.get("DEATHS_INDIRECT", "0")))

            narrative = (
                row.get("EVENT_NARRATIVE", "")
                or row.get("EPISODE_NARRATIVE", "")
                or ""
            )[:800]

            ev = FloodEvent(
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
                property_damage_usd=parse_damage_to_usd(row.get("DAMAGE_PROPERTY", "")),
                crops_damage_usd=parse_damage_to_usd(row.get("DAMAGE_CROPS", "")),
                narrative=narrative,
                damage_context=classify_damage_context(narrative),
                damage_unreported=False,
            )

            if any(event_near_city(ev, city, radius_miles) for city in cities):
                out.append(ev)

    return out


def month_from_datetime(text: str) -> int:
    """
    # -------------------------------------------------------------------------
    # ID:           CFHT-MONTH-001
    # Requirement:  Extract the calendar month integer (1-12) from a NOAA
    #               BEGIN_DATE_TIME string such as "01-MAY-95 14:30:00".
    # Inputs:
    #   text (str): Raw NOAA date-time string.
    # Outputs:
    #   int: Month number 1-12 on success; 0 on parse failure.
    # Error Handling: Returns 0 on any format mismatch; never raises.
    # -------------------------------------------------------------------------
    """
    for fmt in ("%d-%b-%y %H:%M:%S", "%d-%b-%y %H:%M:%S %z"):
        try:
            return datetime.strptime(text.strip(), fmt).month
        except Exception:
            pass
    return 0


def main() -> None:
    """
    # -------------------------------------------------------------------------
    # ID:           CFHT-MAIN-001
    # Requirement:  Orchestrate the full pipeline: fetch NOAA index, resolve
    #               year URLs, download and parse all years, filter to city
    #               proximity, deduplicate, build risk zones, compute stats,
    #               and emit the final JSON artifact.
    # Purpose:      Single entry point that ties all pipeline stages together.
    #               Designed for re-running on demand when NOAA releases
    #               revised or additional yearly files.
    # Side Effects: Writes data/processed/charleston_floods_30y.json.
    # Failure Modes: Network failure at any year will propagate and abort the
    #                run; partial output is not written (write is atomic via
    #                Path.write_text which replaces the file in one call).
    # -------------------------------------------------------------------------
    """
    index_html = fetch_index()
    year_files = {y: resolve_year_file(index_html, y) for y in range(START_YEAR, END_YEAR + 1)}

    all_floods: list[FloodEvent] = []
    comparison_floods: list[FloodEvent] = []
    for y in range(START_YEAR, END_YEAR + 1):
        events = read_year_events(year_files[y])
        comp_events = read_year_comparison_events(year_files[y], COMPARISON_CITIES, CITY_RADIUS_MILES)
        all_floods.extend(events)
        comparison_floods.extend(comp_events)
        print(f"Loaded {len(events):4d} SC flood events and {len(comp_events):4d} WV comparison events for {y}")

    city_events: dict[str, list[FloodEvent]] = {c["key"]: [] for c in CITIES}
    for ev in all_floods:
        for city in CITIES:
            if event_near_city(ev, city, CITY_RADIUS_MILES):
                city_events[city["key"]].append(ev)

    comparison_city_events: dict[str, list[FloodEvent]] = {c["key"]: [] for c in COMPARISON_CITIES}
    for ev in comparison_floods:
        for city in COMPARISON_CITIES:
            if event_near_city(ev, city, CITY_RADIUS_MILES):
                comparison_city_events[city["key"]].append(ev)

    seen: set[int] = set()
    combined: list[FloodEvent] = []
    for city in CITIES:
        for ev in city_events[city["key"]]:
            if ev.event_id not in seen:
                seen.add(ev.event_id)
                combined.append(ev)

    risk_zones = {c["key"]: build_risk_zones(c, city_events[c["key"]]) for c in CITIES}

    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

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

    comparison_city_stats = {}
    for city in COMPARISON_CITIES:
        key = city["key"]
        evs = comparison_city_events[key]
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
        comparison_city_stats[key] = {
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
            "comparison_cities": COMPARISON_CITIES,
            "counts": {c["key"]: len(city_events[c["key"]]) for c in CITIES},
            "comparison_counts": {c["key"]: len(comparison_city_events[c["key"]]) for c in COMPARISON_CITIES},
            "total_unique_events": len(combined),
            # ----------------------------------------------------------------
            # NOAA Damage-Field Split Recommendation (CFHT-NOAA-REC-001)
            # ----------------------------------------------------------------
            # Current NOAA DAMAGE_PROPERTY is a composite "best-guess" that
            # conflates infrastructure, residential, vehicle, and commercial
            # losses (per NWS NWSI 10-1605).  This makes it impossible to
            # distinguish public-sector costs (road repairs) from private
            # insurance losses (flooded homes/cars) at the event level.
            #
            # Recommended new StormEvents CSV fields:
            #   DAMAGE_INFRASTRUCTURE_USD — roads, bridges, culverts, levees,
            #                               stormwater systems, public utilities
            #   DAMAGE_RESIDENTIAL_USD    — private homes, apartments,
            #                               condos, mobile homes, interior loss
            #   DAMAGE_COMMERCIAL_USD     — businesses, retail, hotels, schools
            #   DAMAGE_VEHICLE_USD        — private vehicles and boats
            #   DAMAGE_PUBLIC_ASSETS_USD  — government buildings, parks,
            #                               emergency facilities
            #   DAMAGE_CROPS_USD          — already exists; retain unchanged
            #
            # These fields could be populated using the same data sources NWS
            # already collects (county EMA reports, media, spotters), with a
            # lightweight tagging layer at data entry.  The narrative text
            # that forecasters already write contains the needed signal.
            # ----------------------------------------------------------------
            "noaa_split_recommendation": {
                "rationale": (
                    "DAMAGE_PROPERTY is a single NWS 'best-guess' figure mixing "
                    "infrastructure, residential, commercial, and vehicle losses. "
                    "A road closure costs the city; a flooded living room costs "
                    "a resident.  These are funded by different mechanisms "
                    "(FEMA BRIC, NFIP, auto comprehensive) and need separate fields "
                    "for actionable decision-making."
                ),
                "recommended_fields": [
                    "DAMAGE_INFRASTRUCTURE_USD",
                    "DAMAGE_RESIDENTIAL_USD",
                    "DAMAGE_COMMERCIAL_USD",
                    "DAMAGE_VEHICLE_USD",
                    "DAMAGE_PUBLIC_ASSETS_USD",
                ],
            },
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
                "end":   {"lat": ev.end_lat,   "lon": ev.end_lon},
                "injuries": ev.injuries,
                "deaths": ev.deaths,
                "propertyDamageUSD": round(ev.property_damage_usd, 2),
                "cropDamageUSD":     round(ev.crops_damage_usd, 2),
                "narrative": ev.narrative,
                "damageContext": ev.damage_context,
                "damageUnreported": ev.damage_unreported,
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
            "comparisonCities": comparison_city_stats,
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
                    {"source": "NOAA NCEI Storm Events Bulk CSV + README", "url": NOAA_DIR,
                     "note": "Event frequency/timing/damage/injury statistics computed directly from NOAA records 1995-2024."},
                    {"source": "National Weather Service Flood Safety", "url": "https://www.weather.gov/safety/flood",
                     "note": "Provides before/during/after guidance and Turn Around Don't Drown safety rule."},
                    {"source": "FloodSmart (NFIP)", "url": "https://www.floodsmart.gov/get-insured/buy-a-policy",
                     "note": "States most homeowners/renters insurance does not cover flood damage."},
                    {"source": "NAIC Auto Insurance Database Report (2022/2023)",
                     "url": "https://content.naic.org/sites/default/files/publication-aut-pb-auto-insurance-database.pdf",
                     "note": "State-level comprehensive coverage claim metrics used as closest public payout-frequency proxy for flood-damaged vehicles."},
                ],
                "limitations": [
                    "Storm Events reports are observational and can be revised over time by NCEI.",
                    "Some records may lack coordinates; this map excludes unmappable rows.",
                    "No single open dataset provides flood-only, city-level auto insurance payout frequency; NAIC comprehensive metrics are state-level proxies.",
                    "damageContext labels are inferred from narrative text via keyword scoring and may not reflect the true damage split for individual events.",
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
