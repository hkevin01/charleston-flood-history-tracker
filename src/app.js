/**
 * ============================================================
 * ID:           CFHT-APP-001
 * Module:       app.js
 * Requirement:  Render an interactive OpenLayers map of 30-year Charleston-metro
 *               flood history with filterable event markers, Gaussian risk zones,
 *               FEMA flood-zone overlay, storm-track overlay, trend analysis
 *               sidebar, damage-context badges, city comparison table, and PDF
 *               export — all from a single pre-built static JSON dataset.
 * Purpose:      Zero-server, zero-dependency (beyond OL CDN) frontend so the
 *               tool deploys anywhere static files are served.
 * Rationale:    Keeping all computation in build_dataset.py and all display
 *               logic here creates a clean data/UI boundary.  OpenLayers 10.x
 *               is chosen for its vector layer performance and WMS tile support.
 * Inputs:
 *   - ./data/processed/charleston_floods_30y.json — fetched at startup.
 *   - DOM: #viewDiv, #stats, #trendsSummary, #cityComparison,
 *          #decisionAnalysis, #popup, #popup-content, #popup-closer,
 *          filter controls (yearStart, yearEnd, eventType, showRisk,
 *          showFema, showStormTracks).
 * Outputs:
 *   - Populated OL map; populated sidebar panels; print-ready layout on
 *     window.print() invocation.
 * Preconditions:
 *   - ol global available from CDN (OpenLayers 10.8+).
 *   - JSON file present and valid UTF-8.
 * Postconditions:
 *   - Map renders within 2 seconds on modern broadband.
 *   - All filter controls update the map and stats without page reload.
 * Assumptions:
 *   - User has a modern browser (ES2022 class fields, async/await, Intl).
 *   - FEMA WMS and NOAA WMS endpoints are public and require no API keys.
 * Side Effects:
 *   - Outbound WMS tile requests to hazards.fema.gov and idpgis.ncep.noaa.gov
 *     when respective layer toggles are enabled.
 * Failure Modes:
 *   - JSON fetch failure → console.error; map renders empty with alert.
 *   - WMS endpoint unreachable → tile layer shows no tiles (graceful).
 * Error Handling: try/catch around main fetch; WMS layers fail silently.
 * Constraints:   OL vector layer with 314 features is performant; above
 *                ~50 000 features would require clustering.
 * Verification:  Open in browser, toggle all filters, verify popup content,
 *                verify FEMA tiles appear, verify print layout.
 * References:
 *   - OpenLayers API: https://openlayers.org/en/latest/apidoc/
 *   - FEMA NFHL WMS: https://hazards.fema.gov/gis/nfhl/services/public/NFHL/
 *   - NOAA IBTrACS: https://www.ncei.noaa.gov/products/international-best-track-archive
 * ============================================================
 */

// ---------------------------------------------------------------------------
// ID:           CFHT-CANVAS-PATCH-001
// Requirement:  Suppress "Multiple readback operations using getImageData are
//               faster with willReadFrequently" browser warning from OL internals.
// Rationale:    OL creates 2D canvas contexts without willReadFrequently; this
//               IIFE monkey-patches HTMLCanvasElement before OL initialises.
// Side Effects: All subsequent 2D canvas contexts have willReadFrequently:true.
// ---------------------------------------------------------------------------
(function () {
  const _getContext = HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext = function (type, attrs) {
    if (type === "2d") {
      attrs = Object.assign({ willReadFrequently: true }, attrs || {});
    }
    return _getContext.call(this, type, attrs);
  };
})();

// ---------------------------------------------------------------------------
// ID:           CFHT-CONSTANTS-001
// Requirement:  Define stable color mappings for event types and risk levels.
// Rationale:
//   EVENT_COLORS: Each flood sub-type receives a distinct hue to help users
//     distinguish "coastal tidal surge" from "inland flash flood" visually.
//     Fatal events override these with red; injury-only events with yellow.
//     Orange (#ea580c) for Storm Surge is intentionally distinct from fatal
//     red (#dc2626) to avoid false alarm associations.
//   RISK_COLORS:  Muted single-hue blue gradient to communicate that risk
//     zones are city-scale statistical estimates, not regulatory flood maps.
//     Vivid red/orange avoided so users don't treat zones as definitive.
//   DAMAGE_CTX_LABELS: Human-readable label + icon for each narrative-derived
//     damage-context category from the pipeline.
// ---------------------------------------------------------------------------
const EVENT_COLORS = {
  "Coastal Flood":    "#0f766e",
  "Flash Flood":      "#2563eb",
  "Flood":            "#0284c7",
  "Lakeshore Flood":  "#7c3aed",
  "Storm Surge/Tide": "#ea580c",
};

const RISK_COLORS = {
  "Low":           [190, 215, 240],
  "Guarded":       [110, 165, 215],
  "Elevated":      [ 55, 115, 185],
  "High":          [ 25,  70, 150],
  "Most Affected": [ 10,  35, 110],
};

const DAMAGE_CTX_LABELS = {
  infra:       { label: "Infrastructure",  icon: "🛣️",  color: "#92400e", bg: "#fef3c7" },
  residential: { label: "Residential",     icon: "🏠",  color: "#1e3a5f", bg: "#dbeafe" },
  vehicle:     { label: "Vehicle",         icon: "🚗",  color: "#4b5563", bg: "#f3f4f6" },
  commercial:  { label: "Commercial",      icon: "🏢",  color: "#065f46", bg: "#d1fae5" },
  mixed:       { label: "Mixed",           icon: "🔀",  color: "#6b21a8", bg: "#ede9fe" },
  unknown:     { label: "Unclassified",    icon: "❓",  color: "#6b7280", bg: "#f9fafb" },
};

