# Changelog

## 0.1.0 - 2026-04-26

- Created new Charleston Flood History Tracker project using the tornado tracker structure.
- Added `scripts/build_dataset.py` to build 30-year flood dataset from NOAA Storm Events (1995-2024).
- Added `index.html`, `src/app.js`, and `src/styles.css` for OpenLayers map UI.
- Added flood risk zones, event markers, filtering controls, and city comparison table.
- Added dedicated location decision analysis section answering user flood-risk questions with evidence references.
- Added `tests/test_build_dataset.py` for helper coverage (distance, parsing, proximity, risk-zone generation).
- Added Docker files for static deployment on port 8090.
