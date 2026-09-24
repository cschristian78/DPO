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
  var summaryTable = document.getElementById('port-summary-table');
  var summaryBody = summaryTable ? summaryTable.querySelector('tbody') : null;
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
    summaryTable.hidden = true;
    summaryBody.replaceChildren();
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
    summaryTable.hidden = true;
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
        summaryBody.replaceChildren();
        stats.forEach(function (row) {
          var tr = document.createElement('tr');
          var name = document.createElement('td');
          var label = perspLabel[row.perspective];
          name.textContent = label ? row.perspective + ' ' + label : row.perspective;
          var aal = document.createElement('td');
          aal.textContent = money(row.aal);
          var std = document.createElement('td');
          std.textContent = money(row.stdDev);
          tr.append(name, aal, std);
          summaryBody.appendChild(tr);
        });
        summaryStatus.hidden = true;
        summaryTable.hidden = false;
      })
      .catch(function (err) {
        if (token !== summaryToken) return;
        summaryStatus.hidden = false;
        summaryStatus.textContent = err.message;
        summaryTable.hidden = true;
      });
  }

  var search = document.getElementById('rdm-database-search');
  var note = document.getElementById('rdm-filter-note');
  var allNames = [];

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
    if (search && !search.hidden) return search.value.trim();
    return database.value;
  }

  function setNamed(el, on) {
    if (!el) return;
    if (on) el.setAttribute('name', 'rdm_name');
    else el.removeAttribute('name');
  }

  function fillSelect(names, selected) {
    database.hidden = false;
    if (search) search.hidden = true;
    setNamed(database, true);
    setNamed(search, false);
    database.required = true;
    if (search) search.required = false;
    database.innerHTML = '';
    database.add(new Option(names.length ? 'Select an RDM database' : 'No RDM databases found', ''));
    names.forEach(function (name) { database.add(new Option(name, name)); });
    if (selected && names.indexOf(selected) >= 0) database.value = selected;
  }

  function showSearch(selected) {
    database.hidden = true;
    search.hidden = false;
    setNamed(database, false);
    setNamed(search, true);
    database.required = false;
    search.required = true;
    search.value = selected || '';
    var list = document.getElementById('rdm-database-list');
    list.innerHTML = '';
    allNames.forEach(function (name) {
      var opt = document.createElement('option');
      opt.value = name;
      list.appendChild(opt);
    });
  }

  function applyClientFilter(preserve) {
    var short = clientShort();
    if (!short) {
      if (note) {
        note.hidden = false;
        note.textContent = 'Select a client. The client short name filters the RDM list.';
      }
      fillSelect([], '');
      return;
    }
    var matches = allNames.filter(function (name) {
      return dbPrefix(name).toLowerCase() === short.toLowerCase();
    });
    if (matches.length) {
      if (note) { note.hidden = true; note.textContent = ''; }
      fillSelect(matches, preserve);
      if (database.value) loadAnalyses();
    } else {
      if (note) {
        note.hidden = false;
        note.textContent = 'No RDM databases match short name ' + short + '. Type to search all databases.';
      }
      showSearch(allNames.indexOf(preserve) >= 0 ? preserve : '');
      if (search.value && allNames.indexOf(search.value) >= 0) loadAnalyses();
    }
    if (!currentDatabase()) {
      select.innerHTML = '';
      select.add(new Option('Select an RDM database', ''));
      rows = [];
      if (!keep || userPicked) fill(null);
    }
  }

  function fill(row) {
    document.getElementById('edm-name').value = row ? (row.edmName || '') : '';
    document.getElementById('portfolio-id').value = row && row.portfolioId != null ? row.portfolioId : '';
    document.getElementById('peril').value = row ? (row.peril || '') : '';
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
    loadDatabases();
  });
  document.getElementById('client').addEventListener('change', function () {
    userPicked = true;
    initialAnalysisId = '';
    applyClientFilter('');
  });
  database.addEventListener('change', function () {
    userPicked = true;
    initialAnalysisId = '';
    loadAnalyses();
  });
  if (search) {
    search.addEventListener('change', function () {
      userPicked = true;
      initialAnalysisId = '';
      if (allNames.indexOf(search.value.trim()) >= 0) loadAnalyses();
    });
  }
  select.addEventListener('change', function () {
    userPicked = true;
    var row = select.value === '' ? null : rows[Number(select.value)];
    fill(row || null);
  });
  loadDatabases();
})();