// ---------------------------------------------------------------------------
// ID:           CFHT-STYLE-001
// Requirement:  Compute OL circle style parameters for a flood event based on
//               severity priority: fatal > injury > type-based.
// Inputs:
//   ev (object):        Flood event from JSON (deaths, injuries, eventType).
//   baseRadius (number): Radius computed from damage magnitude (px).
// Outputs:
//   object: { fill, stroke, strokeW, z, r } — all required by eventFeature().
// Postconditions: Returns a valid style parameter object; never throws.
// ---------------------------------------------------------------------------
function styleParamsForEvent(ev, baseRadius) {
  const hasDeath  = (ev.deaths   || 0) > 0;
  const hasInjury = (ev.injuries || 0) > 0;
  if (hasDeath) {
    return { fill: "#dc2626", stroke: "#7f1d1d", strokeW: 1.5, z: 4, r: Math.max(baseRadius, 7) };
  }
  if (hasInjury) {
    return { fill: "#eab308", stroke: "#92400e", strokeW: 1.5, z: 3, r: Math.max(baseRadius, 6) };
  }
  return { fill: EVENT_COLORS[ev.eventType] || "#64748b", stroke: "#1f2937", strokeW: 0.7, z: 2, r: baseRadius };
}

// ---------------------------------------------------------------------------
// ID:           CFHT-RISK-COLOR-001
// Requirement:  Convert a risk level string and normalised score to an RGBA
//               fill color for risk zone polygons.
// Inputs:
//   level    (string): One of RISK_COLORS keys.
//   scoreNorm (number): ∈ [0,1]; controls opacity within the 0.07–0.42 range.
// Outputs:
//   string: CSS rgba() color string.
// Rationale:    Alpha capped at 0.42 so map labels remain legible even at
//               "Most Affected" zones, reinforcing the non-regulatory intent.
// ---------------------------------------------------------------------------
function colorForRisk(level, scoreNorm) {
  const base = RISK_COLORS[level] || RISK_COLORS.Low;
  const alpha = (0.07 + (scoreNorm || 0) * 0.35).toFixed(2);
  return `rgba(${base[0]},${base[1]},${base[2]},${alpha})`;
}

// ---------------------------------------------------------------------------
// ID:           CFHT-MONEY-001
// Requirement:  Format a number as a USD dollar string with no decimal places.
// Inputs:  x (number|null|undefined): Dollar amount.
// Outputs: string: Formatted currency string (e.g. "$1,250,000").
// ---------------------------------------------------------------------------
function money(x) {
  return new Intl.NumberFormat("en-US", {
    style: "currency", currency: "USD", maximumFractionDigits: 0,
  }).format(x || 0);
}

// ---------------------------------------------------------------------------
// ID:           CFHT-MONTH-001
// Requirement:  Extract the 3-letter month abbreviation from a NOAA date-time
//               string such as "01-MAY-95 14:30:00".
// Inputs:  dt (string): Raw NOAA BEGIN_DATE_TIME field value.
// Outputs: string: 3-char month name ("Jan"–"Dec") or "?" on parse failure.
// ---------------------------------------------------------------------------
function monthNameFromDateTime(dt) {
  const m = String(dt || "").match(/^\d{1,2}-([A-Za-z]{3})-/);
  return m ? m[1] : "?";
}

// ---------------------------------------------------------------------------
// ID:           CFHT-DAMAGE-BADGE-001
// Requirement:  Build an inline HTML badge showing the damage context inferred
//               from the narrative, or an empty string if context is absent.
// Inputs:  ctx (string|undefined): damageContext value from event JSON.
// Outputs: string: Self-contained HTML span element.
// ---------------------------------------------------------------------------
function damageBadgeHtml(ctx) {
  const info = DAMAGE_CTX_LABELS[ctx] || DAMAGE_CTX_LABELS.unknown;
  if (ctx === "unknown" || !ctx) return "";
  return (
    `<span style="display:inline-block;margin-top:4px;padding:2px 8px;` +
    `border-radius:10px;font-size:.75rem;font-weight:600;` +
    `background:${info.bg};color:${info.color}">` +
    `${info.icon} ${info.label} damage</span>`
  );
}

