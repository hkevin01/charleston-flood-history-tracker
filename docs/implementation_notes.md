# Implementation Notes

## Data Source

- NOAA NCEI Storm Events bulk details CSV by year.
- Filtering scope: South Carolina records with `EVENT_TYPE` in flood-family set.
- Year window: 1995-2024 inclusive.

## Spatial Logic

- Five city centers with 20-mile radius each.
- Event included for a city when start OR end coordinate falls inside radius.
- Regional event list is deduplicated by NOAA `EVENT_ID`.

## Risk Zones

- Per-city Gaussian surface over regular grid.
- Event weight combines:
  - log-scaled reported damage,
  - injury/fatality impact,
  - event-type emphasis for coastal/surge categories.
- Classified into Low / Guarded / Elevated / High / Most Affected via quantile thresholds.

## Decision Analysis

The UI exposes:
- flood frequency by city,
- seasonal timing,
- safety guidance references,
- insurance guidance references,
- explicit limitations where city-level public insurance payout stats are unavailable.

## Cross-Region Benchmark Context

- Added comparison-only benchmark cities in West Virginia: Fayetteville, Oak Hill, Bridgeport, Fairmont, Clarksburg.
- Pipeline now performs a second pass for WV flood-family events within the same 20-mile radius logic.
- These benchmarks are rendered in a separate comparison table and do not alter Charleston map layers or Charleston risk zones.
