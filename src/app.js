/* Charleston Flood History Tracker - OpenLayers map app */

// Patch HTMLCanvasElement.getContext so that every 2D canvas (including the
// ones OpenLayers creates internally) is created with willReadFrequently:true.
// This silences the "Multiple readback operations using getImageData are faster
// with willReadFrequently" browser warning that originates inside ol/render.
(function () {
  const _getContext = HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext = function (type, attrs) {
    if (type === "2d") {
      attrs = Object.assign({ willReadFrequently: true }, attrs || {});
    }
    return _getContext.call(this, type, attrs);
  };
})();

const EVENT_COLORS = {
  "Coastal Flood": "#0f766e",
  "Flash Flood": "#2563eb",
  "Flood": "#0284c7",
  "Lakeshore Flood": "#7c3aed",
  "Storm Surge/Tide": "#dc2626",
};

// Muted single-hue blue gradient — intentionally desaturated to communicate that
// these are city-scale statistical estimates, not parcel-level determinations.
// Vivid red/orange palettes are avoided so users do not treat the zones as
// definitive; always verify against FEMA flood maps for a specific address.
const RISK_COLORS = {
  "Low":          [190, 215, 240],  // pale blue-gray
  "Guarded":      [110, 165, 215],  // soft medium blue
  "Elevated":     [ 55, 115, 185],  // medium blue
  "High":         [ 25,  70, 150],  // deep blue
  "Most Affected":[ 10,  35, 110],  // dark navy
};

function colorForEvent(type) {
  return EVENT_COLORS[type] || "#64748b";
}

function colorForRisk(level, scoreNorm) {
  const base = RISK_COLORS[level] || RISK_COLORS.Low;
  // Cap alpha at 0.42 so even the highest zones remain semi-transparent,
  // reinforcing that this is a city-scale approximation, not a hard boundary.
  const alpha = (0.07 + (scoreNorm || 0) * 0.35).toFixed(2);
  return `rgba(${base[0]},${base[1]},${base[2]},${alpha})`;
}

function money(x) {
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 }).format(x || 0);
}

function monthNameFromDateTime(dt) {
  const m = String(dt || "").match(/^\d{1,2}-([A-Za-z]{3})-/);
  return m ? m[1] : "?";
}

function eventFeature(ev) {
  const pt = new ol.Feature({
    geometry: new ol.geom.Point(ol.proj.fromLonLat([ev.start.lon, ev.start.lat])),
  });

  const dmg = (ev.propertyDamageUSD || 0) + (ev.cropDamageUSD || 0);
  const radius = Math.max(4, Math.min(11, 4 + Math.log10(1 + dmg / 1000)));
  const hasDeath = (ev.deaths || 0) > 0;

  pt.setStyle(
    new ol.style.Style({
      image: new ol.style.Circle({
        radius: hasDeath ? Math.max(radius, 7) : radius,
        fill: new ol.style.Fill({ color: hasDeath ? "#dc2626" : colorForEvent(ev.eventType) }),
        stroke: new ol.style.Stroke({ color: hasDeath ? "#7f1d1d" : "#1f2937", width: hasDeath ? 1.5 : 0.7 }),
      }),
      zIndex: hasDeath ? 3 : 2,
    })
  );

  const totalDamage = (ev.propertyDamageUSD || 0) + (ev.cropDamageUSD || 0);
  pt.set(
    "_popup",
    (hasDeath ? `<div style="background:#fef2f2;border-left:3px solid #dc2626;padding:3px 6px;margin-bottom:5px;border-radius:3px;font-weight:700;color:#991b1b">⚠ Fatal Event — ${ev.deaths} death${ev.deaths > 1 ? "s" : ""}</div>` : "") +
      `<b>${ev.eventType}</b><br/>` +
      `<b>Date:</b> ${ev.dateTime}<br/>` +
      `<b>County:</b> ${ev.county || "Unknown"}<br/>` +
      `<b>Damage:</b> ${money(totalDamage)}<br/>` +
      `<b>Injuries:</b> ${ev.injuries} <b>Deaths:</b> ${ev.deaths}` +
      (ev.narrative ? `<br/><br/><small>${ev.narrative}</small>` : "")
  );
  return pt;
}