// ---------------------------------------------------------------------------
// ID:           CFHT-EVENT-FEATURE-001
// Requirement:  Create an OL Feature for one flood event: positioned at start
//               coordinates, styled by severity, carrying a popup HTML string.
// Inputs:
//   ev (object): Flood event from JSON dataset; must have start.lat/lon,
//                eventType, deaths, injuries, propertyDamageUSD, cropDamageUSD,
//                narrative, damageContext.
// Outputs:
//   ol.Feature: Styled point feature with "_popup" property set.
// Preconditions:  ev.start.lat and ev.start.lon are valid WGS-84 coordinates.
// Postconditions: Feature is ready to add to a VectorSource.
// ---------------------------------------------------------------------------
function eventFeature(ev) {
  const pt = new ol.Feature({
    geometry: new ol.geom.Point(ol.proj.fromLonLat([ev.start.lon, ev.start.lat])),
  });

  const dmg = (ev.propertyDamageUSD || 0) + (ev.cropDamageUSD || 0);
  const baseRadius = Math.max(4, Math.min(11, 4 + Math.log10(1 + dmg / 1000)));
  const sp = styleParamsForEvent(ev, baseRadius);
  const hasDeath  = (ev.deaths   || 0) > 0;
  const hasInjury = (ev.injuries || 0) > 0;

  pt.setStyle(new ol.style.Style({
    image: new ol.style.Circle({
      radius: sp.r,
      fill:   new ol.style.Fill({ color: sp.fill }),
      stroke: new ol.style.Stroke({ color: sp.stroke, width: sp.strokeW }),
    }),
    zIndex: sp.z,
  }));

  const fatalBanner = hasDeath
    ? `<div style="background:#fef2f2;border-left:3px solid #dc2626;padding:3px 6px;` +
      `margin-bottom:5px;border-radius:3px;font-weight:700;color:#991b1b">` +
      `⚠ Fatal Event — ${ev.deaths} death${ev.deaths > 1 ? "s" : ""}</div>`
    : "";
  const injuryBanner = hasInjury && !hasDeath
    ? `<div style="background:#fefce8;border-left:3px solid #eab308;padding:3px 6px;` +
      `margin-bottom:5px;border-radius:3px;font-weight:700;color:#92400e">` +
      `⚠ Injury Event — ${ev.injuries} injur${ev.injuries > 1 ? "ies" : "y"}, no deaths</div>`
    : "";

  // Data quality warning: NOAA DAMAGE_PROPERTY field was blank (not entered)
  // but the narrative describes clear physical impact — "$0" is misleading.
  const unreportedBanner = ev.damageUnreported
    ? `<div style="background:#fffbeb;border-left:3px solid #f59e0b;padding:3px 6px;` +
      `margin-bottom:5px;border-radius:3px;font-size:0.82em;color:#78350f">` +
      `⚠ <b>Damage not recorded</b> — NOAA field blank despite narrative ` +
      `describing physical impact. Figure may be significantly higher than $0.` +
      `</div>`
    : "";

  const dmgLabel = ev.damageUnreported
    ? `<span style="color:#b45309;font-style:italic">not recorded (see note)</span>`
    : money(dmg);

  pt.set(
    "_popup",
    fatalBanner + injuryBanner + unreportedBanner +
    `<b>${ev.eventType}</b><br/>` +
    `<b>Date:</b> ${ev.dateTime}<br/>` +
    `<b>County:</b> ${ev.county || "Unknown"}<br/>` +
    `<b>Reported damage:</b> ${dmgLabel}<br/>` +
    damageBadgeHtml(ev.damageContext) +
    `<br/><b>Injuries:</b> ${ev.injuries} <b>Deaths:</b> ${ev.deaths}` +
    (ev.narrative ? `<br/><br/><small style="color:#374151">${ev.narrative}</small>` : "")
  );
  return pt;
}

// ---------------------------------------------------------------------------
// ID:           CFHT-RISK-FEATURE-001
// Requirement:  Create an OL polygon Feature for one Gaussian risk zone cell.
// Inputs:
//   zone (object): Risk zone dict from JSON; must have bbox, level, scoreNorm.
// Outputs:
//   ol.Feature: Filled polygon with "_popup" property.
// ---------------------------------------------------------------------------
function riskFeature(zone) {
  const [xmin, ymin, xmax, ymax] = zone.bbox;
  const ring = [[xmin,ymin],[xmax,ymin],[xmax,ymax],[xmin,ymax],[xmin,ymin]]
    .map((c) => ol.proj.fromLonLat(c));

  const f = new ol.Feature({ geometry: new ol.geom.Polygon([ring]) });
  f.setStyle(new ol.style.Style({
    fill:   new ol.style.Fill({ color: colorForRisk(zone.level, zone.scoreNorm) }),
    stroke: new ol.style.Stroke({ color: "rgba(40,40,40,0.08)", width: 0.2 }),
    zIndex: 0,
  }));
  f.set("_popup",
    `<b>${zone.level} — City-scale estimate</b><br/>` +
    `<b>Flood risk score:</b> ${zone.score}<br/>` +
    `<small style="color:#6b7280">⚠ This zone reflects aggregate historical flood reports ` +
    `within the surrounding city area. Two homes in the same zone can have very different ` +
    `actual flood exposure depending on local elevation and drainage.<br/>` +
    `<b>Always verify your specific address at ` +
    `<a href="https://msc.fema.gov/portal/home" target="_blank" rel="noreferrer">msc.fema.gov</a>` +
    `.</b></small>`);
  return f;
}

// ---------------------------------------------------------------------------
// ID:           CFHT-CITY-FEATURE-001
// Requirement:  Create an OL point Feature for a study-city anchor marker.
// Inputs:
//   place (object): City dict from JSON; must have name, lat, lon.
// Outputs:
//   ol.Feature: Diamond-shaped marker with city label and "_popup" property.
// ---------------------------------------------------------------------------
function cityFeature(place) {
  const f = new ol.Feature({ geometry: new ol.geom.Point(ol.proj.fromLonLat([place.lon, place.lat])) });
  f.setStyle(new ol.style.Style({
    image: new ol.style.RegularShape({
      points: 4, radius: 8, angle: Math.PI / 4,
      fill:   new ol.style.Fill({ color: "#0b3a5b" }),
      stroke: new ol.style.Stroke({ color: "#ffffff", width: 1.2 }),
    }),
    text: new ol.style.Text({
      text: place.name.split(",")[0],
      offsetY: -14,
      font: "600 12px 'Segoe UI', sans-serif",
      fill:   new ol.style.Fill({ color: "#0f172a" }),
      stroke: new ol.style.Stroke({ color: "rgba(255,255,255,0.9)", width: 3 }),
    }),
    zIndex: 4,
  }));
  f.set("_popup", `<b>${place.name}</b>`);
  return f;
}

