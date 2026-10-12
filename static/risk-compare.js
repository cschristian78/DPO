(function () {
  var panel = document.getElementById("result-compare");
  var input = document.getElementById("risk-search");
  var list = document.getElementById("risk-suggest");
  var picks = document.getElementById("risk-picks");
  var exposureHost = document.getElementById("risk-exposure");
  var curveHost = document.getElementById("risk-curves");
  if (!panel || !input || !list || !picks || !exposureHost || !curveHost) return;

  var ids = null;
  var curves = null;
  var exposure = null;
  var periods = null;
  var selected = [];
  var active = -1;

  function text(node) {
    return (node && node.textContent ? node.textContent : "").trim();
  }

  function moneyNumber(value) {
    var cleaned = String(value || "").replace(/,/g, "").replace(/\u2014/g, "").trim();
    var number = Number(cleaned);
    return isFinite(number) ? number : 0;
  }

  function formatMoney(value) {
    return Math.round(value).toLocaleString("en-US");
  }

  function rowValues(row) {
    var cells = row.cells;
    var amounts = [];
    for (var i = 3; i < cells.length; i += 1) amounts.push(text(cells[i]));
    return { aal: text(cells[1]), std: text(cells[2]), amounts: amounts };
  }

  function indexTables() {
    if (ids) return;
    var exclTable = document.getElementById("result-table-excl");
    var diffTable = document.getElementById("result-table-diff");
    var exposureTable = document.getElementById("result-table-exposure");
    curves = {};
    exposure = {};
    periods = [];
    if (exclTable) {
      var headers = exclTable.querySelectorAll("thead th.c");
      Array.prototype.forEach.call(headers, function (header) {
        var span = header.querySelector("span");
        periods.push(span ? text(span) : text(header));
      });
      Array.prototype.forEach.call(exclTable.tBodies[0].rows, function (row) {
        var id = text(row.cells[0]);
        if (!id || id === "PORTFOLIO") return;
        curves[id] = { excl: rowValues(row) };
      });
    }
    if (diffTable) {
      Array.prototype.forEach.call(diffTable.tBodies[0].rows, function (row) {
        var id = text(row.cells[0]);
        if (!curves[id]) return;
        curves[id].diff = rowValues(row);
      });
    }
    if (exposureTable) {
      var labels = Array.prototype.map.call(exposureTable.tHead.rows[0].cells, text);
      var valueIndex = labels.indexOf("Location value");
      Array.prototype.forEach.call(exposureTable.tBodies[0].rows, function (row) {
        var id = text(row.cells[0]);
        exposure[id] = valueIndex >= 0 ? text(row.cells[valueIndex]) : "";
      });
    }
    ids = Object.keys(curves).sort(function (left, right) {
      return left.localeCompare(right, undefined, { numeric: true });
    });
  }

  function suggestions(query) {
    var needle = query.trim().toLowerCase();
    if (!needle) return [];
    var prefix = [];
    var contains = [];
    for (var i = 0; i < ids.length; i += 1) {
      if (selected.indexOf(ids[i]) !== -1) continue;
      var hay = ids[i].toLowerCase();
      var at = hay.indexOf(needle);
      if (at === 0 && prefix.length < 12) prefix.push(ids[i]);
      else if (at > 0 && contains.length < 12) contains.push(ids[i]);
      if (prefix.length >= 12) break;
    }
    return prefix.concat(contains).slice(0, 12);
  }

  function optionLabel(id, query) {
    var button = document.createElement("button");
    button.type = "button";
    button.setAttribute("role", "option");
    button.dataset.id = id;
    var needle = query.trim().toLowerCase();
    var at = id.toLowerCase().indexOf(needle);
    if (at < 0 || !needle) {
      button.textContent = id;
      return button;
    }
    button.appendChild(document.createTextNode(id.slice(0, at)));
    var mark = document.createElement("mark");
    mark.textContent = id.slice(at, at + needle.length);
    button.appendChild(mark);
    button.appendChild(document.createTextNode(id.slice(at + needle.length)));
    return button;
  }

  function closeList() {
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
    active = -1;
  }

  function openList() {
    indexTables();
    var found = suggestions(input.value);
    list.innerHTML = "";
    active = found.length ? 0 : -1;
    if (!input.value.trim()) {
      closeList();
      return;
    }
    if (!found.length) {
      var empty = document.createElement("li");
      empty.className = "risk-empty";
      empty.textContent = "No matching risk id";
      list.appendChild(empty);
    }
    found.forEach(function (id, index) {
      var item = document.createElement("li");
      var button = optionLabel(id, input.value);
      button.id = "risk-option-" + index;
      button.setAttribute("aria-selected", index === active ? "true" : "false");
      button.addEventListener("mousedown", function (event) { event.preventDefault(); });
      button.addEventListener("click", function () { choose(id); });
      item.appendChild(button);
      list.appendChild(item);
    });
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
    input.setAttribute("aria-activedescendant", active >= 0 ? "risk-option-0" : "");
  }

  function moveActive(step) {
    var options = list.querySelectorAll("[role='option']");
    if (!options.length) return;
    active = (active + step + options.length) % options.length;
    Array.prototype.forEach.call(options, function (option, index) {
      option.setAttribute("aria-selected", index === active ? "true" : "false");
    });
    input.setAttribute("aria-activedescendant", options[active].id);
    options[active].scrollIntoView({ block: "nearest" });
  }

  function choose(id) {
    if (selected.indexOf(id) === -1) selected.push(id);
    input.value = "";
    closeList();
    render();
    input.focus();
  }

  function remove(id) {
    selected = selected.filter(function (item) { return item !== id; });
    render();
  }

  function renderPicks() {
    picks.innerHTML = "";
    selected.forEach(function (id) {
      var chip = document.createElement("span");
      chip.className = "risk-pick";
      chip.appendChild(document.createTextNode(id));
      var button = document.createElement("button");
      button.type = "button";
      button.setAttribute("aria-label", "Remove " + id);
      button.textContent = "\u00d7";
      button.addEventListener("click", function () { remove(id); });
      chip.appendChild(button);
      picks.appendChild(chip);
    });
  }

  function renderExposure() {
    exposureHost.innerHTML = "";
    if (!selected.length) {
      var prompt = document.createElement("p");
      prompt.className = "muted";
      prompt.textContent = "Type a risk id and choose one or more matches. The exposure summary and both PML curves appear here.";
      exposureHost.appendChild(prompt);
      return;
    }
    var heading = document.createElement("h3");
    heading.textContent = "Exposure summary";
    exposureHost.appendChild(heading);
    var note = document.createElement("p");
    note.className = "muted";
    note.textContent = "Location value of the selected risks.";
    exposureHost.appendChild(note);
    var values = selected.map(function (id) { return moneyNumber(exposure[id]); });
    var peak = values.reduce(function (max, value) { return value > max ? value : max; }, 0);
    if (!peak) {
      var missing = document.createElement("p");
      missing.className = "muted";
      missing.textContent = "No location value is stored for the selected risks.";
      exposureHost.appendChild(missing);
      return;
    }
    var chart = document.createElement("div");
    chart.className = "column-chart exposure-summary" + (selected.length > 8 ? " names" : "");
    chart.style.setProperty("--cols", String(selected.length));
    var axis = document.createElement("div");
    axis.className = "column-yaxis";
    axis.setAttribute("aria-hidden", "true");
    [formatMoney(peak), formatMoney(peak / 2), "0"].forEach(function (label) {
      axis.appendChild(document.createElement("span")).textContent = label;
    });
    var bars = document.createElement("ol");
    bars.className = "impact-chart share-chart";
    bars.setAttribute("aria-label", "Location value by risk");
    selected.forEach(function (id, index) {
      var item = document.createElement("li");
      var value = values[index];
      item.setAttribute("aria-label", id + ": " + formatMoney(value));
      var amount = document.createElement("span");
      amount.className = "impact-value";
      amount.textContent = formatMoney(value);
      var track = document.createElement("span");
      track.className = "impact-track";
      var bar = document.createElement("span");
      bar.className = "impact-bar";
      bar.style.height = (value / peak * 100).toFixed(1) + "%";
      track.appendChild(bar);
      var name = document.createElement("span");
      name.className = "impact-id";
      name.title = id;
      name.textContent = id;
      item.appendChild(amount);
      item.appendChild(track);
      item.appendChild(name);
      bars.appendChild(item);
    });
    chart.appendChild(axis);
    chart.appendChild(bars);
    exposureHost.appendChild(chart);
  }

  function addCell(row, value, className) {
    var cell = document.createElement("td");
    if (className) cell.className = className;
    cell.textContent = value || "\u2014";
    row.appendChild(cell);
  }

  function renderCurves() {
    curveHost.innerHTML = "";
    if (!selected.length) return;
    var heading = document.createElement("h3");
    heading.textContent = "PML curves";
    curveHost.appendChild(heading);
    var note = document.createElement("p");
    note.className = "muted";
    note.textContent = "Return periods run from the largest down. AAL and standard deviation are the last two rows. EXCL and DIFF sit side by side for each selected risk.";
    curveHost.appendChild(note);
    var wrap = document.createElement("div");
    wrap.className = "result-scroll";
    var table = document.createElement("table");
    table.className = "grid nums curve-join";
    var head = table.createTHead();
    var group = head.insertRow();
    var corner = document.createElement("th");
    corner.className = "metric";
    corner.rowSpan = 2;
    corner.textContent = "Metric";
    group.appendChild(corner);
    selected.forEach(function (id) {
      var risk = document.createElement("th");
      risk.colSpan = 2;
      risk.textContent = id;
      group.appendChild(risk);
    });
    var kinds = head.insertRow();
    selected.forEach(function () {
      var excl = document.createElement("th");
      excl.textContent = "EXCL";
      var diff = document.createElement("th");
      diff.textContent = "DIFF";
      kinds.appendChild(excl);
      kinds.appendChild(diff);
    });
    var body = table.createTBody();
    function metricRow(label, field) {
      var row = body.insertRow();
      addCell(row, label, "metric");
      selected.forEach(function (id) {
        var curve = curves[id] || {};
        addCell(row, curve.excl ? curve.excl[field] : "");
        addCell(row, curve.diff ? curve.diff[field] : "");
      });
    }
    var order = periods.map(function (period, index) {
      return { period: period, index: index, rank: Number(period) };
    });
    order.sort(function (left, right) {
      var leftRank = isFinite(left.rank) ? left.rank : -Infinity;
      var rightRank = isFinite(right.rank) ? right.rank : -Infinity;
      return rightRank - leftRank;
    });
    order.forEach(function (item) {
      var row = body.insertRow();
      addCell(row, item.period, "metric");
      selected.forEach(function (id) {
        var curve = curves[id] || {};
        addCell(row, curve.excl && curve.excl.amounts ? curve.excl.amounts[item.index] : "");
        addCell(row, curve.diff && curve.diff.amounts ? curve.diff.amounts[item.index] : "");
      });
    });
    metricRow("AAL", "aal");
    metricRow("Std dev", "std");
    wrap.appendChild(table);
    curveHost.appendChild(wrap);
    var headerHeight = group.getBoundingClientRect().height;
    Array.prototype.forEach.call(kinds.cells, function (cell) {
      cell.style.top = headerHeight + "px";
    });
  }

  function render() {
    renderPicks();
    renderExposure();
    renderCurves();
  }

  input.addEventListener("input", openList);
  input.addEventListener("focus", openList);
  input.addEventListener("keydown", function (event) {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      if (list.hidden) openList();
      moveActive(1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      if (list.hidden) openList();
      moveActive(-1);
    } else if (event.key === "Enter") {
      var options = list.querySelectorAll("[role='option']");
      if (list.hidden || !options.length) return;
      event.preventDefault();
      choose(options[active >= 0 ? active : 0].dataset.id);
    } else if (event.key === "Escape") {
      closeList();
    }
  });
  document.addEventListener("click", function (event) {
    if (!event.target.closest(".risk-combo")) closeList();
  });

  window.DpoRiskCompare = {
    activate: function () {
      indexTables();
      input.focus();
    }
  };
})();
