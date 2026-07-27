(function () {
  const root = document.querySelector("[data-live-url]");
  if (!root) return;
  const url = root.getAttribute("data-live-url");
  const configuredInterval = Number(root.getAttribute("data-live-interval"));
  const INTERVAL = Number.isFinite(configuredInterval) && configuredInterval >= 5000
    ? configuredInterval
    : 20000;

  function replaceHtml(selector, html) {
    if (typeof html !== "string") return;
    const element = document.querySelector(selector);
    if (element) element.innerHTML = html;
  }

  function applyDirectorReport(data) {
    if (data.director_metrics) {
      Object.keys(data.director_metrics).forEach(function (key) {
        const element = document.querySelector('[data-director-metric="' + key + '"]');
        if (element) element.textContent = data.director_metrics[key];
      });
    }
    if (typeof data.latest_audit_url === "string") {
      document.querySelectorAll("[data-director-latest-audit-link]").forEach(function (link) {
        link.setAttribute("href", data.latest_audit_url);
      });
    }
    if (data.reconciliation) {
      const batchCount = document.querySelector("[data-director-reconciliation-batch-count]");
      if (batchCount) {
        const count = data.reconciliation.audit_batch_count;
        batchCount.textContent = count + " audit batch" + (count === 1 ? "" : "es");
      }
      Object.keys(data.reconciliation).forEach(function (key) {
        const element = document.querySelector(
          '[data-director-reconciliation-metric="' + key + '"]'
        );
        if (element) element.textContent = data.reconciliation[key];
      });
    }
    replaceHtml("[data-director-live-product-rows]", data.product_rows_html);
    replaceHtml("[data-director-live-warehouse-rows]", data.warehouse_rows_html);
    replaceHtml("[data-director-live-audit-batches]", data.audit_batches_html);
    replaceHtml("[data-director-live-expiry-risk]", data.expiry_risk_html);
    replaceHtml("[data-director-live-dead-stock]", data.dead_stock_html);
  }

  function apply(data) {
    applyDirectorReport(data);
    if (data.counts) {
      Object.keys(data.counts).forEach(function (key) {
        const el = document.querySelector('[data-metric="' + key + '"]');
        if (el) el.textContent = data.counts[key];
      });
    }
    if (typeof data.charts_html === "string") {
      const charts = document.querySelector("[data-live-charts]");
      if (charts) charts.outerHTML = data.charts_html;
    }
    if (typeof data.expiry_html === "string") {
      const expiry = document.querySelector("[data-live-expiry]");
      if (expiry) expiry.outerHTML = data.expiry_html;
    }
    if (typeof data.batches_html === "string") {
      const tbody = document.querySelector("[data-live-batches]");
      if (tbody) tbody.innerHTML = data.batches_html;
    }
    if (typeof data.scans_html === "string") {
      const tbody = document.querySelector("[data-live-scans]");
      if (tbody) tbody.innerHTML = data.scans_html;
    }
    if (typeof data.shelf_alerts_html === "string") {
      const alerts = document.querySelector("[data-live-shelf-alerts]");
      if (alerts) alerts.outerHTML = data.shelf_alerts_html;
    }
    replaceHtml("[data-dashboard-product-stock-rows]", data.product_stock_rows_html);
  }

  function tick() {
    if (document.hidden) return;
    fetch(url, {
      headers: { Accept: "application/json", "X-Setuora-Background": "true" },
    })
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .then(function (d) {
        if (d) apply(d);
      })
      .catch(function () {});
  }

  setInterval(tick, INTERVAL);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) tick();
  });
})();