function riskFeature(zone) {
  const [xmin, ymin, xmax, ymax] = zone.bbox;
  const ring = [
    [xmin, ymin],
    [xmax, ymin],
    [xmax, ymax],
    [xmin, ymax],
    [xmin, ymin],
  ].map((c) => ol.proj.fromLonLat(c));

  const f = new ol.Feature({ geometry: new ol.geom.Polygon([ring]) });
  f.setStyle(
    new ol.style.Style({
      fill: new ol.style.Fill({ color: colorForRisk(zone.level, zone.scoreNorm) }),
      stroke: new ol.style.Stroke({ color: "rgba(40,40,40,0.08)", width: 0.2 }),
      zIndex: 0,
    })
  );
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

function cityFeature(place) {
  const f = new ol.Feature({ geometry: new ol.geom.Point(ol.proj.fromLonLat([place.lon, place.lat])) });
  f.setStyle(
    new ol.style.Style({
      image: new ol.style.RegularShape({
        points: 4,
        radius: 8,
        angle: Math.PI / 4,
        fill: new ol.style.Fill({ color: "#0b3a5b" }),
        stroke: new ol.style.Stroke({ color: "#ffffff", width: 1.2 }),
      }),
      text: new ol.style.Text({
        text: place.name.split(",")[0],
        offsetY: -14,
        font: "600 12px 'Segoe UI', sans-serif",
        fill: new ol.style.Fill({ color: "#0f172a" }),
        stroke: new ol.style.Stroke({ color: "rgba(255,255,255,0.9)", width: 3 }),
      }),
      zIndex: 4,
    })
  );
  f.set("_popup", `<b>${place.name}</b>`);
  return f;
}

function renderStats(dataset, filteredEvents) {
  const statsEl = document.getElementById("stats");
  const byType = {};
  let dmg = 0;
  let injuries = 0;
  let deaths = 0;
  const monthCounts = {};

  filteredEvents.forEach((e) => {
    byType[e.eventType] = (byType[e.eventType] || 0) + 1;
    dmg += (e.propertyDamageUSD || 0) + (e.cropDamageUSD || 0);
    injuries += e.injuries || 0;
    deaths += e.deaths || 0;
    const m = monthNameFromDateTime(e.dateTime);
    monthCounts[m] = (monthCounts[m] || 0) + 1;
  });

  const topMonths = Object.entries(monthCounts)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 3)
    .map(([m, c]) => `${m} (${c})`)
    .join(", ");

  const typeRows = Object.entries(byType)
    .sort((a, b) => b[1] - a[1])
    .map(([t, c]) => `<li>${t}: ${c}</li>`)
    .join("");

  statsEl.innerHTML =
    `<b>Events shown:</b> ${filteredEvents.length}<br/>` +
    `<b>Year range:</b> ${dataset.meta.year_range[0]}-${dataset.meta.year_range[1]}<br/>` +
    `<b>Total reported damage:</b> ${money(dmg)}<br/>` +
    `<b>Injuries / Deaths:</b> ${injuries} / ${deaths}<br/>` +
    `<b>Top months:</b> ${topMonths || "N/A"}<br/>` +
    `<hr/>` +
    `<b>Event type split</b><ul>${typeRows}</ul>`;
}