// ---------------------------------------------------------------------------
// ID:           CFHT-STATS-001
// Requirement:  Populate the #stats sidebar panel with aggregate metrics for
//               the currently-filtered set of events.
// Inputs:
//   dataset (object):       Full parsed JSON dataset.
//   filteredEvents (array): Events currently passing filter criteria.
//   minDamageFilterUSD (number): Active minimum-damage filter threshold.
//   unreportedOnly (boolean): If true, only events with damageUnreported are shown.
// Outputs:  Mutates innerHTML of #stats element.
// Side Effects: DOM write only; no data mutation.
// ---------------------------------------------------------------------------
function renderStats(dataset, filteredEvents, minDamageFilterUSD = 0, unreportedOnly = false) {
  const statsEl = document.getElementById("stats");
  const byType = {};
  const byCtx  = {};
  let dmg = 0, injuries = 0, deaths = 0;
  const monthCounts = {};

  filteredEvents.forEach((e) => {
    byType[e.eventType] = (byType[e.eventType] || 0) + 1;
    const ctx = e.damageContext || "unknown";
    byCtx[ctx] = (byCtx[ctx] || 0) + 1;
    dmg += (e.propertyDamageUSD || 0) + (e.cropDamageUSD || 0);
    injuries += e.injuries || 0;
    deaths   += e.deaths   || 0;
    const m = monthNameFromDateTime(e.dateTime);
    monthCounts[m] = (monthCounts[m] || 0) + 1;
  });

  const unreportedCount = filteredEvents.filter((e) => e.damageUnreported).length;

  const topMonths = Object.entries(monthCounts)
    .sort((a, b) => b[1] - a[1]).slice(0, 3)
    .map(([m, c]) => `${m} (${c})`).join(", ");

  const typeRows = Object.entries(byType)
    .sort((a, b) => b[1] - a[1])
    .map(([t, c]) => `<li>${t}: ${c}</li>`).join("");

  // Damage context breakdown — surface the narrative-inferred split
  const ctxRows = Object.entries(byCtx)
    .sort((a, b) => b[1] - a[1])
    .map(([ctx, c]) => {
      const info = DAMAGE_CTX_LABELS[ctx] || DAMAGE_CTX_LABELS.unknown;
      return `<li>${info.icon} ${info.label}: ${c}</li>`;
    }).join("");

  statsEl.innerHTML =
    `<b>Events shown:</b> ${filteredEvents.length}<br/>` +
    `<b>Year range:</b> ${dataset.meta.year_range[0]}-${dataset.meta.year_range[1]}<br/>` +
    `<b>Min reported damage filter:</b> ${money(minDamageFilterUSD)}<br/>` +
    `<b>Data quality mode:</b> ${unreportedOnly ? "Unreported only" : "All events"}<br/>` +
    `<b>Total reported damage:</b> ${money(dmg)}<br/>` +
    (unreportedCount > 0
      ? `<span style="color:#92400e;font-size:0.85em">⚠ ${unreportedCount} event${unreportedCount > 1 ? "s" : ""} ` +
        `with <b>damage not recorded</b> in NOAA (field blank, narrative describes impact)</span><br/>`
      : "") +
    `<b>Injuries / Deaths:</b> ${injuries} / ${deaths}<br/>` +
    `<b>Top months:</b> ${topMonths || "N/A"}<br/>` +
    `<hr/>` +
    `<b>Event type split</b><ul>${typeRows}</ul>` +
    (ctxRows ? `<b>Damage context (narrative)</b><ul>${ctxRows}</ul>` : "");
}

