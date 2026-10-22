/**
 * Markets Live — all asset classes, SSE-driven real-time updates.
 * Initial snapshot: GET /api/markets
 * Live ticks:       EventSource /api/stream  (event: "market", field: ticks)
 *
 * How it works (TradingView-style):
 *   1. On page-active: fetch /api/markets to populate every row instantly.
 *   2. Reuse the SHARED conn-mgr SSE connection (already open for heartbeat).
 *   3. On each "market" SSE event: patch only the changed cells in-place.
 *      Zero re-render, zero flash, continuous updates.
 */
;(function () {
  "use strict";

  // ── Format helpers ────────────────────────────────────────────────────────
  var _prec = { forex:6, crypto:2, gold:2, us_stocks:2, india:2, futures:2, other:5 };
  function fmtP(v, market) {
    if (v == null) return "--";
    var d = _prec[market] || 5;
    return Number(v).toLocaleString("en-US", {minimumFractionDigits:d, maximumFractionDigits:d});
  }
  function fmtPct(v) {
    if (v == null) return "--";
    return (v >= 0 ? "+" : "") + Number(v).toFixed(3) + "%";
  }
  function esc(s) { return String(s != null ? s : "").replace(/&/g,"&amp;").replace(/</g,"&lt;"); }
  function cc(v) { return v == null ? "neu" : v > 0 ? "pos" : v < 0 ? "neg" : "neu"; }

  // ── Row registry: symbol -> <tr> element ─────────────────────────────────
  var _rows = {};   // sym -> { tr, market, pip }

  // ── Flash animation on tick update ───────────────────────────────────────
  function flashCell(td, up) {
    td.classList.remove("flash-up", "flash-down");
    void td.offsetWidth;
    td.classList.add(up ? "flash-up" : "flash-down");
    setTimeout(function() { td.classList.remove("flash-up", "flash-down"); }, 700);
  }

  // ── Apply a tick update to an existing row without re-rendering ──────────
  function patchRow(sym, tick) {
    var info = _rows[sym];
    if (!info) return;
    var cells = info.tr.querySelectorAll("td");
    // cells: 0=sym, 1=label, 2=price, 3=change%, 4=bid, 5=ask, 6=spread, 7=vol, 8=src, 9=ts
    var oldPrice = parseFloat(info.tr.dataset.price || 0);
    var newPrice = tick.price;
    var up = newPrice >= oldPrice;

    if (newPrice != null) {
      cells[2].textContent = fmtP(newPrice, info.market);
      cells[2].className = "mkt-price " + cc(tick.change_pct);
      info.tr.dataset.price = newPrice;
      flashCell(cells[2], up);
    }
    if (tick.change_pct != null) {
      cells[3].textContent = fmtPct(tick.change_pct);
      cells[3].className = "mkt-chg " + cc(tick.change_pct);
    }
    if (tick.bid != null) cells[4].textContent = fmtP(tick.bid, info.market);
    if (tick.ask != null) cells[5].textContent = fmtP(tick.ask, info.market);
    // Spread
    if (tick.bid != null && tick.ask != null) {
      var spread = ((tick.ask - tick.bid) / (info.pip || 0.0001)).toFixed(1);
      cells[6].textContent = spread;
    }
    cells[9].textContent = new Date().toLocaleTimeString();
  }

  // ── Build a row from snapshot data ───────────────────────────────────────
  function buildRow(d, market) {
    var tr = document.createElement("tr");
    tr.id = "mkt-row-" + d.symbol.replace(/[^a-zA-Z0-9]/g, "_");
    tr.dataset.symbol = d.symbol;
    tr.dataset.price  = d.price || 0;
    tr.dataset.market = market;
    tr.dataset.label  = (d.label || "").toLowerCase();
    tr.dataset.aclass = (d.asset_class || "").toLowerCase();

    var spread = (d.bid != null && d.ask != null && d.pip_size)
      ? ((d.ask - d.bid) / d.pip_size).toFixed(1)
      : "--";

    var availClass = d.available ? "" : "mkt-unavail";
    var priceText  = d.available ? fmtP(d.price, market) : "DATA UNAVAILABLE";

    tr.innerHTML =
      "<td><span class=\"badge badge-outline\" style=\"font-size:10px;font-family:monospace\">" + esc(d.symbol) + "</span></td>" +
      "<td style=\"font-size:12px\">" + esc(d.label) + "</td>" +
      "<td class=\"mkt-price " + cc(d.change_pct) + " " + availClass + "\">" + priceText + "</td>" +
      "<td class=\"mkt-chg "  + cc(d.change_pct) + "\">" + fmtPct(d.change_pct) + "</td>" +
      "<td>" + (d.bid != null ? fmtP(d.bid, market) : "--") + "</td>" +
      "<td>" + (d.ask != null ? fmtP(d.ask, market) : "--") + "</td>" +
      "<td style=\"font-size:11px\">" + spread + "</td>" +
      "<td style=\"font-size:11px\">" + (d.volume != null ? Number(d.volume).toLocaleString() : "--") + "</td>" +
      "<td style=\"font-size:10px;color:var(--text-muted)\">" + esc(d.source || "--") + "</td>" +
      "<td style=\"font-size:10px;color:var(--text-muted)\">" + new Date().toLocaleTimeString() + "</td>";

    _rows[d.symbol] = { tr: tr, market: market, pip: d.pip_size || 0.0001 };
    return tr;
  }

  // ── Populate a tab table from snapshot ───────────────────────────────────
  function populateTab(tabId, rows, market) {
    var tb = document.querySelector("#" + tabId + " tbody");
    if (!tb) return;
    if (!rows || !rows.length) {
      tb.innerHTML = "<tr><td colspan=\"10\" class=\"table-empty\">No data available for this market</td></tr>";
      return;
    }
    tb.innerHTML = "";
    rows.forEach(function(d) {
      tb.appendChild(buildRow(d, market));
    });
  }

  // ── Filter rows in a tab ──────────────────────────────────────────────────
  function filterTab(inputEl, tabId) {
    var q = (inputEl.value || "").toLowerCase().trim();
    var rows = document.querySelectorAll("#" + tabId + " tbody tr");
    rows.forEach(function(tr) {
      if (!q) { tr.style.display = ""; return; }
      var sym   = (tr.dataset.symbol || "").toLowerCase();
      var label = (tr.dataset.label  || "").toLowerCase();
      var acls  = (tr.dataset.aclass || "").toLowerCase();
      tr.style.display = (sym.includes(q) || label.includes(q) || acls.includes(q)) ? "" : "none";
    });
  }

  // ── Initial snapshot fetch ────────────────────────────────────────────────
  var _loaded = false;
  async function loadSnapshot() {
    var updated = document.getElementById("mkt-last-updated");
    try {
      var res  = await fetch("/api/markets");
      if (!res.ok) throw new Error("HTTP " + res.status);
      var data = await res.json();
      var g    = data.groups || {};

      populateTab("mkt-tab-forex",     g.forex     || [], "forex");
      populateTab("mkt-tab-crypto",    g.crypto    || [], "crypto");
      populateTab("mkt-tab-gold",      g.gold      || [], "gold");
      populateTab("mkt-tab-us",        g.us_stocks || [], "us_stocks");
      populateTab("mkt-tab-india",     g.india     || [], "india");
      populateTab("mkt-tab-futures",   g.futures   || [], "futures");

      // "All" tab — merge everything
      var allTb = document.querySelector("#mkt-tab-all tbody");
      if (allTb) {
        allTb.innerHTML = "";
        var all = [].concat(g.forex||[], g.crypto||[], g.gold||[], g.us_stocks||[], g.india||[], g.futures||[], g.other||[]);
        all.forEach(function(d) { allTb.appendChild(buildRow(d, d.market || "other")); });
      }

      if (updated) updated.textContent = "Loaded: " + new Date().toLocaleTimeString();
      _loaded = true;
    } catch(e) {
      console.error("[Markets]", e);
      if (updated) updated.textContent = "Load failed: " + e.message;
    }
  }

  // ── Subscribe to SSE for live ticks ──────────────────────────────────────
  // Reuses conn-mgr EventSource if available, otherwise opens own.
  function subscribeSSE() {
    // Try to hook into conn-mgr's event bus
    if (window.connMgr && typeof window.connMgr.on === "function") {
      window.connMgr.on("market", function(data) {
        var ticks = data.ticks || data.prices || {};
        Object.keys(ticks).forEach(function(sym) {
          var t = ticks[sym];
          // Normalise: old format was a bare price number
          if (typeof t === "number") { t = { price: t }; }
          patchRow(sym, t);
        });
      });
      return;
    }

    // Fallback: own EventSource
    var es = new EventSource("/api/stream");
    es.addEventListener("market", function(e) {
      try {
        var data  = JSON.parse(e.data);
        var ticks = data.ticks || data.prices || {};
        Object.keys(ticks).forEach(function(sym) {
          var t = ticks[sym];
          if (typeof t === "number") { t = { price: t }; }
          patchRow(sym, t);
        });
      } catch(_) {}
    });
    es.onerror = function() { /* silent — will reconnect */ };
    window._mktSSE = es;
  }

  // ── Tab switching ─────────────────────────────────────────────────────────
  function switchTab(tabId) {
    document.querySelectorAll(".mkt-tab-btn").forEach(function(b) {
      b.classList.toggle("active", b.dataset.tab === tabId);
    });
    document.querySelectorAll(".mkt-tab-pane").forEach(function(p) {
      p.style.display = p.id === tabId ? "" : "none";
    });
  }

  // ── Init ──────────────────────────────────────────────────────────────────
  var _active = false;
  function activate() {
    if (_active) return;
    _active = true;
    if (!_loaded) loadSnapshot();
    subscribeSSE();
  }
  function deactivate() {
    _active = false;
    if (window._mktSSE) { window._mktSSE.close(); window._mktSSE = null; }
  }

  document.addEventListener("DOMContentLoaded", function() {
    var pg = document.getElementById("page-markets");
    if (!pg) return;

    new MutationObserver(function() {
      pg.classList.contains("active") ? activate() : deactivate();
    }).observe(pg, { attributes: true, attributeFilter: ["class"] });

    if (pg.classList.contains("active")) activate();
  });

  // ── Expose globals ────────────────────────────────────────────────────────
  window.marketsPage = {
    reload: function() { _loaded = false; _rows = {}; loadSnapshot(); },
    switchTab: switchTab,
    filterTab: filterTab,
  };
})();
