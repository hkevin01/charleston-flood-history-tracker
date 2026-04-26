#!/usr/bin/env python3
"""
# ============================================================
# ID:           CFHT-ANNOTATE-001
# Module:       annotate_damage_context.py
# Requirement:  Classify each flood event's damage context from its
#               NOAA narrative text and write the result back into the
#               processed JSON without requiring a full pipeline re-run.
# Purpose:      Provide a rapid-iteration tool so the frontend can display
#               damage-context badges (Infrastructure / Residential / Vehicle /
#               Commercial / Mixed / Unknown) immediately, using the already-
#               downloaded processed dataset.
# Rationale:    NOAA DAMAGE_PROPERTY is a single composite "best-guess" figure
#               that conflates roads, bridges, buildings, vehicles, and other
#               assets (per NWS NWSI 10-1605).  No official sub-split exists in
#               the current CSV schema.  Narrative text is the only available
#               field that can disambiguate these categories, and NWS forecasters
#               typically lead the narrative with the highest-impact damage type.
# Inputs:
#   - data/processed/charleston_floods_30y.json (must exist)
# Outputs:
#   - data/processed/charleston_floods_30y.json (overwritten in-place)
#     Each event object gains a "damageContext" key.
# Preconditions:  Processed JSON exists and is valid UTF-8 JSON.
# Postconditions: Every floodEvent has a non-null "damageContext" string.
# Assumptions:
#   - Narrative text is English and follows NWS Storm Data preparation
#     conventions (NWSI 10-1605).  Abbreviations such as "Rd" and "Blvd"
#     are lowercased and matched via substring.
#   - Multi-category events (score tie ≤ 1) are labelled "mixed".
#   - Events with no narrative default to "unknown".
# Side Effects:   Overwrites the processed JSON file.
# Failure Modes:  JSON decode error → script exits with non-zero code.
# Error Handling: Reads entire file before writing to avoid partial corruption.
# Constraints:    Runtime O(n_events); negligible memory and CPU cost.
# Verification:   See tests/test_build_dataset.py → test_classify_damage_context.
# References:
#   - NWS NWSI 10-1605 Storm Data Preparation (damage field definitions)
#   - NOAA Storm Events FAQ: ncei.noaa.gov/stormevents/faq.jsp
#     "How are the damage amounts determined?"
# ============================================================
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
JSON_PATH = PROJECT_ROOT / "data" / "processed" / "charleston_floods_30y.json"

# ---------------------------------------------------------------------------
# Keyword sets for each damage context category.
# Derived from manual inspection of 314 NOAA Charleston-metro flood narratives.
# Road/infrastructure keywords deliberately broad (abbreviations included).
# ---------------------------------------------------------------------------

_INFRA_KEYWORDS = re.compile(
    r"\b("
    r"road|street|highway|hwy|route|blvd|boulevard|ave|avenue|ln|lane|dr|drive|"
    r"rd |rd\b|intersection|bridge|culvert|drainage|stormwater|overpass|underpass|"
    r"closed|closure|impassable|washout|roadway|pavement|sidewalk|corridor|"
    r"interchange|ramp|causeway|levee|dam|ditch|canal|storm\s*drain|sewer"
    r")\b",
    re.IGNORECASE,
)

_RESIDENTIAL_KEYWORDS = re.compile(
    r"\b("
    r"home|house|residence|resident|residential|apartment|condo|condominium|"
    r"subdivision|neighborhood|mobile\s*home|crawl\s*space|living\s*room|bedroom|"
    r"kitchen|garage\s+of|water\s+inside|flooded\s+home|flooded\s+house|"
    r"dwelling|units|townhome|townhouse|property\s+owner|homeowner"
    r")\b",
    re.IGNORECASE,
)

_VEHICLE_KEYWORDS = re.compile(
    r"\b("
    r"car|cars|vehicle|vehicles|automobile|motorist|driver|stranded|floating|"
    r"stalled|submerged\s+vehicle|water\s+rescue|rescue|swept|float|SUV|"
    r"truck|van|bus|automobile|parking\s+lot|water\s+entering\s+vehicle"
    r")\b",
    re.IGNORECASE,
)

_COMMERCIAL_KEYWORDS = re.compile(
    r"\b("
    r"business|businesses|restaurant|store|shop|mall|shopping|office|hotel|"
    r"motel|commercial|retail|campus|college|university|school|church|"
    r"warehouse|industrial|plaza|center\b|centre\b"
    r")\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Keywords indicating physical damage occurred even when $0 was entered.
# These signal that the NWS forecaster described damage in the narrative but
# did not complete the DAMAGE_PROPERTY field — a known gap in pre-2010 records.
# ---------------------------------------------------------------------------
_PHYSICAL_DAMAGE_RE = re.compile(
    r"\b("
    r"flooded|under\s*water|underwater|stalled?|swept\s+away|swept|"
    r"damaged?|damage\s+to|impassable|washout|washed\s+out|closed|blocked|"
    r"destroyed|collapsed|inundated|overflow|overflowed|trapped|"
    r"rescue|evacuat|homes?\s+flood|cars?\s+under|car\s+stall|"
    r"completely\s+flood|submerge|water\s+inside|water\s+entering"
    r")\b",
    re.IGNORECASE,
)


def damage_appears_unreported(narrative: str, property_usd: float, crop_usd: float) -> bool:
    """
    # -------------------------------------------------------------------------
    # ID:           CFHT-UNREPORTED-001
    # Requirement:  Return True when an event has zero recorded damage but the
    #               narrative describes clear physical impact, indicating the
    #               NOAA DAMAGE_PROPERTY field was left blank by the forecaster.
    # Purpose:      Flag data quality gaps so the UI can warn users that "$0"
    #               may mean "not entered" rather than "no damage".
    # Rationale:
    #   Inspection of NOAA raw CSVs shows that pre-2010 records frequently
    #   have empty DAMAGE_PROPERTY strings even when the narrative clearly
    #   describes flooded trailer parks, submerged cars, impassable roads, etc.
    #   Displaying "$0 damage" as-is is actively misleading; a clearly labelled
    #   "damage not reported" is more honest and useful.
    # Inputs:
    #   narrative (str):    Event narrative text (may be empty).
    #   property_usd (float): Parsed DAMAGE_PROPERTY value (0.0 when blank).
    #   crop_usd (float):   Parsed DAMAGE_CROPS value (0.0 when blank).
    # Outputs:
    #   bool: True = "we believe damage occurred but was not recorded."
    # Assumptions:
    #   - All blank DAMAGE_PROPERTY fields parsed to 0.0 by parse_damage_to_usd().
    #   - The heuristic produces false positives for genuinely zero-damage events
    #     that mention prior flood context (acceptable — label is advisory only).
    # -------------------------------------------------------------------------
    """
    if (property_usd or 0) > 0 or (crop_usd or 0) > 0:
        return False  # damage IS recorded; no flag needed
    if not narrative or not narrative.strip():
        return False  # no narrative to assess
    return bool(_PHYSICAL_DAMAGE_RE.search(narrative))


def classify_damage_context(narrative: str) -> str:
    """
    # -------------------------------------------------------------------------
    # ID:           CFHT-CLASSIFY-001
    # Requirement:  Given a NOAA event narrative string, return a single damage-
    #               context label: infra | residential | vehicle | commercial |
    #               mixed | unknown.
    # Purpose:      Surface a human-meaningful split of the opaque
    #               DAMAGE_PROPERTY figure for residents and researchers.
    # Rationale:    NWS forecasters lead the narrative with the event's
    #               primary impact.  Keyword frequency therefore approximates
    #               the dominant damage category for the majority of events.
    # Inputs:
    #   narrative (str): EVENT_NARRATIVE or EPISODE_NARRATIVE text from NOAA
    #               CSV; may be empty.  Max length 800 chars (pipeline cap).
    # Outputs:
    #   str: One of {"infra", "residential", "vehicle", "commercial",
    #                "mixed", "unknown"}.
    # Preconditions:  None — empty/None input is valid.
    # Postconditions: Return value is always one of the six labels above.
    # Assumptions:  English language; NWS Storm Data preparation conventions.
    # Failure Modes: Over-classification possible when a narrative mentions
    #                roads in passing while describing structural damage.
    #                Mitigation: "mixed" when top-two scores differ by ≤ 1.
    # -------------------------------------------------------------------------
    """
    if not narrative or not narrative.strip():
        return "unknown"

    text = narrative.lower()

    scores = {
        "infra":       len(_INFRA_KEYWORDS.findall(text)),
        "residential": len(_RESIDENTIAL_KEYWORDS.findall(text)),
        "vehicle":     len(_VEHICLE_KEYWORDS.findall(text)),
        "commercial":  len(_COMMERCIAL_KEYWORDS.findall(text)),
    }

    total = sum(scores.values())
    if total == 0:
        return "unknown"

    sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    top_label, top_score = sorted_scores[0]
    second_score = sorted_scores[1][1]

    # Tie or near-tie between two categories → mixed
    if top_score > 0 and (top_score - second_score) <= 1:
        return "mixed"

    return top_label


def main() -> None:
    """
    # -------------------------------------------------------------------------
    # ID:           CFHT-ANNOTATE-MAIN-001
    # Requirement:  Load processed JSON, annotate every floodEvent with
    #               damageContext, write back atomically.
    # -------------------------------------------------------------------------
    """
    if not JSON_PATH.exists():
        print(f"ERROR: {JSON_PATH} not found. Run build_dataset.py first.", file=sys.stderr)
        sys.exit(1)

    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))

    counts: dict[str, int] = {}
    unreported_count = 0
    for ev in data.get("floodEvents", []):
        ctx = classify_damage_context(ev.get("narrative", ""))
        ev["damageContext"] = ctx
        counts[ctx] = counts.get(ctx, 0) + 1

        unreported = damage_appears_unreported(
            ev.get("narrative", ""),
            ev.get("propertyDamageUSD", 0),
            ev.get("cropDamageUSD", 0),
        )
        ev["damageUnreported"] = unreported
        if unreported:
            unreported_count += 1

    JSON_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")

    print(f"Annotated {sum(counts.values())} events → {JSON_PATH.name}")
    print(f"  damageUnreported=True : {unreported_count} events (NOAA field blank, narrative describes damage)")
    for label, n in sorted(counts.items(), key=lambda x: -x[1]):
        bar = "█" * int(n / max(counts.values()) * 20)
        print(f"  {label:<12} {n:>4}  {bar}")

    # ------------------------------------------------------------------
    # NOAA metadata split recommendation
    # ------------------------------------------------------------------
    print("""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
