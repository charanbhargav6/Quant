/**
 * Options Page — Phase 8
 * Polls /api/options every 60s (NSE chain is heavy).
 * Shows: NIFTY + BANKNIFTY option chain with B-S Greeks,
 *        Portfolio Greeks summary, open options positions.
 * Engine makes all trading decisions — dashboard only monitors.
 */
;(function () {
  "use strict";

  var fmt  = function(v, d) { d = d != null ? d : 2; return v != null ? Number(v).toLocaleString("en-IN",{minimumFractionDigits:d,maximumFractionDigits:d}) : "--"; };
  var fmtV = function(v) { return v != null ? Number(v).toLocaleString("en-IN") : "--"; };
  var esc  = function(s) { return String(s != null ? s : "").replace(/&/g,"&amp;").replace(/</g,"&lt;"); };
  var cc   = function(v) { return v == null ? "" : v > 0 ? "pos" : v < 0 ? "neg" : ""; };
  var sign = function(v, d) { if (v == null) return "--"; d = d != null ? d : 4; return (v > 0 ? "+" : "") + Number(v).toFixed(d); };

  // -- Portfolio Greeks header cards -----------------------------------------
  function renderPortfolioGreeks(pg) {
    var el = document.getElementById("opts-portfolio-greeks");
    if (!el) return;
    if (!pg || !pg.available) {
      el.innerHTML = "<div class=\"india-na-block\">Portfolio Greeks not available — no open options positions tracked by engine</div>";
      return;
    }
    function gCard(label, val, color) {
      return "<div class=\"opts-greek-card\"><div class=\"opts-greek-label\">" + esc(label) + "</div>" +
        "<div class=\"opts-greek-val\" style=\"color:" + color + "\">" + sign(val) + "</div></div>";
    }
    el.innerHTML =
      "<div class=\"opts-greek-grid\">" +
      gCard("Portfolio Delta", pg.total_delta, pg.total_delta >= 0 ? "var(--success)" : "var(--danger)") +
      gCard("Portfolio Gamma", pg.total_gamma, "var(--text-primary)") +
      gCard("Daily Theta",     pg.daily_theta, pg.daily_theta <= 0 ? "var(--danger)"  : "var(--success)") +
      gCard("Portfolio Vega",  pg.total_vega,  "var(--text-primary)") +
      "<div class=\"opts-greek-card\"><div class=\"opts-greek-label\">Positions</div><div class=\"opts-greek-val\">" + (pg.positions || 0) + "</div></div>" +
      "</div>" +
      "<div style=\"font-size:10px;color:var(--text-muted);margin-top:8px;\">Source: engine greeks_monitor.py &middot; Updated every 15 min during market hours</div>";
  }

  // -- Chain summary bar (PCR, OI) ------------------------------------------
  function renderChainSummary(chain, elId) {
    var el = document.getElementById(elId);
    if (!el || !chain || !chain.available) {
      if (el) el.innerHTML = "";
      return;
    }
    var pcrCls = chain.pcr_signal === "BEARISH" ? "neg" : chain.pcr_signal === "BULLISH" ? "pos" : "";
    el.innerHTML =
      "<div class=\"opts-chain-summary\">" +
      "<span>Spot: <strong>" + fmt(chain.spot) + "</strong></span>" +
      "<span>Expiry: <strong>" + esc(chain.expiry || "--") + "</strong></span>" +
      "<span>DTE: <strong>" + (chain.dte != null ? chain.dte : "--") + "</strong></span>" +
      "<span>PCR: <strong class=\"" + pcrCls + "\">" + fmt(chain.pcr, 3) + "</strong> <span class=\"opts-pcr-badge " + esc(chain.pcr_signal) + "\">" + esc(chain.pcr_signal) + "</span></span>" +
      "<span>Call OI: <strong>" + fmtV(chain.total_call_oi) + "</strong></span>" +
      "<span>Put OI: <strong>" + fmtV(chain.total_put_oi) + "</strong></span>" +
      "<span style=\"font-size:10px;color:var(--text-muted)\">" + esc(chain.source || "") + "</span>" +
      "</div>";
  }

  // -- Option chain table ---------------------------------------------------
  function renderChain(chain, tbodyId, spotFilter) {
    var tb = document.getElementById(tbodyId);
    if (!tb) return;
    if (!chain || !chain.available) {
      tb.innerHTML = "<tr><td colspan=\"16\" class=\"table-empty\">" +
        (chain ? esc(chain.reason || "Data unavailable") : "DATA UNAVAILABLE") + "</td></tr>";
      return;
    }

    var rows = chain.chain || [];
    var spot = chain.spot || 0;

    // Filter: show ATM +-10 strikes unless spotFilter=false
    if (spotFilter !== false && spot > 0) {
      var step = rows.length > 2 ? rows[1].strike - rows[0].strike : 50;
      var window = step * 10;
      rows = rows.filter(function(r) { return Math.abs(r.strike - spot) <= window; });
    }

    if (!rows.length) {
      tb.innerHTML = "<tr><td colspan=\"16\" class=\"table-empty\">No strikes in range</td></tr>";
      return;
    }

    tb.innerHTML = rows.map(function(r) {
      var isATM = r.moneyness === "ATM";
      var atmCls = isATM ? "opts-atm-row" : "";
      function side(s, type) {
        if (!s) return "<td>--</td><td>--</td><td>--</td><td>--</td><td>--</td><td>--</td><td>--</td>";
        var isCE = type === "CE";
        return "<td class=\"" + (isCE ? cc(s.delta) : "") + "\">" + fmt(s.ltp) + "</td>" +
          "<td>" + (s.iv != null ? s.iv + "%" : "--") + "</td>" +
          "<td>" + (s.delta != null ? s.delta.toFixed(4) : "--") + "</td>" +
          "<td>" + (s.gamma != null ? s.gamma.toFixed(6) : "--") + "</td>" +
          "<td>" + (s.theta != null ? s.theta.toFixed(4) : "--") + "</td>" +
          "<td>" + (s.vega  != null ? s.vega.toFixed(4)  : "--") + "</td>" +
          "<td>" + fmtV(s.oi) + "</td>";
      }
      return "<tr class=\"" + atmCls + "\">" +
        side(r.ce, "CE") +
        "<td class=\"opts-strike-cell\"><strong>" + fmt(r.strike, 0) + "</strong><br><span style=\"font-size:9px\">" + esc(r.moneyness) + "</span></td>" +
        side(r.pe, "PE") +
      "</tr>";
    }).join("");
  }

  // -- Render everything ----------------------------------------------------
  function render(data) {
    renderPortfolioGreeks(data.portfolio_greeks);
    renderChainSummary(data.nifty,     "opts-nifty-summary");
    renderChainSummary(data.banknifty, "opts-bnf-summary");
    renderChain(data.nifty,     "opts-nifty-chain",     true);
    renderChain(data.banknifty, "opts-bnf-chain",       true);

    var updated = document.getElementById("opts-last-updated");
    if (updated) updated.textContent = "Updated: " + new Date().toLocaleTimeString("en-IN", {timeZone:"Asia/Kolkata"}) + " IST";

    // IV Rank note
    var note = document.getElementById("opts-ivrank-note");
    if (note && data.config) note.textContent = "Greeks: " + (data.config.greeks_source || "BS calculator");
  }

  // -- Refresh --------------------------------------------------------------
  async function refresh() {
    var btn = document.getElementById("opts-refresh-btn");
    if (btn) btn.disabled = true;
    try {
      var res  = await fetch("/api/options");
      if (!res.ok) throw new Error("HTTP " + res.status);
      var data = await res.json();
      render(data);
    } catch(e) {
      console.error("[Options]", e);
      var u = document.getElementById("opts-last-updated");
      if (u) u.textContent = "Fetch failed: " + e.message;
    } finally {
      if (btn) btn.disabled = false;
      if (typeof lucide !== "undefined") lucide.createIcons();
    }
  }

  // -- Chain toggle (show all vs ATM window) --------------------------------
  window._optsShowAll = false;
  function toggleAllStrikes() {
    window._optsShowAll = !window._optsShowAll;
    var btn = document.getElementById("opts-toggle-all");
    if (btn) btn.textContent = window._optsShowAll ? "Show ATM Window" : "Show All Strikes";
    // Re-render last data
    if (window._optsLastData) {
      renderChain(window._optsLastData.nifty,     "opts-nifty-chain",     !window._optsShowAll);
      renderChain(window._optsLastData.banknifty, "opts-bnf-chain",       !window._optsShowAll);
    }
  }

  // Wrap refresh to store last data
  var _origRefresh = refresh;
  refresh = async function() {
    var btn = document.getElementById("opts-refresh-btn");
    if (btn) btn.disabled = true;
    try {
      var res  = await fetch("/api/options");
      if (!res.ok) throw new Error("HTTP " + res.status);
      var data = await res.json();
      window._optsLastData = data;
      render(data);
    } catch(e) {
      console.error("[Options]", e);
      var u = document.getElementById("opts-last-updated");
      if (u) u.textContent = "Fetch failed: " + e.message;
    } finally {
      if (btn) btn.disabled = false;
      if (typeof lucide !== "undefined") lucide.createIcons();
    }
  };

  // -- Auto-load and 60s refresh --------------------------------------------
  var _timer = null;
  function onActive() {
    refresh();
    _timer = _timer || setInterval(refresh, 60000);
  }
  function onInactive() {
    if (_timer) { clearInterval(_timer); _timer = null; }
  }

  document.addEventListener("DOMContentLoaded", function() {
    var pg = document.getElementById("page-options");
    if (!pg) return;
    new MutationObserver(function() {
      pg.classList.contains("active") ? onActive() : onInactive();
    }).observe(pg, { attributes: true, attributeFilter: ["class"] });
    if (pg.classList.contains("active")) onActive();
  });

  window.optionsPage = { refresh: refresh, toggleAllStrikes: toggleAllStrikes };
})();
