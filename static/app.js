// Tab switching + EP curve chart (vanilla JS, no dependencies).
(function () {
  // ---- tabs
  var tabs = document.querySelectorAll('.tab');
  tabs.forEach(function (t) {
    t.addEventListener('click', function () {
      tabs.forEach(function (x) { x.classList.remove('active'); });
      document.querySelectorAll('.tabpane').forEach(function (p) { p.classList.remove('active'); });
      t.classList.add('active');
      document.getElementById('pane-' + t.dataset.tab).classList.add('active');
      if (t.dataset.tab === 'chart') drawChart(window.__overlay || null);
    });
  });

  // ---- connect page: toggle SQL login fields
  var authRadios = document.querySelectorAll('input[name=auth]');
  var loginBox = document.getElementById('sqllogin');
  function toggleLogin() {
    if (!loginBox) return;
    var sql = document.querySelector('input[name=auth]:checked').value === 'sql';
    loginBox.style.display = sql ? 'block' : 'none';
  }
  authRadios.forEach(function (r) { r.addEventListener('change', toggleLogin); });
  toggleLogin();

  // ---- EP chart
  var canvas = document.getElementById('epchart');
  if (!canvas || !window.__chart) return;
  var sel = document.getElementById('entitysel');
  if (sel) sel.addEventListener('change', function () {
    var e = sel.value;
    if (!e) { window.__overlay = null; drawChart(null); return; }
    fetch('/api/curve/' + encodeURIComponent(window.__runId) + '?entity=' + encodeURIComponent(e))
      .then(function (r) { return r.json(); })
      .then(function (j) { window.__overlay = j; drawChart(j); });
  });

  function drawChart(overlay) {
    var ctx = canvas.getContext('2d');
    var W = canvas.width, H = canvas.height, pad = { l: 70, r: 20, t: 20, b: 50 };
    ctx.clearRect(0, 0, W, H);
    var base = window.__chart;
    var series = [{ t: base.t, l: base.l, color: '#1f6feb', label: 'Portfolio' }];
    if (overlay && overlay.t) series.push({ t: overlay.t, l: overlay.l, color: '#d97706', label: overlay.label });

    var tMin = 1, tMax = 1200, lMax = 0;
    series.forEach(function (s) { s.l.forEach(function (v) { if (v > lMax) lMax = v; }); });
    lMax = lMax * 1.05 || 1;
    function X(t) { return pad.l + (Math.log(t) - Math.log(tMin)) / (Math.log(tMax) - Math.log(tMin)) * (W - pad.l - pad.r); }
    function Y(v) { return H - pad.b - (v / lMax) * (H - pad.t - pad.b); }

    // gridlines + x labels
    ctx.strokeStyle = '#e6ebf3'; ctx.fillStyle = '#7b8aa0'; ctx.font = '11px sans-serif';
    [1, 5, 10, 25, 50, 100, 250, 500, 1000].forEach(function (t) {
      var x = X(t);
      ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, H - pad.b); ctx.stroke();
      ctx.fillText(t === 1 ? '1' : String(t), x - 8, H - pad.b + 18);
    });
    ctx.fillText('return period (years, log scale)', pad.l, H - 8);
    // y labels
    for (var i = 0; i <= 4; i++) {
      var v = lMax * i / 4, y = Y(v);
      ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke();
      ctx.fillText(fmt(v), 8, y + 4);
    }
    // curves
    series.forEach(function (s) {
      ctx.strokeStyle = s.color; ctx.lineWidth = 2; ctx.beginPath();
      s.t.forEach(function (t, i) { var x = X(t), y = Y(s.l[i]); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
      ctx.stroke();
    });
    // PML point markers (portfolio)
    var pts = window.__points, pml = window.__pml;
    ctx.fillStyle = '#1f6feb';
    pts.forEach(function (p, i) { ctx.beginPath(); ctx.arc(X(p), Y(pml[i]), 3.5, 0, 7); ctx.fill(); });
    // legend
    var lx = W - pad.r - 190, ly = pad.t + 6;
    series.forEach(function (s, i) {
      ctx.fillStyle = s.color; ctx.fillRect(lx, ly + i * 20, 26, 4);
      ctx.fillStyle = '#1c2333'; ctx.fillText(s.label, lx + 32, ly + i * 20 + 5);
    });
  }
  function fmt(v) {
    if (v >= 1e9) return (v / 1e9).toFixed(1) + 'B';
    if (v >= 1e6) return (v / 1e6).toFixed(1) + 'M';
    if (v >= 1e3) return (v / 1e3).toFixed(0) + 'K';
    return v.toFixed(0);
  }
  drawChart(null);
})();
