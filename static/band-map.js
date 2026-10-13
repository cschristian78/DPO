(function () {
  var el = document.getElementById("band-map");
  var dataEl = document.getElementById("band-map-data");
  var measureSelect = document.getElementById("band-measure");
  var periodWrap = document.getElementById("band-period-wrap");
  var periodSelect = document.getElementById("band-period");
  var legend = document.getElementById("band-legend");
  var rowsEl = document.getElementById("band-rows");
  var applyButton = document.getElementById("band-apply");
  var methodSelect = document.getElementById("band-method");
  var countInput = document.getElementById("band-count");
  var countWrap = document.getElementById("band-count-wrap");
  var recalcButton = document.getElementById("band-recalc");
  var addButton = document.getElementById("band-add");
  var bandsPanel = rowsEl ? rowsEl.closest(".map-bands") : null;
  if (!el || !dataEl || !measureSelect || !periodSelect || !rowsEl || !methodSelect || !countInput || !recalcButton || !addButton || !window.L || !window.topojson) return;

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
  var colorByRank = false;
  var builtPeriod = "";

  function text(value) {
    return value === null || value === undefined || value === "" ? "" : String(value);
  }

  function amount(value, digits) {
    if (value == null || !isFinite(Number(value))) return "";
    return Number(value).toLocaleString("en-US", { maximumFractionDigits: digits });
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
      point.dtc == null ? "DTC Unknown" : "DTC " + amount(point.dtc, 2) + " mi",
      point.elevation == null ? "Elevation Unknown" : "Elevation " + amount(point.elevation, 0) + " ft",
      point.incept ? "Incept " + point.incept : "",
      point.expire ? "Expire " + point.expire : "",
      point.locationValue == null ? "" : "Location value " + money.format(point.locationValue),
      "Marginal DIFF " + money.format(diff || 0),
      premiumLine(point)
    ];
    return lines.filter(Boolean).join("<br>");
  }

  function measure() {
    return measureSelect.value || "diff";
  }

  function usesPeriod() {
    return measure() === "diff" || measure() === "ratio";
  }

  function formatRatio(value) {
    return Number(value).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 4 });
  }

  function premiumLine(point) {
    var premium = Number(point.premium);
    if (!isFinite(premium) || premium <= 0) return "No premium";
    var ratio = ratioValue(point, String(periodSelect.value));
    var line = "Premium " + money.format(premium);
    if (ratio == null) return line + "<br>Premium to PML Ratio —";
    return line + "<br>Premium to PML Ratio " + formatRatio(ratio);
  }

  function presetBands() {
    var ranges = payload.ranges || {};
    if (measure() === "dtc") return ranges.dtc || [];
    if (measure() === "elevation") return ranges.elevation || [];
    return null;
  }

  function round2(value) {
    return Math.round(value * 100) / 100;
  }

  function roundEdge(value) {
    var scale = measure() === "ratio" ? 10000 : 100;
    return Math.round(value * scale) / scale;
  }

  function diffForPeriod(point, period) {
    var value = point.diffs ? point.diffs[period] : 0;
    return value == null ? 0 : Number(value);
  }

  function ratioValue(point, period) {
    var premium = Number(point.premium);
    if (!isFinite(premium) || premium <= 0) return null;
    var diff = diffForPeriod(point, period);
    if (!isFinite(diff) || diff <= 0) return null;
    return premium / diff;
  }

  function valuesForMeasure() {
    var period = String(periodSelect.value);
    return locations.map(function (point) {
      if (measure() === "ratio") return ratioValue(point, period);
      if (measure() === "dtc") return point.dtc == null || !isFinite(Number(point.dtc)) ? null : Number(point.dtc);
      if (measure() === "elevation") return point.elevation == null || !isFinite(Number(point.elevation)) ? null : Number(point.elevation);
      return diffForPeriod(point, period);
    });
  }

  function finiteValues(values) {
    return values.filter(function (value) {
      return value != null && isFinite(value);
    });
  }

  function rangeCount() {
    var count = Number(countInput.value);
    if (!isFinite(count)) return null;
    count = Math.round(count);
    if (count < 1 || count > 20) return null;
    return count;
  }

  function evenEdges(values, count) {
    var min = values.length ? Math.min.apply(null, values) : 0;
    var max = values.length ? Math.max.apply(null, values) : 0;
    var edges = [];
    for (var index = 0; index <= count; index += 1) {
      edges.push(roundEdge(min + (max - min) * index / count));
    }
    if (edges[0] > min) edges[0] = min;
    if (edges[count] < max) edges[count] = max;
    for (var step = 1; step < edges.length; step += 1) {
      if (edges[step] < edges[step - 1]) edges[step] = edges[step - 1];
    }
    return edges;
  }

  function equalCountBands(values, count) {
    var sorted = values.slice().sort(function (a, b) { return a - b; });
    var total = sorted.length;
    var bands = [];
    for (var band = 0; band < count; band += 1) {
      if (!total) {
        bands.push({ start: 0, end: 0 });
        continue;
      }
      var startIndex = Math.floor(band * total / count);
      var endIndex = Math.floor((band + 1) * total / count) - 1;
      if (startIndex >= total) startIndex = total - 1;
      if (endIndex < startIndex) endIndex = startIndex;
      var startValue = roundEdge(sorted[startIndex]);
      var endValue = roundEdge(sorted[endIndex]);
      if (endValue < startValue) endValue = startValue;
      bands.push({ start: startValue, end: endValue });
    }
    return bands;
  }

  function rankBands(values, count) {
    var order = [];
    values.forEach(function (value, index) {
      if (value == null || !isFinite(value)) return;
      order.push({ value: value, index: index });
    });
    order.sort(function (a, b) {
      if (a.value !== b.value) return a.value - b.value;
      return a.index - b.index;
    });
    var assignment = new Array(values.length);
    var total = order.length;
    for (var index = 0; index < values.length; index += 1) assignment[index] = -1;
    order.forEach(function (item, position) {
      assignment[item.index] = total ? Math.min(count - 1, Math.floor(position * count / total)) : -1;
    });
    return assignment;
  }

  function savedColors() {
    var found = [];
    rowsEl.querySelectorAll(".map-band-row input[type='color']").forEach(function (input) {
      found.push(input.value);
    });
    return found;
  }

  function addBandRow(index, start, end, previousColors) {
    var row = document.createElement("div");
    row.className = "map-band-row";
    var color = document.createElement("input");
    color.type = "color";
    color.value = previousColors[index] || colors[index % colors.length];
    color.setAttribute("aria-label", "Range " + (index + 1) + " color");
    var from = document.createElement("input");
    from.type = "number";
    from.step = "any";
    from.value = String(start);
    from.setAttribute("aria-label", "Range " + (index + 1) + " from");
    var to = document.createElement("input");
    to.type = "number";
    to.step = "any";
    to.value = String(end);
    to.setAttribute("aria-label", "Range " + (index + 1) + " to");
    var count = document.createElement("span");
    count.className = "map-band-count";
    count.textContent = "0";
    row.append(color, from, to, count);
    if (methodSelect.value === "user") {
      var remove = document.createElement("button");
      remove.type = "button";
      remove.className = "band-remove";
      remove.textContent = "\u00d7";
      remove.setAttribute("aria-label", "Remove range " + (index + 1));
      remove.addEventListener("click", function () {
        row.remove();
        relabelRows();
        if (!readBands().error) paint();
      });
      row.appendChild(remove);
    }
    rowsEl.appendChild(row);
  }

  function relabelRows() {
    rowsEl.querySelectorAll(".map-band-row").forEach(function (row, index) {
      var inputs = row.querySelectorAll("input");
      inputs[0].setAttribute("aria-label", "Range " + (index + 1) + " color");
      inputs[1].setAttribute("aria-label", "Range " + (index + 1) + " from");
      inputs[2].setAttribute("aria-label", "Range " + (index + 1) + " to");
      var remove = row.querySelector(".band-remove");
      if (remove) remove.setAttribute("aria-label", "Remove range " + (index + 1));
    });
  }

  function clearUserRanges() {
    colorByRank = false;
    rowsEl.replaceChildren();
  }

  function buildRows() {
    var preset = presetBands();
    var edges = null;
    var counted = null;
    var rowCount = 5;
    if (preset) {
      rowCount = preset.length;
      if (!rowCount) {
        rowsEl.replaceChildren();
        if (status) status.textContent = measure() === "dtc"
          ? "No distance-to-coast ranges are set."
          : "No elevation ranges are set.";
        return false;
      }
    } else if (methodSelect.value === "user") {
      clearUserRanges();
      return true;
    } else {
      var count = rangeCount();
      if (!count) {
        if (status) status.textContent = "Enter a number of ranges from 1 to 20.";
        return false;
      }
      var values = finiteValues(valuesForMeasure());
      if (measure() === "ratio" && !values.length) {
        colorByRank = false;
        rowsEl.replaceChildren();
        builtPeriod = String(periodSelect.value);
        return true;
      }
      if (methodSelect.value === "count") {
        counted = equalCountBands(values, count);
        rowCount = counted.length;
        colorByRank = true;
      } else {
        edges = evenEdges(values, count);
        rowCount = count;
        colorByRank = false;
      }
      builtPeriod = String(periodSelect.value);
    }
    var previousColors = savedColors();
    rowsEl.replaceChildren();
    for (var index = 0; index < rowCount; index += 1) {
      var start = preset ? preset[index].start : counted ? counted[index].start : edges[index];
      var end = preset ? preset[index].end : counted ? counted[index].end : edges[index + 1];
      addBandRow(index, start, end, previousColors);
    }
    return true;
  }

  function readBands() {
    var bands = [];
    var rows = rowsEl.querySelectorAll(".map-band-row");
    for (var index = 0; index < rows.length; index += 1) {
      var inputs = rows[index].querySelectorAll("input");
      var startText = String(inputs[1].value).trim();
      var endText = String(inputs[2].value).trim();
      var start = Number(startText);
      var end = Number(endText);
      if (startText === "" || endText === "" || !isFinite(start) || !isFinite(end)) {
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
    if (!pointsLayer) return;
    var bands = read.bands;
    var period = String(periodSelect.value);
    var values = valuesForMeasure();
    var ranks = colorByRank && usesPeriod() ? rankBands(values, bands.length) : null;
    var outside = 0;
    var missing = 0;
    var noPremium = 0;
    var noDiff = 0;
    if (pointsLayer) pointsLayer.clearLayers();
    locations.forEach(function (point, locationIndex) {
      var diff = diffForPeriod(point, period);
      var raw = values[locationIndex];
      var absent = raw == null || !isFinite(Number(raw));
      var index = absent ? -1 : ranks ? ranks[locationIndex] : bandFor(Number(raw), bands);
      var color = index < 0 ? outsideColor : bands[index].color;
      if (absent) {
        missing += 1;
        if (measure() === "ratio") {
          var premium = Number(point.premium);
          if (!isFinite(premium) || premium <= 0) noPremium += 1;
          else noDiff += 1;
        }
      } else if (index < 0) outside += 1;
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
    var userEmpty = usesPeriod() && methodSelect.value === "user" && !bands.length;
    var message = locations.length + " locations plotted.";
    if (userEmpty) message += " Add a range, then apply.";
    else {
      if (measure() === "ratio") {
        if (noPremium) message += " " + noPremium + " have no premium.";
        if (noDiff) message += " " + noDiff + " have premium and no marginal DIFF at this return period.";
      } else if (missing) {
        message += " " + missing + " have no " + (measure() === "dtc" ? "distance to coast" : "elevation") + ".";
      }
      if (outside) message += " " + outside + " fall outside the ranges.";
    }
    if (payload.skipped) message += " " + payload.skipped + " had no coordinates.";
    if (!userEmpty && usesPeriod() && methodSelect.value !== "user" && builtPeriod && builtPeriod !== period) {
      message += " Choose ReCalc to rebuild the ranges for this return period.";
    }
    if (status) status.textContent = message;
  }

  var ratioBaselineSet = false;

  function useRatioBaseline() {
    if (ratioBaselineSet || measure() !== "ratio") return;
    ratioBaselineSet = true;
    var options = periodSelect.options;
    for (var index = 0; index < options.length; index += 1) {
      if (options[index].value === "100") {
        periodSelect.value = "100";
        return;
      }
    }
  }

  function syncMethod() {
    var user = methodSelect.value === "user";
    var periodMode = usesPeriod();
    if (countWrap) countWrap.hidden = !periodMode || user;
    recalcButton.hidden = !periodMode || user;
    addButton.hidden = !periodMode || !user;
    if (bandsPanel) bandsPanel.classList.toggle("user-defined", periodMode && user);
  }

  function syncMeasure() {
    var periodMode = usesPeriod();
    if (periodWrap) periodWrap.hidden = !periodMode;
    syncMethod();
    var legendText = "DIFF ranges";
    var aria = "All locations by DIFF range";
    if (measure() === "ratio") {
      legendText = "Premium to PML Ratio";
      aria = "All locations by Premium to PML Ratio";
    } else if (measure() === "dtc") {
      legendText = "DTC ranges";
      aria = "All locations by DTC range";
    } else if (measure() === "elevation") {
      legendText = "Elevation ranges";
      aria = "All locations by elevation range";
    }
    if (legend) legend.textContent = legendText;
    el.setAttribute("aria-label", aria);
  }

  function resetRanges() {
    syncMeasure();
    if (buildRows()) paint();
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

  measureSelect.addEventListener("change", function () {
    useRatioBaseline();
    resetRanges();
  });
  periodSelect.addEventListener("change", function () {
    if (!usesPeriod()) return;
    colorByRank = false;
    paint();
  });
  recalcButton.addEventListener("click", function () {
    if (!usesPeriod() || methodSelect.value === "user") return;
    if (buildRows()) paint();
  });
  addButton.addEventListener("click", function () {
    if (rowsEl.querySelectorAll(".map-band-row").length >= 20) {
      if (status) status.textContent = "Twenty ranges is the maximum.";
      return;
    }
    addBandRow(rowsEl.querySelectorAll(".map-band-row").length, "", "", savedColors());
  });
  methodSelect.addEventListener("change", function () {
    syncMethod();
    if (!usesPeriod()) return;
    if (methodSelect.value === "user") {
      clearUserRanges();
      paint();
    }
  });
  applyButton.addEventListener("click", function () {
    colorByRank = false;
    paint();
  });
})();