// ---------------------------------------------------------------------------
// ID:           CFHT-TRENDS-001
// Requirement:  Populate the #trendsSummary panel with a decade-bucketed table,
//               a 5-year frequency trend verdict, and a fatality verdict.
// Purpose:      Answer two key questions users ask: "Is it getting worse?" and
//               "Are fatalities lower today?" without requiring statistical
//               expertise from the user.
// Inputs:
//   dataset (object):       Full parsed JSON dataset.
//   filteredEvents (array): Events currently passing filter criteria.
// Outputs:  Mutates innerHTML of #trendsSummary element.
// Rationale:
//   5-year comparison windows (2020-2024 vs 2015-2019) are long enough to
//   smooth year-to-year noise while being recent enough to be policy-relevant.
//   ±15% threshold for "worse/improving" avoids over-reacting to single-year
//   outliers.
// ---------------------------------------------------------------------------
function renderTrendsSummary(dataset, filteredEvents) {
  const el = document.getElementById("trendsSummary");
  if (!el) return;

  const byYear = {};
  filteredEvents.forEach((e) => {
    if (!byYear[e.year]) byYear[e.year] = { count: 0, deaths: 0, injuries: 0, damage: 0 };
    byYear[e.year].count++;
    byYear[e.year].deaths   += e.deaths   || 0;
    byYear[e.year].injuries += e.injuries || 0;
    byYear[e.year].damage   += (e.propertyDamageUSD || 0) + (e.cropDamageUSD || 0);
  });

  const years = Object.keys(byYear).map(Number).sort();
  if (years.length === 0) {
    el.innerHTML = `<h3>📊 Trend Summary</h3><p style="color:#6b7280;font-size:.82rem">No data for selected filters.</p>`;
    return;
  }

  // Decade buckets
  const decades = {};
  years.forEach((y) => {
    const d = Math.floor(y / 10) * 10;
    if (!decades[d]) decades[d] = { count: 0, deaths: 0, damage: 0, yrs: 0 };
    decades[d].count  += byYear[y].count;
    decades[d].deaths += byYear[y].deaths;
    decades[d].damage += byYear[y].damage;
    decades[d].yrs++;
  });

  // 5-year frequency trend
  const recent5 = years.filter((y) => y >= 2020);
  const prior5  = years.filter((y) => y >= 2015 && y < 2020);
  const sumCount = (ys) => ys.reduce((s, y) => s + byYear[y].count, 0);
  const recentAvg = recent5.length ? sumCount(recent5) / recent5.length : 0;
  const priorAvg  = prior5.length  ? sumCount(prior5)  / prior5.length  : 0;

  let trendVerdict, trendColor, trendIcon;
  if (priorAvg === 0) {
    trendVerdict = "Insufficient baseline"; trendColor = "#6b7280"; trendIcon = "❓";
  } else if (recentAvg > priorAvg * 1.15) {
    trendVerdict = "Getting worse"; trendColor = "#dc2626"; trendIcon = "📈";
  } else if (recentAvg < priorAvg * 0.85) {
    trendVerdict = "Improving"; trendColor = "#16a34a"; trendIcon = "📉";
  } else {
    trendVerdict = "Roughly stable"; trendColor = "#d97706"; trendIcon = "➡️";
  }

  // Fatality analysis
  const totalDeaths  = filteredEvents.reduce((s, e) => s + (e.deaths || 0), 0);
  const recentDeaths = recent5.reduce((s, y) => s + (byYear[y]?.deaths || 0), 0);
  const olderDeaths  = years.filter((y) => y < 2020).reduce((s, y) => s + (byYear[y]?.deaths || 0), 0);
  let fatalVerdict, fatalColor;
  if (totalDeaths === 0) {
    fatalVerdict = "No fatalities recorded"; fatalColor = "#16a34a";
  } else if (recentDeaths === 0 && olderDeaths > 0) {
    fatalVerdict = "Lower — none in recent years"; fatalColor = "#16a34a";
  } else if (recentDeaths > olderDeaths) {
    fatalVerdict = "Higher in recent years"; fatalColor = "#dc2626";
  } else {
    fatalVerdict = `${totalDeaths} total — steady`; fatalColor = "#d97706";
  }

  const decadeRows = Object.entries(decades).sort()
    .map(([d, v]) => {
      const avgPerYear = v.yrs ? (v.count / v.yrs).toFixed(1) : "—";
      const deathCell = v.deaths > 0
        ? `<td style="color:#dc2626;font-weight:700">${v.deaths}</td>`
        : `<td style="color:#16a34a">0</td>`;
      return `<tr><td>${d}s</td><td>${v.count}</td><td>${avgPerYear}/yr</td>${deathCell}<td>${money(v.damage)}</td></tr>`;
    }).join("");

  el.innerHTML = `
    <h3 style="margin:.35rem 0 .5rem">📊 Trend Summary</h3>
    <div class="trend-verdict" style="border-left-color:${trendColor}">
      <div class="trend-verdict-label">${trendIcon} Flooding frequency (2020–2024 vs 2015–2019)</div>
      <div class="trend-verdict-value" style="color:${trendColor}">${trendVerdict}</div>
      <div class="trend-verdict-detail">
        Recent avg: <b>${recentAvg.toFixed(1)} events/yr</b>&nbsp;·&nbsp;Prior avg: <b>${priorAvg.toFixed(1)} events/yr</b>
      </div>
    </div>
    <div class="trend-verdict" style="border-left-color:${fatalColor};margin-top:.45rem">
      <div class="trend-verdict-label">💀 Fatalities — are they lower today?</div>
      <div class="trend-verdict-value" style="color:${fatalColor}">${fatalVerdict}</div>
      <div class="trend-verdict-detail">
        2020–2024: <b>${recentDeaths}</b> &nbsp;·&nbsp; Before 2020: <b>${olderDeaths}</b> &nbsp;·&nbsp; Total: <b>${totalDeaths}</b>
      </div>
    </div>
    <table class="cmp-table" style="margin-top:.5rem">
      <thead><tr><th>Decade</th><th>Events</th><th>Rate</th><th>Deaths</th><th>Damage</th></tr></thead>
      <tbody>${decadeRows}</tbody>
    </table>
  `;
}

// ---------------------------------------------------------------------------
// ID:           CFHT-CITY-CMP-001
// Requirement:  Populate the #cityComparison panel with a ranked table showing
//               per-city event count and reported damage for filtered events.
// Inputs:
//   dataset (object):       Full parsed JSON dataset (provides city list).
//   filteredEvents (array): Events currently passing filter criteria.
// Outputs:  Mutates innerHTML of #cityComparison element.
// ---------------------------------------------------------------------------
function renderCityComparison(dataset, filteredEvents) {
  const tbl = document.getElementById("cityComparison");
  const rows = dataset.meta.cities.map((city) => {
    const near = filteredEvents.filter((e) => {
      const d1 = haversine(city.lat, city.lon, e.start.lat, e.start.lon);
      const d2 = haversine(city.lat, city.lon, e.end.lat, e.end.lon);
      return d1 <= dataset.meta.city_radius_miles || d2 <= dataset.meta.city_radius_miles;
    });
    const damage = near.reduce((s, e) => s + (e.propertyDamageUSD || 0) + (e.cropDamageUSD || 0), 0);
    const bad    = near.reduce((s, e) => s + (e.injuries || 0) + (e.deaths || 0) * 2, 0);
    return { name: city.name, count: near.length, damage, bad };
  });

  const ranked = rows.slice().sort((a, b) => a.count - b.count || a.damage - b.damage || a.bad - b.bad);
  let html = `<table class="cmp-table"><thead><tr><th>Metric</th>`;
  ranked.forEach((r, i) => {
    const badge = i === 0 ? " <span class='rank-winner'>Lower #1</span>" : "";
    html += `<th>${r.name}${badge}</th>`;
  });
  html += `</tr></thead><tbody>`;
  html += `<tr><td>Flood events (30y)</td>${ranked.map((r) => `<td>${r.count}</td>`).join("")}</tr>`;
  html += `<tr><td>Reported damage</td>${ranked.map((r) => `<td>${money(r.damage)}</td>`).join("")}</tr>`;
  html += `</tbody></table>`;
  tbl.innerHTML = html;
}

