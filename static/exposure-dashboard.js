(function () {
  var select = document.getElementById("exposure-top");
  var dashboard = document.getElementById("exposure-dashboard");
  if (!select || !dashboard) return;

  function formatCount(value) {
    return Math.round(value).toLocaleString("en-US");
  }

  function formatShare(part, whole) {
    if (!whole) return "0.0%";
    return (part / whole * 100).toFixed(1) + "%";
  }

  function formatAverage(value, count) {
    if (!count) return "—";
    return (value / count).toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  }

  function writeRow(row, count, value, totalCount, totalValue) {
    row.querySelector(".risks").textContent = formatCount(count);
    row.querySelector(".risk-share").textContent = formatShare(count, totalCount);
    row.querySelector(".tiv").textContent = formatCount(value);
    row.querySelector(".tiv-share").textContent = formatShare(value, totalValue);
    row.querySelector(".average").textContent = formatAverage(value, count);
  }

  function applyTop() {
    var limit = Number(select.value);
    if (limit !== 5 && limit !== 10 && limit !== 25) limit = 10;
    dashboard.querySelectorAll(".exposure-chart[data-mode='top']").forEach(function (chart) {
      var total = chart.querySelector("tfoot tr");
      var totalCount = Number(total.dataset.totalCount);
      var totalValue = Number(total.dataset.totalValue);
      var other = chart.querySelector(".exposure-other");
      var extraCount = other ? Number(other.dataset.tailCount) : 0;
      var extraValue = other ? Number(other.dataset.tailValue) : 0;
      chart.querySelectorAll("[data-top-rank]").forEach(function (item) {
        var keep = Number(item.dataset.topRank) <= limit;
        item.hidden = !keep;
        if (!keep) {
          extraCount += Number(item.dataset.count);
          extraValue += Number(item.dataset.value);
        }
      });
      if (!other) return;
      other.hidden = extraCount === 0 && extraValue === 0;
      if (!other.hidden) writeRow(other, extraCount, extraValue, totalCount, totalValue);
    });
  }

  select.addEventListener("change", applyTop);
  applyTop();
  window.DpoExposureDashboard = { activate: applyTop };
})();
