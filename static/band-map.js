(function () {
  var el = document.getElementById("band-map");
  var dataEl = document.getElementById("band-map-data");
  var periodSelect = document.getElementById("band-period");
  var rowsEl = document.getElementById("band-rows");
  var applyButton = document.getElementById("band-apply");
  if (!el || !dataEl || !periodSelect || !rowsEl || !window.L || !window.topojson) return;

  var payload = {};
  try { payload = JSON.parse(dataEl.textContent || "{}"); } catch (e) { payload = {}; }
  var locations = payload.locations || [];
  var colors = ["#2166ac", "#67a9cf", "#fee090", "#f46d43", "#a50026"];
  var outsideColor = "#9aa3ad";
  var lower48 = [[24.4, -125], [49.5, -66.9]];
  var money = new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0
  });
  var status = document.getElementById("band-map-status");
  var pointsLayer = null;

  function text(value) {
    return value === null || value === undefined || value === "" ? "" : String(value);
  }

  function popup(point, diff) {
    var lines = [
      point.id,
      point.street || "",
      [point.county, point.state].filter(Boolean).join(", "),
      point.postalCode ? "Postal code " + point.postalCode : "",
      "Construction " + text(point.construction),
      "Occupancy " + text(point.occupancy),
      "Stories " + text(point.stories),
      "Year built " + text(point.yearBuilt),
      "Floor area " + (point.floorArea == null ? "" : Math.round(point.floorArea).toLocaleString("en-US")),
      point.incept ? "Incept " + point.incept : "",
      point.expire ? "Expire " + point.expire : "",
      point.locationValue == null ? "" : "Location value " + money.format(point.locationValue),
      "Marginal DIFF " + money.format(diff || 0)
    ];
    return lines.filter(Boolean).join("<br>");
  }

  function round2(value) {
    return Math.round(value * 100) / 100;
  }

  function diffsForPeriod() {
    var period = String(periodSelect.value);
    return locations.map(function (point) {
      var value = point.diffs ? point.diffs[period] : 0;
      return value == null ? 0 : Number(value);
    });
  }

  function evenEdges(values) {
    var min = values.length ? Math.min.apply(null, values) : 0;
    var max = values.length ? Math.max.apply(null, values) : 0;
    var edges = [round2(min)];
    for (var index = 1; index < 5; index += 1) {
      edges.push(round2(min + (max - min) * index / 5));
    }
    edges.push(round2(max));
    if (edges[0] > min) edges[0] = min;
    if (edges[5] < max) edges[5] = max;
    for (var step = 1; step < edges.length; step += 1) {
      if (edges[step] < edges[step - 1]) edges[step] = edges[step - 1];
    }
    return edges;
  }

  function buildRows() {
    var edges = evenEdges(diffsForPeriod());
    rowsEl.replaceChildren();
    for (var index = 0; index < 5; index += 1) {
      var row = document.createElement("div");
      row.className = "map-band-row";
      var color = document.createElement("input");
      color.type = "color";
      color.value = colors[index];
      color.setAttribute("aria-label", "Range " + (index + 1) + " color");
      var from = document.createElement("input");
      from.type = "number";
      from.step = "any";
      from.value = String(edges[index]);
      from.setAttribute("aria-label", "Range " + (index + 1) + " from");
      var to = document.createElement("input");
      to.type = "number";
      to.step = "any";
      to.value = String(edges[index + 1]);
      to.setAttribute("aria-label", "Range " + (index + 1) + " to");
      var count = document.createElement("span");
      count.className = "map-band-count";
      count.textContent = "0";
      row.append(color, from, to, count);
      rowsEl.appendChild(row);
    }
  }

  function readBands() {
    var bands = [];
    var rows = rowsEl.querySelectorAll(".map-band-row");
    for (var index = 0; index < rows.length; index += 1) {
      var inputs = rows[index].querySelectorAll("input");
      var start = Number(inputs[1].value);
      var end = Number(inputs[2].value);
      if (!isFinite(start) || !isFinite(end)) {
        return { error: "Range " + (index + 1) + " must be a number." };
      }
      if (start > end) {
        return { error: "Range " + (index + 1) + " starts above its end." };
      }
      bands.push({ start: start, end: end, color: inputs[0].value, count: 0, row: rows[index] });
    }
    return { bands: bands };
  }

  function bandFor(value, bands) {
    for (var index = 0; index < bands.length; index += 1) {
      var last = index === bands.length - 1;
      var band = bands[index];
      if (value >= band.start && (last ? value <= band.end : value < band.end)) return index;
    }
    return -1;
  }

  function paint() {
    var read = readBands();
    if (read.error) {
      if (status) status.textContent = read.error;
      return;
    }
    var bands = read.bands;
    var period = String(periodSelect.value);
    var outside = 0;
    if (pointsLayer) pointsLayer.clearLayers();
    locations.forEach(function (point) {
      var diff = point.diffs && point.diffs[period] != null ? Number(point.diffs[period]) : 0;
      var index = bandFor(diff, bands);
      var color = index < 0 ? outsideColor : bands[index].color;
      if (index < 0) outside += 1;
      else bands[index].count += 1;
      L.circleMarker([point.lat, point.lon], {
        radius: 3.5,
        weight: 0.8,
        color: "#ffffff",
        fillColor: color,
        fillOpacity: 0.9
      }).bindPopup(popup(point, diff)).addTo(pointsLayer);
    });
    bands.forEach(function (band) {
      band.row.querySelector(".map-band-count").textContent = String(band.count);
    });
    var message = locations.length + " locations plotted.";
    if (outside) message += " " + outside + " fall outside the ranges.";
    if (payload.skipped) message += " " + payload.skipped + " had no coordinates.";
    if (status) status.textContent = message;
  }

  function resetRanges() {
    buildRows();
    paint();
  }

  fetch(el.dataset.boundariesUrl)
    .then(function (response) { return response.json(); })
    .then(function (us) {
      var map = L.map(el, {
        preferCanvas: true,
        zoomSnap: 0.25,
        minZoom: 3,
        maxZoom: 12,
        attributionControl: false
      });
      L.geoJSON(topojson.feature(us, us.objects.states), {
        interactive: false,
        style: { stroke: false, fillColor: "#f6f6f2", fillOpacity: 1 }
      }).addTo(map);
      L.geoJSON(topojson.feature(us, us.objects.counties), {
        interactive: false,
        style: { color: "#c5c9c4", weight: 0.5, fill: false }
      }).addTo(map);
      L.geoJSON(topojson.mesh(us, us.objects.states, function (a, b) { return a !== b; }), {
        interactive: false,
        style: { color: "#5f666c", weight: 1.1, fill: false }
      }).addTo(map);
      pointsLayer = L.layerGroup().addTo(map);
      var bounds = locations.map(function (point) { return [point.lat, point.lon]; });
      var fitTarget = bounds.length ? bounds : lower48;
      map.fitBounds(fitTarget, { padding: [28, 28] });
      el._locationMap = map;
      el._fitTarget = fitTarget;
      resetRanges();
    })
    .catch(function () {
      if (status) status.textContent = "The location map could not be loaded.";
    });

  periodSelect.addEventListener("change", resetRanges);
  applyButton.addEventListener("click", paint);
})();