// ---------------------------------------------------------------------------
// ID:           CFHT-DECISION-001
// Requirement:  Populate the #decisionAnalysis panel with evidence-backed
//               answers to common flood-risk decision questions, a per-city
//               summary table, and data-source citations.
// Inputs:
//   dataset (object): Full parsed JSON dataset.
// Outputs:  Mutates innerHTML of #decisionAnalysis element.
// ---------------------------------------------------------------------------
function renderDecisionSection(dataset) {
  const el = document.getElementById("decisionAnalysis");
  const da = dataset.stats.decisionAnalysis;
  const cstats = dataset.stats.cities;

  const cityRows = dataset.meta.cities.map((c) => {
    const s = cstats[c.key];
    const peak = (s.peakMonths || []).map((p) => `${p.month} (${p.count})`).join(", ");
    return `<tr><td>${c.name}</td><td>${s.eventCount}</td><td>${s.avgPerYear}</td><td>${peak}</td><td>${money(s.totalDamageUSD)}</td></tr>`;
  }).join("");

  const evidence = da.evidence.map((e) =>
    `<li><a href="${e.url}" target="_blank" rel="noreferrer">${e.source}</a>: ${e.note}</li>`
  ).join("");
  const limits = da.limitations.map((x) => `<li>${x}</li>`).join("");

  el.innerHTML = `
    <h3>Location Decision Analysis (Flooding Focus)</h3>
    <p class="qa"><b>How often does flooding happen?</b><br/>${da.answers.how_often}</p>
    <p class="qa"><b>When does it usually happen?</b><br/>${da.answers.when}</p>
    <p class="qa"><b>Home, car, or stay put?</b><br/>${da.answers.home_or_car_or_stay}</p>
    <p class="qa"><b>Safety measures</b><br/>${da.answers.safety}</p>
    <p class="qa"><b>How often does insurance pay for flooded cars?</b><br/>${da.answers.insurance_auto}</p>
    <p class="qa"><b>How people live with it</b><br/>${da.answers.how_people_deal}</p>
    <table class="cmp-table">
      <thead><tr><th>City</th><th>Events (30y)</th><th>Avg / year</th><th>Peak months</th><th>Reported damage</th></tr></thead>
      <tbody>${cityRows}</tbody>
    </table>
    <h4>Evidence Sources</h4><ul>${evidence}</ul>
    <h4>Limitations</h4><ul>${limits}</ul>
  `;
}

// ---------------------------------------------------------------------------
// ID:           CFHT-PRINT-REPORT-001
// Requirement:  Populate a print-only summary block with current filters and
//               key metrics so PDF export reads like a concise report.
// Inputs:
//   dataset (object): Full parsed JSON dataset.
//   filteredEvents (array): Events currently passing filter criteria.
//   filters (object): { yearStart, yearEnd, eventType, minDamage }.
// Outputs:  Mutates innerHTML of #printReport element.
// Side Effects: DOM write only; no data mutation.
// ---------------------------------------------------------------------------
function renderPrintReport(dataset, filteredEvents, filters) {
  const el = document.getElementById("printReport");
  if (!el) return;

  const totalDamage = filteredEvents.reduce(
    (s, e) => s + (e.propertyDamageUSD || 0) + (e.cropDamageUSD || 0),
    0
  );
  const unreported = filteredEvents.filter((e) => e.damageUnreported).length;
  const byType = {};
  filteredEvents.forEach((e) => {
    byType[e.eventType] = (byType[e.eventType] || 0) + 1;
  });
  const typeSummary = Object.entries(byType)
    .sort((a, b) => b[1] - a[1])
    .map(([k, v]) => `${k}: ${v}`)
    .join(" | ");

  const generatedAt = new Date().toLocaleString("en-US", {
    year: "numeric", month: "short", day: "2-digit",
    hour: "2-digit", minute: "2-digit",
  });

  el.innerHTML =
    `<h2 style="margin:0 0 .2rem;font-size:1rem;color:#0b3a5b">Charleston Flood Risk Report Snapshot</h2>` +
    `<div><b>Generated:</b> ${generatedAt}</div>` +
    `<div><b>Filter window:</b> ${filters.yearStart}-${filters.yearEnd}</div>` +
    `<div><b>Event type:</b> ${filters.eventType || "All flood types"}</div>` +
    `<div><b>Minimum reported damage:</b> ${money(filters.minDamage)}</div>` +
    `<div><b>Data quality mode:</b> ${filters.unreportedOnly ? "Unreported only" : "All events"}</div>` +
    `<div><b>Events shown:</b> ${filteredEvents.length}</div>` +
    `<div><b>Total reported damage:</b> ${money(totalDamage)}</div>` +
    `<div><b>Damage not recorded flags:</b> ${unreported}</div>` +
    `<div><b>Type split:</b> ${typeSummary || "N/A"}</div>` +
    `<div style="margin-top:.2rem;color:#475569">` +
    `Data source: NOAA Storm Events (NCEI). "Damage not recorded" indicates blank NOAA damage fields with impact narratives.` +
    `</div>`;
}

