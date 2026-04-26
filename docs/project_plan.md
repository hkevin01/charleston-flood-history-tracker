# Charleston Flood History Tracker Project Plan

## Phase 1 - Data Pipeline
- [x] Scaffold project structure matching tornado tracker.
- [x] Ingest NOAA Storm Events 1995-2024 flood-family records.
- [x] Filter events to Charleston metro study cities (20-mile radius each).
- [x] Compute flood risk-zone grid per city.
- [x] Emit `data/processed/charleston_floods_30y.json`.

Phase 1 Gate: PASS

## Phase 2 - Map Experience
- [x] Build map with OpenLayers + OSM.
- [x] Plot flood events with type-based colors and damage-scaled symbols.
- [x] Render risk-zone polygons with low → most-affected levels.
- [x] Add filters (year range, event type, minimum damage).

Phase 2 Gate: PASS

## Phase 3 - Decision Analysis and Delivery
- [x] Add city comparison panel for location decision support.
- [x] Add dedicated analysis section for safety, timing, insurance, and adaptation questions.
- [x] Add evidence links and explicit limitations.
- [x] Add tests, Docker deployment, and documentation.

Phase 3 Gate: PASS