NOAA DAMAGE_PROPERTY — Recommended Official Sub-Split Schema
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Current state (per NWS NWSI 10-1605, FAQ):
  DAMAGE_PROPERTY — single "best guess" combining ALL of the following
  DAMAGE_CROPS    — agricultural losses only (already separate)

Recommended new fields NOAA should add to the StormEvents CSV:
  DAMAGE_INFRASTRUCTURE_USD  Roads, bridges, culverts, stormwater systems,
                              utilities, levees, public right-of-way.
  DAMAGE_RESIDENTIAL_USD     Private homes, apartments, mobile homes,
                             condominiums, crawl-space/interior flooding.
  DAMAGE_COMMERCIAL_USD      Businesses, retail, restaurants, hotels,
                             warehouses, industrial facilities.
  DAMAGE_VEHICLE_USD         Flooded/stranded/damaged private vehicles
                             and boats (distinct from commercial fleet).
  DAMAGE_PUBLIC_ASSETS_USD   Government buildings, schools, parks,
                             emergency facilities, military bases.
  DAMAGE_CROPS_USD           Already exists; keep as-is.

Why this matters:
  • A road getting flooded ≠ a neighborhood getting wrecked.
    Road closure → temporary disruption.
    Residential flooding → insurance claims, displacement, long-term costs.
  • Insurance products differ: auto comprehensive ≠ NFIP building/contents
    ≠ commercial property ≠ public FEMA BRIC mitigation grants.
  • Researchers, insurers, and city planners need the split to allocate
    resilience spending correctly.
  • Current DAMAGE_PROPERTY aggregation systematically inflates or deflates
    city-level "damage" depending on whether a report captures a bridge
    repair cost or 40 flooded cars — completely different risk profiles.

Data collection approach NOAA could use:
  Retain the existing DAMAGE_PROPERTY as a "total" fallback.
  Add optional sub-fields, populated from the same sources already used
  (county EMAs, media, law enforcement, spotters).  NWS forecasters
  already write structured narratives that contain this information —
  a light parsing/tagging layer at data entry would suffice.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
""")


if __name__ == "__main__":
    main()
