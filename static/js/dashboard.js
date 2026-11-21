// Dashboard charts (Chart.js). Each <canvas data-chart="ID"> reads its data from the
// <script type="application/json" id="ID"> next to it, written by the server:
//   {type: "bar" | "line", stacked?, horizontal?, integer?, labels: [...], datasets: [{label, data, color, line?, bar?, right_axis?}]}
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
      open: themeColor("--color-error", "#dc2626"),
      waiting: themeColor("--color-warning", "#d97706"),
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
      x: { stacked: !!spec.stacked, beginAtZero: true, ticks: { color: colors.text } },
      // integer = counts (queries, actions): ticks 0, 1, 2 … not 0.5
      y: { stacked: !!spec.stacked, beginAtZero: true, ticks: { color: colors.text, precision: spec.integer ? 0 : undefined } },
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
        indexAxis: spec.horizontal ? "y" : "x", // horizontal = one bar per row, e.g. per Branch
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

  // htmx skips 4xx replies by default. Inside a ▸ drilldown box the reply is a short,
  // friendly message (e.g. "This Branch is not in your filters"), so show it there
  // instead of leaving "Loading…" on screen.
  document.body.addEventListener("htmx:beforeSwap", function (event) {
    var status = event.detail.xhr.status;
    if (status >= 400 && status < 500 && event.detail.target.hasAttribute("data-drilldown")) {
      event.detail.shouldSwap = true;
      event.detail.isError = false;
    }
  });

  drawCharts(document);
  document.body.addEventListener("htmx:afterSettle", function (event) {
    drawCharts(event.target);
  });

  // Item filter (dashboard_item_picker.html): the picked items, as [{id, label}].
  // `sourceId` is the <script type="application/json"> holding the items picked so far.
  function itemPicker(sourceId) {
    var source = document.getElementById(sourceId);
    return {
      picked: source ? JSON.parse(source.textContent) : [],
      has: function (id) {
        return this.picked.some(function (item) { return item.id === String(id); });
      },
      toggle: function (id, label) {
        if (this.has(id)) this.remove(id);
        else this.picked.push({ id: String(id), label: label });
      },
      remove: function (id) {
        this.picked = this.picked.filter(function (item) { return item.id !== String(id); });
      },
      clear: function () {
        this.picked = [];
      },
    };
  }
  // This script runs before Alpine starts (Alpine is deferred), but register safely either way.
  if (window.Alpine) window.Alpine.data("itemPicker", itemPicker);
  else document.addEventListener("alpine:init", function () { window.Alpine.data("itemPicker", itemPicker); });
})();
