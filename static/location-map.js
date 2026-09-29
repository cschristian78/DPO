(function () {
  var maps = document.querySelectorAll(".geo-map");
  if (!maps.length || !window.L || !window.topojson) return;

  var money = new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0
  });
  var lower48 = [[24.4, -125], [49.5, -66.9]];
  var boundariesUrl = maps[0].dataset.boundariesUrl;

  function text(value) {
    return value === null || value === undefined || value === "" ? "" : String(value);
  }

  function popup(point) {
    var id = point.id || point.entity_id;
    var lines = [
      id,
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
      point.diff == null ? "" : "Marginal DIFF " + money.format(point.diff)
    ];
    return lines.filter(Boolean).join("<br>");
  }

  function build(el, us, points) {
    var status = el.nextElementSibling && el.nextElementSibling.classList.contains("location-points")
      ? el.parentElement.querySelector(".location-map-status")
      : el.parentElement.querySelector(".location-map-status");
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
    var pointsLayer = L.layerGroup().addTo(map);
    var bounds = [];
    points.forEach(function (point) {
      var lat = point.lat != null ? point.lat : point.latitude;
      var lon = point.lon != null ? point.lon : point.longitude;
      if (lat == null || lon == null) return;
      L.circleMarker([lat, lon], {
        radius: 3.5,
        weight: 0.8,
        color: "#ffffff",
        fillColor: "#1d5fa6",
        fillOpacity: 0.85
      }).bindPopup(popup(point)).addTo(pointsLayer);
      bounds.push([lat, lon]);
    });
    var fitTarget = bounds.length ? bounds : lower48;
    map.fitBounds(fitTarget, { padding: [28, 28] });
    if (status) status.textContent = bounds.length + " locations plotted.";
    el._locationMap = map;
    el._fitTarget = fitTarget;
  }

  fetch(boundariesUrl)
    .then(function (response) { return response.json(); })
    .then(function (us) {
      maps.forEach(function (el) {
        var raw = el.nextElementSibling;
        var points = [];
        if (raw && raw.classList.contains("location-points")) {
          try { points = JSON.parse(raw.textContent || "[]"); } catch (e) { points = []; }
        }
        build(el, us, points);
      });
    })
    .catch(function () {
      maps.forEach(function (el) {
        var status = el.parentElement.querySelector(".location-map-status");
        if (status) status.textContent = "The location map could not be loaded.";
      });
    });

  window.addEventListener("resize", function () {
    maps.forEach(function (el) {
      if (el._locationMap) el._locationMap.invalidateSize();
    });
  });
})();
