/**
 * India Markets page controller
 * Polls /api/india — powered by ZerodhaAgent + yfinance in the engine.
 * No signals computed here. All decisions stay in the trading engine.
 */
;(function () {
  "use strict";

  const fmt  = (v, dec) => { dec = dec != null ? dec : 2; return v != null ? Number(v).toLocaleString("en-IN", {minimumFractionDigits:dec,maximumFractionDigits:dec}) : null; };
  const fmtV = (v) => v != null ? Number(v).toLocaleString("en-IN") : null;
  const tsIST = (iso) => { if (!iso) return "--"; return new Date(iso).toLocaleTimeString("en-IN",{hour:"2-digit",minute:"2-digit",second:"2-digit",timeZone:"Asia/Kolkata"}) + " IST"; };
  const esc  = (s) => String(s != null ? s : "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
  const chgClass = (v) => v == null ? "neu" : v > 0 ? "pos" : v < 0 ? "neg" : "neu";
  const chgSign  = (v) => v == null ? "--" : (v > 0 ? "+" : "") + (fmt(v) != null ? fmt(v) : "--");

  function naBlock(reason) {
    return "<div class=\"india-na-block\"><span class=\"india-na-label\">DATA UNAVAILABLE</span><br><span class=\"india-na-reason\">" + esc(reason || "Source not connected") + "</span></div>";
  }
  function naCell() { return "<td class=\"india-na-cell\">DATA UNAVAILABLE</td>"; }

  function renderMarketStatus(market, zerodha) {
    var sub  = document.getElementById("india-market-status-sub");
    var chip = document.getElementById("india-zerodha-chip");
    if (sub && market) {
      var labels = {OPEN:"OPEN",PRE_OPEN:"PRE-OPEN",CLOSING:"CLOSING",CLOSED:"CLOSED",WEEKEND:"WEEKEND"};
      var s = market.status || "UNKNOWN";
      sub.innerHTML = "<span class=\"india-mkt-badge " + esc(s) + "\">" + esc(labels[s] || s) + "</span> &nbsp; " + esc(market.ist_time || "") + " &nbsp;&middot;&nbsp; NSE/BSE &nbsp;&middot;&nbsp; <span style=\"font-size:10px;color:var(--text-muted)\">Source: system_clock</span>";
    }
    if (chip && zerodha) {
      var auth = zerodha.authenticated;
      chip.className = "status-chip " + (auth ? "live" : "offline");
      chip.innerHTML = "<span class=\"status-dot " + (auth?"live":"offline") + "\"></span> Zerodha" + (auth ? " \u2713" : " \u2717");
      if (!auth) chip.title = "Zerodha token not loaded -- daily login required by 09:00 IST";
    }
  }

  function renderIndexCard(bodyId, srcId, q) {
    var body = document.getElementById(bodyId);
    var src  = document.getElementById(srcId);
    if (!body) return;
    if (!q || !q.available) {
      body.innerHTML = naBlock(q && q.reason === "not_authenticated" ? "Zerodha not logged in -- daily OAuth token required" : (q && q.reason) || "No data returned from source");
      if (src) src.textContent = "Source: Zerodha Kite - NSE";
      return;
    }
    var cc = chgClass(q.change);
    body.innerHTML = "<div class=\"india-big-price\">" + (fmt(q.last_price) || "--") + "</div>" +
      "<div class=\"india-change " + cc + "\" style=\"font-size:15px;margin-top:2px;\">" + chgSign(q.change) + " &nbsp;(" + chgSign(q.change_pct) + "%)</div>" +
      "<div class=\"india-ohlc-row\"><span>O&nbsp;<strong>" + (fmt(q.open)||"--") + "</strong></span><span>H&nbsp;<strong>" + (fmt(q.high)||"--") + "</strong></span><span>L&nbsp;<strong>" + (fmt(q.low)||"--") + "</strong></span><span>C&nbsp;<strong>" + (fmt(q.prev_close)||"--") + "</strong></span></div>" +
      (q.volume != null ? "<div style=\"margin-top:6px;font-size:11px;color:var(--text-muted)\">Vol:&nbsp;<strong style=\"color:var(--text-primary)\">" + fmtV(q.volume) + "</strong></div>" : "");
    if (src) src.textContent = "Source: " + (q.source || "zerodha_kite") + " - " + tsIST(q.ts);
  }

  function renderVix(d) {
    var body = document.getElementById("india-vix-body");
    var src  = document.getElementById("india-vix-source");
    if (!body) return;
    if (!d || !d.available) {
      body.innerHTML = naBlock("yfinance unavailable or market closed (VIX updated daily)");
      if (src) src.textContent = "Source: yfinance - ^INDIAVIX";
      return;
    }
    var cc  = chgClass(d.change);
    var col = d.regime === "EXTREME" ? "#ef4444" : d.regime === "HIGH" ? "#f59e0b" : d.regime === "LOW" ? "#10b981" : "var(--text-primary)";
    body.innerHTML = "<div class=\"india-big-price\" style=\"color:" + col + "\">" + (fmt(d.value,2)||"--") + "</div>" +
      "<div style=\"margin-top:4px;display:flex;align-items:center;gap:8px;\"><span class=\"india-vix-regime " + esc(d.regime) + "\">" + esc(d.regime) + "</span><span class=\"india-change " + cc + "\" style=\"font-size:13px;\">" + chgSign(d.change) + "&nbsp;(" + chgSign(d.change_pct) + "%)</span></div>" +
      "<div style=\"margin-top:8px;font-size:11px;color:var(--text-muted);\">Prev close:&nbsp;<strong style=\"color:var(--text-primary)\">" + (fmt(d.prev_close)||"--") + "</strong>&nbsp;&middot;&nbsp;Thresholds: Low&lt;13 &middot; High&gt;20 &middot; Extreme&gt;28</div>";
    if (src) src.textContent = "Source: " + esc(d.source) + " - " + tsIST(d.ts);
  }

  function renderFiiDii(d) {
    var body = document.getElementById("india-fiidii-body");
    var src  = document.getElementById("india-fiidii-source");
    if (!body) return;
    if (!d || d.available === false) {
      body.innerHTML = naBlock("NSE website unavailable or data not yet published today (released after market close)");
      if (src) src.textContent = "Source: NSE website - Daily after close";
      return;
    }
    var fc = chgClass(d.fii_net), dc = chgClass(d.dii_net), cb = chgClass(d.combined);
    body.innerHTML = "<div style=\"display:grid;grid-template-columns:1fr 1fr 1fr;gap:var(--space-3);margin-bottom:10px;\">" +
      "<div><div style=\"font-size:10px;color:var(--text-muted);margin-bottom:2px;\">FII Net (&#8377; Cr)</div><div class=\"india-change " + fc + "\" style=\"font-size:20px;font-weight:700;\">" + chgSign(d.fii_net) + "</div></div>" +
      "<div><div style=\"font-size:10px;color:var(--text-muted);margin-bottom:2px;\">DII Net (&#8377; Cr)</div><div class=\"india-change " + dc + "\" style=\"font-size:20px;font-weight:700;\">" + chgSign(d.dii_net) + "</div></div>" +
      "<div><div style=\"font-size:10px;color:var(--text-muted);margin-bottom:2px;\">Combined (&#8377; Cr)</div><div class=\"india-change " + cb + "\" style=\"font-size:20px;font-weight:700;\">" + chgSign(d.combined) + "</div></div>" +
      "</div><div style=\"font-size:11px;color:var(--text-muted);\">Date:&nbsp;<strong style=\"color:var(--text-primary)\">" + esc(d.date||"--") + "</strong>&nbsp;&middot;&nbsp;Engine bias:&nbsp;<span class=\"india-change " + cb + "\" style=\"font-weight:600;\">" + esc(d.bias||"--") + "</span></div>" +
      "<div style=\"font-size:10px;color:var(--text-muted);margin-top:4px;\">The engine uses this flow data for directional bias. It does not directly trigger orders.</div>";
    if (src) src.textContent = "Source: NSE website - " + tsIST(d.ts);
  }

  function renderPcr(pn, pb) {
    var body = document.getElementById("india-pcr-body");
    var src  = document.getElementById("india-pcr-source");
    if (!body) return;
    function pcrRow(label, d) {
      if (!d || d.available === false) return "<div style=\"margin-bottom:var(--space-3);\"><div style=\"font-size:11px;color:var(--text-muted);margin-bottom:4px;\">" + esc(label) + "</div>" + naBlock("NSE option chain unavailable") + "</div>";
      var sc = d.signal || "NEUTRAL";
      return "<div style=\"margin-bottom:var(--space-3);\"><div style=\"font-size:11px;color:var(--text-muted);margin-bottom:4px;\">" + esc(label) + "</div>" +
        "<div style=\"display:flex;align-items:center;gap:10px;\"><span style=\"font-size:22px;font-weight:700;\">" + (fmt(d.pcr,3)||"--") + "</span><span class=\"india-pcr-badge " + esc(sc) + "\">" + esc(sc) + "</span></div>" +
        "<div style=\"font-size:10px;color:var(--text-muted);margin-top:4px;\">Put OI:&nbsp;" + (fmtV(d.put_oi)||"--") + "&nbsp;&middot;&nbsp;Call OI:&nbsp;" + (fmtV(d.call_oi)||"--") + "</div></div>";
    }
    body.innerHTML = pcrRow("NIFTY PCR", pn) + pcrRow("BANKNIFTY PCR", pb) +
      "<div style=\"font-size:10px;color:var(--text-muted);border-top:1px solid var(--border-subtle);padding-top:8px;margin-top:4px;\">PCR &gt; 1.2 = Bearish &middot; PCR &lt; 0.8 = Bullish &middot; Updated every 3 min during market hours</div>";
    if (src) src.textContent = "Source: NSE option chain - " + tsIST((pn||pb) ? (pn||pb).ts : null);
  }

  function renderFutures(futures) {
    var tb = document.querySelector("#india-futures-table tbody");
    if (!tb) return;
    if (!futures || !futures.length) { tb.innerHTML = "<tr><td colspan=\"10\" class=\"table-empty\">No futures data</td></tr>"; return; }
    tb.innerHTML = futures.map(function(f) {
      if (!f.available) return "<tr><td><strong>" + esc(f.label||f.symbol) + "</strong></td><td>" + esc(f.lot_size||"--") + "</td>" + naCell()+naCell()+naCell()+naCell()+naCell()+naCell() + "<td class=\"india-na-cell\">" + esc(f.source||"zerodha_kite") + "</td><td class=\"india-na-cell\">--</td></tr>";
      var cc = chgClass(f.change);
      return "<tr><td><strong>" + esc(f.label||f.symbol) + "</strong></td><td>" + esc(f.lot_size||"--") + "</td><td style=\"font-weight:600;\">" + (fmt(f.last_price)||"--") + "</td><td class=\"india-change " + cc + "\">" + chgSign(f.change) + "</td><td class=\"india-change " + cc + "\">" + chgSign(f.change_pct) + "%</td><td>" + (fmtV(f.open_interest)||"--") + "</td><td>" + (fmt(f.high)||"--") + "</td><td>" + (fmt(f.low)||"--") + "</td><td style=\"font-size:10px;color:var(--text-muted)\">" + esc(f.source||"--") + "</td><td style=\"font-size:10px;color:var(--text-muted)\">" + tsIST(f.ts) + "</td></tr>";
    }).join("");
  }

  var _allStocks = [];
  function renderStocks(stocks) { _allStocks = stocks || []; _renderStockRows(_allStocks); }
  function _renderStockRows(stocks) {
    var tb = document.querySelector("#india-stocks-table tbody");
    if (!tb) return;
    if (!stocks.length) { tb.innerHTML = "<tr><td colspan=\"11\" class=\"table-empty\">No stocks data</td></tr>"; return; }
    tb.innerHTML = stocks.map(function(s) {
      if (!s.available) return "<tr><td><strong>" + esc(s.symbol) + "</strong></td><td style=\"font-size:12px;\">" + esc(s.label||"") + "</td><td><span class=\"badge badge-outline\" style=\"font-size:10px;\">" + esc(s.sector||"") + "</span></td>" + naCell()+naCell()+naCell()+naCell()+naCell()+naCell() + "<td class=\"india-na-cell\">" + esc(s.reason==="not_authenticated"?"Not logged in":"Unavailable") + "</td><td class=\"india-na-cell\">--</td></tr>";
      var cc = chgClass(s.change);
      var ckt = (s.upper_circuit && s.lower_circuit) ? "<span style=\"font-size:10px;color:var(--text-muted)\">U:" + (fmt(s.upper_circuit,0)||"--") + " / L:" + (fmt(s.lower_circuit,0)||"--") + "</span>" : "--";
      return "<tr><td><strong>" + esc(s.symbol) + "</strong></td><td style=\"font-size:12px;\">" + esc(s.label||"") + "</td><td><span class=\"badge badge-outline\" style=\"font-size:10px;\">" + esc(s.sector||"") + "</span></td><td style=\"font-weight:600;\">" + (fmt(s.last_price)||"--") + "</td><td class=\"india-change " + cc + "\">" + chgSign(s.change) + "</td><td class=\"india-change " + cc + "\">" + chgSign(s.change_pct) + "%</td><td style=\"font-size:11px;\">" + (fmtV(s.volume)||"--") + "</td><td>" + (fmt(s.high)||"--") + "</td><td>" + (fmt(s.low)||"--") + "</td><td style=\"font-size:10px;\">" + ckt + "</td><td style=\"font-size:10px;color:var(--text-muted)\">" + tsIST(s.ts) + "</td></tr>";
    }).join("");
  }

  function filterStocks(q) {
    var lc = (q || "").toLowerCase();
    _renderStockRows(!lc ? _allStocks : _allStocks.filter(function(s) {
      return (s.symbol||"").toLowerCase().indexOf(lc) >= 0 || (s.label||"").toLowerCase().indexOf(lc) >= 0 || (s.sector||"").toLowerCase().indexOf(lc) >= 0;
    }));
  }

  async function refresh() {
    var btn     = document.getElementById("india-refresh-btn");
    var updated = document.getElementById("india-last-updated");
    if (btn) btn.disabled = true;
    try {
      var res  = await fetch("/api/india");
      if (!res.ok) throw new Error("HTTP " + res.status);
      var data = await res.json();
      renderMarketStatus(data.market, data.zerodha);
      renderIndexCard("india-nifty-body",     "india-nifty-source",     data.nifty);
      renderIndexCard("india-banknifty-body", "india-banknifty-source", data.banknifty);
      renderVix(data.india_vix);
      renderFiiDii(data.fii_dii);
      renderPcr(data.pcr_nifty, data.pcr_banknifty);
      renderFutures(data.futures);
      renderStocks(data.stocks);
      if (updated) updated.textContent = "Updated: " + new Date().toLocaleTimeString("en-IN",{timeZone:"Asia/Kolkata"}) + " IST";
    } catch(e) {
      console.error("[India]", e);
      if (updated) updated.textContent = "Fetch failed -- " + e.message;
      ["india-nifty-body","india-banknifty-body","india-vix-body","india-fiidii-body","india-pcr-body"].forEach(function(id) {
        var el = document.getElementById(id); if (el) el.innerHTML = naBlock("Server error: " + e.message);
      });
    } finally {
      if (btn) btn.disabled = false;
      if (typeof lucide !== "undefined") lucide.createIcons();
    }
  }

  var _timer = null;
  function _onActive()   { refresh(); _timer = _timer || setInterval(refresh, 30000); }
  function _onInactive() { if (_timer) { clearInterval(_timer); _timer = null; } }

  document.addEventListener("DOMContentLoaded", function() {
    var pg = document.getElementById("page-markets");
    if (!pg) return;
    new MutationObserver(function() {
      pg.classList.contains("active") ? _onActive() : _onInactive();
    }).observe(pg, {attributes:true, attributeFilter:["class"]});
    if (pg.classList.contains("active")) _onActive();
  });

  window.indiaPage = {refresh:refresh, filterStocks:filterStocks};
})();
