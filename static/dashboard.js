// Dashboard chart rendering. Expects window.DASHBOARD_CHARTS to already be
// set (by a small inline <script> in dashboard.html, since that's the one
// piece that actually comes from the server on each page load).
document.addEventListener("DOMContentLoaded", function () {
  // Colors sampled/approximated from Drizzl's cans: aqua Yuzu, orange,
  // passionfruit purple, mixed-berry pink, lemon yellow, and leaf green.
  var PALETTE = ["#56c9c4", "#f28a3b", "#a986cf", "#e878ad", "#efd454", "#35a46f"];
  var gridColor = "rgba(41, 159, 153, 0.13)";
  var textColor = "#667473";
  function colorForLabel(label, index) {
    var value = String(label || "").toLowerCase();
    if (value.includes("orange")) return "#f28a3b";
    if (value.includes("yuzu")) return "#56c9c4";
    if (value.includes("passionfruit")) return "#a986cf";
    if (value.includes("berry")) return "#e878ad";
    if (value.includes("lemon")) return "#efd454";
    if (value.includes("damaged")) return "#e878ad";
    if (value.includes("short")) return "#f28a3b";
    if (value.includes("wrong")) return "#a986cf";
    return PALETTE[index % PALETTE.length];
  }
  Chart.defaults.color = textColor;
  Chart.defaults.borderColor = gridColor;

  // Each chart's own canvas only exists in the DOM when the template's
  // conditional above it found data -- getElementById returning null just
  // means "nothing to chart yet," skip it rather than error.
  function barOrLineChart(canvasId, type, labels, datasets, opts) {
    var el = document.getElementById(canvasId);
    if (!el) return;
    opts = opts || {};
    new Chart(el, {
      type: type,
      data: {
        labels: labels,
        datasets: datasets.map(function (d, i) {
          return Object.assign({
            backgroundColor: type === "line" ? "transparent" : (d.colors || colorForLabel(d.label, i)),
            borderColor: colorForLabel(d.label, i),
            borderWidth: type === "line" ? 2 : 1,
            tension: 0.25,
          }, d);
        }),
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        scales: {
          x: {
            stacked: !!opts.stacked,
            grid: { display: false },
            title: { display: !!opts.xTitle, text: opts.xTitle },
          },
          y: {
            stacked: !!opts.stacked,
            beginAtZero: true,
            grid: { color: gridColor },
            title: { display: !!opts.yTitle, text: opts.yTitle },
          },
        },
        plugins: {
          legend: { display: datasets.length > 1 },
          title: { display: !!opts.title, text: opts.title, font: { size: 14 } },
        },
      },
    });
  }

  // Pie charts don't use cartesian x/y scales and need one color per
  // slice (not per dataset), so this is a separate function rather than
  // another branch bolted onto barOrLineChart.
  function pieChart(canvasId, labels, data, opts) {
    var el = document.getElementById(canvasId);
    if (!el) return;
    opts = opts || {};
    new Chart(el, {
      type: "pie",
      data: {
        labels: labels,
        datasets: [{
          data: data,
          backgroundColor: labels.map(function (label, i) { return colorForLabel(label, i); }),
          borderColor: "#fff",
          borderWidth: 1,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: true, position: "right" },
          title: { display: !!opts.title, text: opts.title, font: { size: 14 } },
        },
      },
    });
  }

  var charts = window.DASHBOARD_CHARTS;

  barOrLineChart(
    "stockChart", "bar",
    charts.stock.labels,
    charts.stock.datasets.map(function (d) { return { label: d.label, data: d.data }; }),
    { title: "Stock by location", xTitle: "Location", yTitle: "Qty on hand", stacked: true }
  );

  barOrLineChart(
    "poByFacilityChart", "bar",
    charts.po_by_facility.labels,
    [{ label: "Cumulative ordered", data: charts.po_by_facility.data }],
    { title: "Cumulative PO Quantity by Warehouse (All Orders to Date)", xTitle: "Facility", yTitle: "Total quantity ordered" }
  );

  barOrLineChart(
    "damageTrendChart", "line",
    charts.damage_trend.labels,
    [{ label: "Damaged units", data: charts.damage_trend.data }],
    { title: "Damage trend over time", xTitle: "Date", yTitle: "Units damaged" }
  );

  barOrLineChart(
    "damageCauseChart", "bar",
    charts.damage_cause.labels,
    [{ label: "Loss units", data: charts.damage_cause.data, colors: charts.damage_cause.labels.map(colorForLabel) }],
    { title: "Damaged units by cause", xTitle: "Cause", yTitle: "Units damaged" }
  );

  pieChart(
    "flavorPopularityChart",
    charts.flavor_popularity.labels,
    charts.flavor_popularity.data,
    { title: "Flavor popularity (share of total units ordered)" }
  );
});
