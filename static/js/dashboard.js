// Dashboard charts (Chart.js). Each <canvas data-chart="ID"> reads its data from the
// <script type="application/json" id="ID"> next to it, written by the server:
//   {type: "bar" | "line", stacked?, labels: [...], datasets: [{label, data, color, line?, bar?, right_axis?}]}
// Charts are drawn on page load and again after every htmx swap (filters / tabs).
(function () {
  "use strict";

  // Colours follow the DaisyUI theme, so charts look right in light and dark mode.
  function themeColor(name, fallback) {
    var value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || fallback;
  }

  function palette() {
    return {
      excess: themeColor("--color-success", "#16a34a"),
      shortage: themeColor("--color-error", "#dc2626"),
      net: themeColor("--color-primary", "#2563eb"),
      queries: themeColor("--color-warning", "#d97706"),
      age0: themeColor("--color-success", "#16a34a"),
      age1: themeColor("--color-info", "#0891b2"),
      age2: themeColor("--color-warning", "#d97706"),
      age3: "#ea580c",
      age4: themeColor("--color-error", "#dc2626"),
      text: themeColor("--color-base-content", "#1f2937"),
    };
  }

  function buildConfig(spec) {
    var colors = palette();
    var hasRightAxis = spec.datasets.some(function (d) { return d.right_axis; });
    var datasets = spec.datasets.map(function (d) {
      var color = colors[d.color] || colors.net;
      var type = d.line ? "line" : d.bar ? "bar" : spec.type;
      return {
        type: type,
        label: d.label,
        data: d.data,
        backgroundColor: color,
        borderColor: color,
        borderWidth: type === "line" ? 2 : 0,
        pointRadius: type === "line" ? 3 : 0,
        tension: 0.2,
        maxBarThickness: 48, // one audit = one slim bar, not a full-width block
        yAxisID: d.right_axis ? "y1" : "y",
        order: type === "line" ? 0 : 1, // lines drawn on top of bars
      };
    });
    var scales = {
      x: { stacked: !!spec.stacked, ticks: { color: colors.text } },
      y: { stacked: !!spec.stacked, beginAtZero: true, ticks: { color: colors.text } },
    };
    if (hasRightAxis) {
      scales.y1 = {
        position: "right",
        beginAtZero: true,
        grid: { drawOnChartArea: false },
        ticks: { color: colors.text, precision: 0 },
        title: { display: true, text: "Queries", color: colors.text },
      };
    }
    return {
      type: spec.type,
      data: { labels: spec.labels, datasets: datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: { legend: { labels: { color: colors.text } } },
        scales: scales,
      },
    };
  }

  function drawCharts(root) {
    if (typeof Chart === "undefined") return; // CDN not reachable: the tables still show the numbers
    root.querySelectorAll("canvas[data-chart]").forEach(function (canvas) {
      var source = document.getElementById(canvas.dataset.chart);
      if (!source) return;
      var existing = Chart.getChart(canvas);
      if (existing) existing.destroy();
      new Chart(canvas, buildConfig(JSON.parse(source.textContent)));
    });
  }

  drawCharts(document);
  document.body.addEventListener("htmx:afterSettle", function (event) {
    drawCharts(event.target);
  });
})();
