# Contributing

Thanks for your interest in the Charleston Flood History Tracker!

## Getting started

1. Fork the repository and clone your fork.
2. Install Python dependencies (requires Python ≥ 3.10):
   ```bash
   pip install -r requirements.txt   # if present
   ```
3. Run the dataset build script (downloads raw NOAA CSVs into `data/raw/`):
   ```bash
   python scripts/build_dataset.py
   ```
4. Open `index.html` directly in a browser or serve it locally:
   ```bash
   python -m http.server 8080
   ```

## Project layout

| Path | Purpose |
|------|---------|
| `index.html` / `src/` | Front-end map application |
| `scripts/build_dataset.py` | Data pipeline — NOAA → processed JSON |
| `data/processed/` | Versioned output — committed to the repo |
| `data/raw/` | Downloaded source files — **gitignored**, never commit |
| `tests/` | Pytest test suite for the data pipeline |
| `docker/` | Optional containerised deployment |

## Submitting changes

1. Create a feature branch: `git checkout -b feat/my-change`
2. Make your changes and run the tests: `pytest tests/`
3. Open a pull request against `main` using the PR template.

## Data contributions

If you have a source that contradicts or supplements the current dataset, open a **Data issue** using the issue template and include a link to the authoritative source (NOAA NCEI, NWS, etc.).

## Code style

- Python: follow PEP 8; `black` formatter is welcome.
- JavaScript: match the existing style in `src/app.js`.

## Code of conduct

Please read and follow our [Code of Conduct](CODE_OF_CONDUCT.md).
