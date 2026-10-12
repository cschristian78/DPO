(function () {
  var form = document.getElementById('new-analysis');
  if (!form) return;
  var refreshBtn = document.getElementById('refresh-rdm');
  var database = document.getElementById('rdm-database');
  var select = document.getElementById('rdm-analysis');
  var rows = [];
  var keep = form.dataset.keep === '1';
  var initialAnalysisId = form.dataset.analysisId || '';
  var userPicked = false;

  function readJson(r) {
    return r.json().then(function (j) { return { ok: r.ok, body: j }; });
  }

  var summaryStatus = document.getElementById('port-summary-status');
  var summaryLine = document.getElementById('port-summary-line');
  var summaryToken = 0;
  var perspOrder = ['GU', 'GR', 'RL', 'RP', 'RC', 'CL'];
  var perspLabel = {
    GU: 'Ground up', GR: 'Gross', RL: 'Retained',
    RP: 'Post-CAT', RC: 'Ceded', CL: 'Client'
  };

  function resetSummary(message) {
    summaryToken += 1;
    if (!summaryStatus) return;
    summaryStatus.hidden = false;
    summaryStatus.textContent = message;
    if (summaryLine) {
      summaryLine.hidden = true;
      summaryLine.replaceChildren();
    }
  }

  function money(n) {
    if (n === null || n === undefined || n < 0) return '\u2014';
    return n.toLocaleString(undefined, { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });
  }

  function loadSummary(db, analysisId) {
    if (!summaryStatus) return;
    var token = summaryToken += 1;
    summaryStatus.hidden = false;
    summaryStatus.textContent = 'Loading portfolio summary';
    summaryLine.hidden = true;
    fetch('/api/rdm-portstats?database=' + encodeURIComponent(db) + '&analysis=' + encodeURIComponent(analysisId))
      .then(readJson)
      .then(function (res) {
        if (token !== summaryToken) return;
        if (!res.ok) throw new Error(res.body.error || 'Could not load portfolio summary');
        var stats = (res.body.stats || []).slice();
        if (!stats.length) {
          summaryStatus.textContent = 'No portfolio stats for this analysis.';
          return;
        }
        stats.sort(function (a, b) {
          var ia = perspOrder.indexOf(a.perspective);
          var ib = perspOrder.indexOf(b.perspective);
          if (ia < 0) ia = 100;
          if (ib < 0) ib = 100;
          if (ia !== ib) return ia - ib;
          return String(a.perspective).localeCompare(String(b.perspective));
        });
        summaryLine.replaceChildren();
        stats.forEach(function (row) {
          var item = document.createElement('span');
          item.className = 'summary-item';
          var name = document.createElement('span');
          name.className = 'summary-persp';
          var label = perspLabel[row.perspective];
          name.textContent = label ? row.perspective + ' ' + label : row.perspective;
          var aal = document.createElement('span');
          aal.textContent = money(row.aal);
          var std = document.createElement('span');
          std.className = 'summary-std';
          std.textContent = money(row.stdDev);
          item.append(name, aal, std);
          summaryLine.appendChild(item);
        });
        summaryStatus.hidden = true;
        summaryLine.hidden = false;
      })
      .catch(function (err) {
        if (token !== summaryToken) return;
        summaryStatus.hidden = false;
        summaryStatus.textContent = err.message;
        summaryLine.hidden = true;
      });
  }

  var note = document.getElementById('rdm-filter-note');
  var allNames = [];
  var showAll = false;

  function dbPrefix(name) {
    var pos = String(name).indexOf('_');
    return pos > 0 ? name.slice(0, pos) : '';
  }

  function clientShort() {
    var client = document.getElementById('client');
    var opt = client.options[client.selectedIndex];
    return opt && opt.dataset.short ? opt.dataset.short : '';
  }

  function currentDatabase() {
    return database.value;
  }

  function fillSelect(names, selected) {
    database.innerHTML = '';
    database.add(new Option(names.length ? 'Select an RDM database' : 'No RDM databases found', ''));
    names.forEach(function (name) { database.add(new Option(name, name)); });
    if (selected && names.indexOf(selected) >= 0) database.value = selected;
  }

  function applyClientFilter(preserve) {
    var short = clientShort();
    if (!showAll && !short) {
      if (note) {
        note.hidden = false;
        note.textContent = 'Select a client. The client short name filters the RDM list.';
      }
      fillSelect([], '');
      return;
    }
    var matches = showAll ? allNames.slice() : allNames.filter(function (name) {
      return dbPrefix(name).toLowerCase() === short.toLowerCase();
    });
    if (matches.length) {
      if (note) {
        note.hidden = !showAll;
        note.textContent = showAll ? 'Showing all RDM databases.' : '';
        if (showAll) note.hidden = false;
      }
      fillSelect(matches, preserve);
      if (database.value) loadAnalyses();
    } else {
      if (note) {
        note.hidden = false;
        note.textContent = 'No RDM databases match short name ' + short + '. Choose Show All DB to list every RDM.';
      }
      fillSelect([], '');
    }
    if (!currentDatabase()) {
      select.innerHTML = '';
      select.add(new Option('Select an RDM database', ''));
      rows = [];
      if (!keep || userPicked) fill(null);
    }
  }

  var edmSelect = document.getElementById('edm-name');
  var edmNames = [];
  var pendingEdm = edmSelect ? edmSelect.value : '';

  function matchEdm(logical) {
    if (!logical) return '';
    var wanted = String(logical);
    if (edmNames.indexOf(wanted) >= 0) return wanted;
    var prefix = wanted.toLowerCase() + '_';
    var matches = edmNames.filter(function (name) {
      return name.toLowerCase().indexOf(prefix) === 0;
    }).sort();
    return matches.length ? matches[0] : '';
  }

  function fillEdmSelect(selected) {
    if (!edmSelect) return;
    edmSelect.innerHTML = '';
    edmSelect.add(new Option(edmNames.length ? 'Select an EDM database' : 'No EDM databases found', ''));
    edmNames.forEach(function (name) { edmSelect.add(new Option(name, name)); });
    var chosen = matchEdm(selected) || (edmNames.indexOf(selected) >= 0 ? selected : '');
    if (chosen) edmSelect.value = chosen;
    else if (selected) {
      edmSelect.add(new Option(selected, selected));
      edmSelect.value = selected;
    }
  }

  function selectEdm(logical) {
    pendingEdm = logical || '';
    if (!edmSelect) return;
    if (!edmNames.length) return;
    var chosen = matchEdm(pendingEdm);
    edmSelect.value = chosen || '';
  }

  function loadEdmDatabases() {
    if (!edmSelect) return;
    fetch('/api/edm-databases')
      .then(readJson)
      .then(function (res) {
        if (!res.ok) throw new Error(res.body.error || 'Could not load EDM databases');
        edmNames = res.body.databases || [];
        var saved = keep && !userPicked ? pendingEdm : '';
        fillEdmSelect(saved || pendingEdm);
        if (!(keep && !userPicked) && pendingEdm) selectEdm(pendingEdm);
      })
      .catch(function (err) {
        edmSelect.innerHTML = '';
        edmSelect.add(new Option(err.message, ''));
      });
  }

  function fill(row) {
    var logical = row ? (row.edmName || '') : '';
    if (keep && !userPicked && edmSelect && edmSelect.value) logical = edmSelect.value;
    selectEdm(logical);
    document.getElementById('portfolio-id').value = row && row.portfolioId != null ? row.portfolioId : '';
    document.getElementById('peril').value = row ? (row.peril || '') : '';
    document.getElementById('engine-version').value = row ? (row.engineVersion || '') : '';
    document.getElementById('rdm-analysis-id').value = row ? row.id : '';
    document.getElementById('rdm-analysis-name').value = row ? (row.name || '') : '';
    if (row) loadSummary(currentDatabase(), row.id);
    else resetSummary('Select an RDM analysis.');
  }

  function loadAnalyses() {
    var db = currentDatabase();
    select.innerHTML = '';
    rows = [];
    if (!keep || userPicked) fill(null);
    if (!db) {
      select.add(new Option('Select an RDM database', ''));
      return;
    }
    select.add(new Option('Loading analyses', ''));
    fetch('/api/rdm-analyses?database=' + encodeURIComponent(db))
      .then(readJson)
      .then(function (res) {
        select.innerHTML = '';
        if (!res.ok) throw new Error(res.body.error || 'Could not load analyses');
        rows = res.body.analyses || [];
        select.add(new Option(rows.length ? 'Select an analysis' : 'No analyses found', ''));
        var selected = '';
        rows.forEach(function (row, i) {
          select.add(new Option(row.name || ('Analysis ' + row.id), String(i)));
          if (String(row.id) === String(initialAnalysisId)) selected = String(i);
        });
        if (selected !== '' && !userPicked) {
          select.value = selected;
          fill(rows[Number(selected)]);
        }
      })
      .catch(function (err) {
        select.innerHTML = '';
        select.add(new Option(err.message, ''));
      });
  }

  function loadDatabases() {
    var current = currentDatabase();
    refreshBtn.disabled = true;
    fetch('/api/rdm-databases')
      .then(readJson)
      .then(function (res) {
        if (!res.ok) throw new Error(res.body.error || 'Could not load RDM databases');
        allNames = res.body.databases || [];
        applyClientFilter(current);
      })
      .catch(function (err) {
        database.innerHTML = '';
        database.add(new Option(err.message, ''));
        if (note) {
          note.hidden = false;
          note.textContent = err.message;
        }
      })
      .then(function () { refreshBtn.disabled = false; });
  }

  refreshBtn.addEventListener('click', function () {
    userPicked = true;
    showAll = true;
    var current = database.value;
    function revealAll() {
      fillSelect(allNames, current);
      if (note) {
        note.hidden = false;
        note.textContent = 'Showing all RDM databases.';
      }
      if (database.value) loadAnalyses();
      else {
        select.innerHTML = '';
        select.add(new Option('Select an RDM database', ''));
        rows = [];
        fill(null);
      }
    }
    if (allNames.length) revealAll();
    else loadDatabases();
  });
  document.getElementById('client').addEventListener('change', function () {
    userPicked = true;
    showAll = false;
    initialAnalysisId = '';
    applyClientFilter('');
  });
  database.addEventListener('change', function () {
    userPicked = true;
    initialAnalysisId = '';
    loadAnalyses();
  });
  select.addEventListener('change', function () {
    userPicked = true;
    var row = select.value === '' ? null : rows[Number(select.value)];
    fill(row || null);
  });
  loadDatabases();
  loadEdmDatabases();
})();