// ---------------------------------------------------------------------------
// ID:           CFHT-HAVERSINE-001
// Requirement:  Compute great-circle distance in miles between two WGS-84
//               coordinate pairs.  Front-end mirror of the Python pipeline
//               function; used for city-proximity filtering in city comparison.
// Inputs:  lat1,lon1,lat2,lon2 (number): Decimal degrees.
// Outputs: number: Distance in statute miles.
// ---------------------------------------------------------------------------
function haversine(lat1, lon1, lat2, lon2) {
  const r = 3958.8;
  const dLat = ((lat2 - lat1) * Math.PI) / 180;
  const dLon = ((lon2 - lon1) * Math.PI) / 180;
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos((lat1 * Math.PI) / 180) * Math.cos((lat2 * Math.PI) / 180) *
    Math.sin(dLon / 2) ** 2;
  return r * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

// ---------------------------------------------------------------------------
// ID:           CFHT-FEMA-LAYER-001
// Requirement:  Build and return an OL ImageWMS layer that displays FEMA NFHL
//               Special Flood Hazard Area (SFHA) zones on demand.
// Purpose:      Allow users to compare historical event density against the
//               official regulatory flood-zone boundary in a single view.
// Rationale:
//   - FEMA NFHL WMS is free, public, no API key required.
//   - WMS layer 28 ("FIRM Flood Hazard Areas") is the standard polygon layer
//     showing Zone A, AE, AO, VE, and other SFHA designations.
//   - ImageWMS (vs TileWMS) is used to get pixel-perfect alignment without
//     seam artifacts at the expense of a slightly larger tile payload.
// Outputs:
//   ol.layer.Image: Pre-configured WMS layer, initially invisible.
// Side Effects:
//   - When visible, makes GET requests to hazards.fema.gov for each
//     map viewport change.  These requests are not intercepted or cached
//     by this application.
// References:
//   - FEMA NFHL services: https://hazards.fema.gov/gis/nfhl/services/public/NFHL/
//   - WMS capability URL:  .../MapServer/WMSServer?SERVICE=WMS&REQUEST=GetCapabilities
// ---------------------------------------------------------------------------
function buildFemaLayer() {
  return new ol.layer.Image({
    source: new ol.source.ImageWMS({
      url: "https://hazards.fema.gov/gis/nfhl/services/public/NFHL/MapServer/WMSServer",
      params: {
        LAYERS: "28",
        FORMAT: "image/png",
        TRANSPARENT: "true",
        VERSION: "1.3.0",
      },
      ratio: 1,
      serverType: "mapserver",
    }),
    opacity: 0.55,
    visible: false,
    zIndex: 1,
  });
}

// ---------------------------------------------------------------------------
// ID:           CFHT-STORM-TRACK-LAYER-001
// Requirement:  Build and return an OL TileWMS layer that overlays NOAA NHC
//               historical Atlantic hurricane best-track lines.
// Purpose:      Contextualise flooding events against the tropical systems
//               that frequently drive coastal and surge flooding in Charleston.
// Rationale:
//   - NHC best-track (HURDAT2) data is available via NOAA NCEI ArcGIS service.
//   - TileWMS is used for track lines (many small tiles) vs ImageWMS.
//   - Zoom threshold of 6 prevents the service from being queried at world-
//     scale where no meaningful track detail is renderable.
// Outputs:
//   ol.layer.Tile: Pre-configured WMS layer, initially invisible.
// Side Effects:
//   - When visible, makes GET requests to gis.ncdc.noaa.gov.
// References:
//   - IBTrACS WMS: https://gis.ncdc.noaa.gov/arcgis/rest/services/ibtracs/
// ---------------------------------------------------------------------------
function buildStormTrackLayer() {
  return new ol.layer.Tile({
    source: new ol.source.TileWMS({
      url: "https://gis.ncdc.noaa.gov/arcgis/services/ibtracs/IBTrACS_All/MapServer/WMSServer",
      params: {
        LAYERS: "0,1,2",
        FORMAT: "image/png",
        TRANSPARENT: "true",
        VERSION: "1.1.1",
      },
      serverType: "geoserver",
    }),
    opacity: 0.75,
    visible: false,
    zIndex: 1,
    minZoom: 4,
  });
}

// ---------------------------------------------------------------------------
// ID:           CFHT-PRINT-001
// Requirement:  Trigger the browser's native print dialog so users can produce
//               a PDF of the current map state and statistics panels.
// Purpose:      Provide an exportable risk report without a server-side PDF
//               renderer, avoiding paid third-party dependencies.
// Rationale:    window.print() + @media print CSS is sufficient for a single-
//               page risk summary.  The print stylesheet hides controls and
//               ensures the map canvas is captured via canvas.toDataURL().
// Side Effects: Opens the browser print dialog.  No DOM mutations.
// ---------------------------------------------------------------------------
function exportPDF() {
  window.print();
}

// ---------------------------------------------------------------------------
// ID:           CFHT-MAIN-001
// Requirement:  Async IIFE that bootstraps the entire application:
//               load JSON → build map → wire all controls → render all panels.
// Purpose:      Single entry point; all OL layer objects are in closure scope
//               to avoid global namespace pollution.
// Preconditions:  DOM loaded (script tag is deferred in index.html).
// Postconditions: Map is interactive; all sidebar panels are populated.
// Failure Modes:  JSON fetch failure → alert shown; map renders empty.
// ---------------------------------------------------------------------------
(async function main() {
  // ── 1. Load dataset ───────────────────────────────────────────────────────
  let dataset;
  try {
    const response = await fetch("./data/processed/charleston_floods_30y.json");
    dataset = await response.json();
  } catch (err) {
    console.error("Failed to load flood dataset:", err);
    alert("Could not load the flood dataset. Check the browser console for details.");
    return;
  }

  // ── 2. OL vector sources ──────────────────────────────────────────────────
  const floodSource = new ol.source.Vector();
  const citySource  = new ol.source.Vector();
  const riskSource  = new ol.source.Vector();

  const riskLayer  = new ol.layer.Vector({ source: riskSource,  zIndex: 0 });
  const floodLayer = new ol.layer.Vector({ source: floodSource, zIndex: 2 });
  const cityLayer  = new ol.layer.Vector({ source: citySource,  zIndex: 3 });

  // ── 3. WMS overlay layers ─────────────────────────────────────────────────
  const femaLayer       = buildFemaLayer();
  const stormTrackLayer = buildStormTrackLayer();

  // ── 4. Map initialisation ─────────────────────────────────────────────────
  const map = new ol.Map({
    target: "viewDiv",
    layers: [
      new ol.layer.Tile({ source: new ol.source.OSM() }),
      riskLayer,
      femaLayer,
      stormTrackLayer,
      floodLayer,
      cityLayer,
    ],
    view: new ol.View({ center: ol.proj.fromLonLat([-80.03, 32.92]), zoom: 10 }),
  });

  // ── 5. Popup overlay ──────────────────────────────────────────────────────
  const popupEl      = document.getElementById("popup");
  const popupContent = document.getElementById("popup-content");
  const popupCloser  = document.getElementById("popup-closer");
  const popup = new ol.Overlay({
    element: popupEl, positioning: "bottom-center", stopEvent: false, offset: [0, -8],
  });
  map.addOverlay(popup);

  popupCloser.addEventListener("click", (e) => {
    e.preventDefault();
    popup.setPosition(undefined);
  });

  map.on("click", (evt) => {
    const f = map.forEachFeatureAtPixel(evt.pixel, (x) => x);
    if (f && f.get("_popup")) {
      popupContent.innerHTML = f.get("_popup");
      popup.setPosition(evt.coordinate);
    } else {
      popup.setPosition(undefined);
    }
  });

  map.on("pointermove", (evt) => {
    map.getTargetElement().style.cursor = map.hasFeatureAtPixel(evt.pixel) ? "pointer" : "";
  });

  // ── 6. City markers (static, never filtered) ─────────────────────────────
  dataset.places.forEach((p) => citySource.addFeature(cityFeature(p)));

  // ── 7. Filter controls ────────────────────────────────────────────────────
  const yearStartEl  = document.getElementById("yearStart");
  const yearEndEl    = document.getElementById("yearEnd");
  const minDamageEl  = document.getElementById("minDamage");
  const showUnreportedOnlyEl = document.getElementById("showUnreportedOnly");
  const eventTypeEl  = document.getElementById("eventType");
  const showRiskEl   = document.getElementById("showRisk");
  const showFemaEl   = document.getElementById("showFema");
  const showTracksEl = document.getElementById("showStormTracks");
  const exportBtnEl  = document.getElementById("exportPdfBtn");

  yearStartEl.value = String(dataset.meta.year_range[0]);
  yearEndEl.value   = String(dataset.meta.year_range[1]);

  // ── 8. Redraw function ───────────────────────────────────────────────────
  /**
   * ID:       CFHT-REDRAW-001
   * Requirement: Re-filter dataset and redraw all dynamic layers when any
   *              filter control changes.
   * Side Effects: Clears and repopulates floodSource and riskSource;
   *               updates all sidebar panels.
   */
  function redraw() {
    const yStart = parseInt(yearStartEl.value, 10) || dataset.meta.year_range[0];
    const yEnd   = parseInt(yearEndEl.value,   10) || dataset.meta.year_range[1];
    const minDamage = Math.max(0, parseFloat(minDamageEl?.value || "0") || 0);
    const unreportedOnly = !!showUnreportedOnlyEl?.checked;
    const typeFilter = eventTypeEl.value;
    const showRisk   = showRiskEl.checked;

    const filtered = dataset.floodEvents.filter((e) => {
      if (e.year < yStart || e.year > yEnd) return false;
      if (typeFilter && e.eventType !== typeFilter) return false;
      const reportedDamage = (e.propertyDamageUSD || 0) + (e.cropDamageUSD || 0);
      if (reportedDamage < minDamage) return false;
      if (unreportedOnly && !e.damageUnreported) return false;
      return true;
    });

    floodSource.clear();
    filtered.forEach((e) => floodSource.addFeature(eventFeature(e)));

    riskSource.clear();
    riskLayer.setVisible(showRisk);
    if (showRisk) {
      const allZones = Object.values(dataset.riskZones).flat();
      allZones.forEach((z) => riskSource.addFeature(riskFeature(z)));
    }

    renderStats(dataset, filtered, minDamage, unreportedOnly);
    renderTrendsSummary(dataset, filtered);
    renderCityComparison(dataset, filtered);
    renderPrintReport(dataset, filtered, {
      yearStart: yStart,
      yearEnd: yEnd,
      eventType: typeFilter,
      minDamage,
      unreportedOnly,
    });
  }

  // ── 9. Wire filter control events ────────────────────────────────────────
  [yearStartEl, yearEndEl, minDamageEl, showUnreportedOnlyEl, eventTypeEl, showRiskEl].forEach((el) => {
    el.addEventListener("change", redraw);
  });

  if (showFemaEl) {
    showFemaEl.addEventListener("change", () => {
      femaLayer.setVisible(showFemaEl.checked);
    });
  }

  if (showTracksEl) {
    showTracksEl.addEventListener("change", () => {
      stormTrackLayer.setVisible(showTracksEl.checked);
    });
  }

  if (exportBtnEl) {
    exportBtnEl.addEventListener("click", exportPDF);
  }

  // Mobile panel toggle
  const panelToggleEl = document.getElementById("panelToggle");
  const sidebarEl     = document.getElementById("sidebar");
  if (panelToggleEl && sidebarEl) {
    panelToggleEl.addEventListener("click", () => {
      const isOpen = sidebarEl.classList.toggle("sidebar-open");
      panelToggleEl.textContent = isOpen ? "✕ Close" : "☰ Stats";
      panelToggleEl.setAttribute("aria-expanded", String(isOpen));
    });
  }

  // ── 10. Static sections ───────────────────────────────────────────────────
  renderDecisionSection(dataset);

  // ── 11. Initial draw ─────────────────────────────────────────────────────
  redraw();
})();
