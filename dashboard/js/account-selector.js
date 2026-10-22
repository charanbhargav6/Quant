/**
 * Account Selector — Multi-account support
 * ─────────────────────────────────────────────────────────────────────────────
 * Loads /api/accounts, renders a dropdown in the header.
 * Broadcasts "accountChanged" events that all page controllers listen to.
 *
 * Security:
 *   - No credentials ever exposed to the browser
 *   - Each account sees only its own data (server enforces via account login filter)
 *   - Cross-account totals are computed only when "ALL" is selected
 *   - The account_id is passed as a query param; the server does the scoping
 *
 * Usage (in any page controller):
 *   window.accountSelector.getActive()  → { id, name, type } or { id:"ALL" }
 *   window.addEventListener("accountChanged", function(e) { refresh(e.detail); });
 */
;(function(){
  "use strict";

  var _accounts = [];
  var _active   = { id: "ALL", name: "All Accounts", type: "aggregate" };

  /* ── Render dropdown ───────────────────────────────────────────────────── */
  function render(){
    var el = document.getElementById("account-selector-wrap");
    if(!el) return;

    var opts = '<option value="ALL">🌐 All Accounts</option>' +
      _accounts.map(function(a){
        var icon = a.type === "live" ? "🔴" : a.type === "paper" ? "🟡" : "🔵";
        var conn = a.connected ? "" : " (offline)";
        return '<option value="'+esc(a.id)+'">'+icon+" "+esc(a.name||a.id)+conn+'</option>';
      }).join('');

    el.innerHTML =
      '<div class="acct-select-wrap">'+
        '<select id="account-selector" class="acct-select" onchange="accountSelector._onChange(this.value)">'+
          opts+
        '</select>'+
        '<span id="acct-status-dot" class="acct-dot"></span>'+
      '</div>';

    /* Restore previously selected */
    var sel = document.getElementById("account-selector");
    if(sel && _active.id) sel.value = _active.id;
    _updateDot();
  }

  function _updateDot(){
    var dot = document.getElementById("acct-status-dot");
    if(!dot) return;
    if(_active.id === "ALL"){
      dot.style.background = "#3b82f6";
      dot.title = "Aggregate — all accounts";
    } else {
      var acc = _accounts.find(function(a){ return a.id === _active.id; });
      if(!acc){ dot.style.background="#6b7280"; return; }
      dot.style.background = acc.connected ? "#10b981" : "#ef4444";
      dot.title = acc.connected ? "Connected" : "Offline";
    }
  }

  function esc(s){ return String(s==null?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/"/g,"&quot;"); }

  /* ── Auto-detect API base (same logic as api-provider.js) ───────────────── */
  function apiBase() {
    if (window.CRAVE_API_BASE) return window.CRAVE_API_BASE;
    var port = parseInt(window.location.port || '80', 10);
    if (port === 9090 || port === 5500 || port === 3000 || port === 8080) {
      return window.location.protocol + '//' + window.location.hostname + ':8765';
    }
    return '';
  }

  /* ── Load accounts from server ─────────────────────────────────────────── */
  async function load(){
    try{
      var res = await fetch(apiBase() + "/api/accounts");
      if(!res.ok) throw new Error("HTTP "+res.status);
      var data = await res.json();
      _accounts = data.accounts || [];
      render();
      _dispatchChange();
    }catch(e){
      console.warn("[AccountSelector] load failed:", e);
    }
  }

  /* ── Handle selection change ───────────────────────────────────────────── */
  function _onChange(val){
    if(val === "ALL"){
      _active = { id: "ALL", name: "All Accounts", type: "aggregate" };
    } else {
      var acc = _accounts.find(function(a){ return a.id === val; });
      _active = acc || { id: val, name: val, type: "unknown" };
    }
    _updateDot();
    // Invalidate DataService cache so pages re-fetch with new account scope
    if (window.DS && typeof window.DS.invalidate === 'function') {
      window.DS.invalidate();
    }
    _dispatchChange();
  }

  function _dispatchChange(){
    window.dispatchEvent(new CustomEvent("accountChanged", { detail: _active }));
  }


  /* ── API scoping helper ─────────────────────────────────────────────────
     Call this from page controllers to add ?account=XXX to API URLs.
     When ALL is selected, returns the base URL unchanged (server sends
     aggregate or first-active-account data).                              */
  function scopedUrl(baseUrl){
    if(!_active || _active.id === "ALL") return baseUrl;
    var sep = baseUrl.includes("?") ? "&" : "?";
    return baseUrl + sep + "account=" + encodeURIComponent(_active.id);
  }

  /* ── Init ──────────────────────────────────────────────────────────────── */
  document.addEventListener("DOMContentLoaded", function(){
    load();
    /* Reload every 30s to pick up newly added accounts */
    setInterval(load, 30000);
  });

  window.accountSelector = {
    getActive:  function(){ return _active; },
    getAll:     function(){ return _accounts; },
    scopedUrl:  scopedUrl,
    reload:     load,
    _onChange:  _onChange,
  };
})();