function renderCityComparison(dataset, filteredEvents) {
  const tbl = document.getElementById("cityComparison");
  const rows = dataset.meta.cities.map((city) => {
    const near = filteredEvents.filter((e) => {
      const d1 = haversine(city.lat, city.lon, e.start.lat, e.start.lon);
      const d2 = haversine(city.lat, city.lon, e.end.lat, e.end.lon);
      return d1 <= dataset.meta.city_radius_miles || d2 <= dataset.meta.city_radius_miles;
    });
    const damage = near.reduce((s, e) => s + (e.propertyDamageUSD || 0) + (e.cropDamageUSD || 0), 0);
    const bad = near.reduce((s, e) => s + (e.injuries || 0) + (e.deaths || 0) * 2, 0);
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

function renderDecisionSection(dataset) {
  const el = document.getElementById("decisionAnalysis");
  const da = dataset.stats.decisionAnalysis;
  const cstats = dataset.stats.cities;

  const cityRows = dataset.meta.cities
    .map((c) => {
      const s = cstats[c.key];
      const peak = (s.peakMonths || []).map((p) => `${p.month} (${p.count})`).join(", ");
      return `<tr><td>${c.name}</td><td>${s.eventCount}</td><td>${s.avgPerYear}</td><td>${peak}</td><td>${money(s.totalDamageUSD)}</td></tr>`;
    })
    .join("");

  const evidence = da.evidence.map((e) => `<li><a href="${e.url}" target="_blank" rel="noreferrer">${e.source}</a>: ${e.note}</li>`).join("");
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

    <h4>Evidence Sources</h4>
    <ul>${evidence}</ul>
    <h4>Limitations</h4>
    <ul>${limits}</ul>
  `;
}

function haversine(lat1, lon1, lat2, lon2) {
  const r = 3958.8;
  const dLat = ((lat2 - lat1) * Math.PI) / 180;
  const dLon = ((lon2 - lon1) * Math.PI) / 180;
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos((lat1 * Math.PI) / 180) *
      Math.cos((lat2 * Math.PI) / 180) *
      Math.sin(dLon / 2) ** 2;
  return r * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

(async function main() {
  const response = await fetch("./data/processed/charleston_floods_30y.json");
  const dataset = await response.json();

  const floodSource = new ol.source.Vector();
  const citySource = new ol.source.Vector();
  const riskSource = new ol.source.Vector();

  const riskLayer = new ol.layer.Vector({ source: riskSource, zIndex: 0 });
  const floodLayer = new ol.layer.Vector({ source: floodSource, zIndex: 2 });
  const cityLayer = new ol.layer.Vector({ source: citySource, zIndex: 3 });

  const map = new ol.Map({
    target: "viewDiv",
    layers: [new ol.layer.Tile({ source: new ol.source.OSM() }), riskLayer, floodLayer, cityLayer],
    view: new ol.View({ center: ol.proj.fromLonLat([-80.03, 32.92]), zoom: 10 }),
  });

  const popupEl = document.getElementById("popup");
  const popupContent = document.getElementById("popup-content");
  const popupCloser = document.getElementById("popup-closer");
  const popup = new ol.Overlay({ element: popupEl, positioning: "bottom-center", stopEvent: false, offset: [0, -8] });
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

  dataset.places.forEach((p) => citySource.addFeature(cityFeature(p)));

  const yearStartEl = document.getElementById("yearStart");
  const yearEndEl = document.getElementById("yearEnd");
  yearStartEl.value = String(dataset.meta.year_range[0]);
  yearEndEl.value = String(dataset.meta.year_range[1]);

  function applyFilters() {
    const yearStart = Number(yearStartEl.value);
    const yearEnd = Number(yearEndEl.value);
    const type = document.getElementById("eventType").value;
    const minDamage = Number(document.getElementById("minDamage").value);
    const showRisk = document.getElementById("toggleRisk").checked;

    const filtered = dataset.floodEvents.filter((e) => {
      const totalDamage = (e.propertyDamageUSD || 0) + (e.cropDamageUSD || 0);
      const typeOk = type === "ALL" || e.eventType === type;
      return e.year >= yearStart && e.year <= yearEnd && typeOk && totalDamage >= minDamage;
    });

    floodSource.clear();
    filtered.forEach((e) => floodSource.addFeature(eventFeature(e)));

    riskSource.clear();
    riskLayer.setVisible(showRisk);
    if (showRisk) {
      Object.values(dataset.riskZones).forEach((zones) => {
        zones.forEach((z) => riskSource.addFeature(riskFeature(z)));
      });
    }

    renderStats(dataset, filtered);
    renderCityComparison(dataset, filtered);
    renderDecisionSection(dataset);
  }

  document.getElementById("applyFilters").addEventListener("click", applyFilters);
  applyFilters();
})();
