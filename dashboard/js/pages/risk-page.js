/**
 * Risk Page — Phase 9
 * Data source:  GET /api/risk  (every 8 s) + SSE event "risk"
 * Engine makes ALL trading decisions. This page is monitoring only.
 */
;(function () {
  "use strict";

  var esc  = function(s){ return String(s==null?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;"); };
  var pct  = function(v,d){ return v==null?"--":Number(v).toFixed(d==null?1:d)+"%"; };
  var num  = function(v,d){ return v==null?"--":Number(v).toFixed(d==null?2:d); };

  function ratioColor(r,warnAt,critAt){
    if(r>=(critAt||0.9)) return "#ef4444";
    if(r>=(warnAt||0.6)) return "#f59e0b";
    return "#10b981";
  }

  function barHTML(val,max,warnAt,critAt,suffix){
    if(val==null) return '<span class="risk-na">DATA UNAVAILABLE</span>';
    var pctV=Math.min(100,max>0?(val/max*100):0);
    var color=ratioColor(pctV/100,warnAt,critAt);
    suffix=suffix||"%";
    return (
      '<div class="risk-bar-wrap">'+
        '<div class="risk-bar-track">'+
          '<div class="risk-bar-fill" style="width:'+pctV.toFixed(1)+'%;background:'+color+'"></div>'+
        '</div>'+
        '<span class="risk-bar-label" style="color:'+color+'">'+
          num(val,2)+suffix+' / '+num(max,1)+suffix+
        '</span>'+
      '</div>'
    );
  }

  function renderStatus(d){
    var el=document.getElementById("risk-status-banner");
    if(!el) return;
    var s=d.overall_status||"NORMAL";
    var icons={NORMAL:"shield-check",WARNING:"shield-alert",CRITICAL:"shield-x",HALTED:"ban"};
    var colors={NORMAL:"#10b981",WARNING:"#f59e0b",CRITICAL:"#ef4444",HALTED:"#ef4444"};
    var labels={NORMAL:"NORMAL",WARNING:"WARNING",CRITICAL:"CRITICAL",HALTED:"HALTED"};
    var emojis={NORMAL:"🟢",WARNING:"🟡",CRITICAL:"🔴",HALTED:"⛔"};
    var color=colors[s]||"#94a3b8";
    el.className="risk-status-banner risk-status-"+s.toLowerCase();
    el.innerHTML=
      '<div class="risk-status-icon"><i data-lucide="'+(icons[s]||"shield")+'" width="28" height="28" style="color:'+color+'"></i></div>'+
      '<div class="risk-status-text">'+
        '<div class="risk-status-label" style="color:'+color+'">'+(emojis[s]||"")+" "+(labels[s]||s)+'</div>'+
        '<div class="risk-status-sub">'+esc(d.prop_firm?d.prop_firm.toUpperCase()+" rules active":"Engine circuit breakers active")+'</div>'+
      '</div>'+
      '<div class="risk-status-ts" id="risk-last-updated" style="margin-left:auto;font-size:11px;color:var(--text-muted)"></div>';
    if(typeof lucide!=="undefined") lucide.createIcons();
  }

  function renderGauges(d){
    var heat=(d.heat||{});
    var gauges=[
      {id:"risk-gauge-daily",label:"Daily Loss",        val:d.daily_loss_pct, max:d.daily_loss_limit, warnAt:0.6,critAt:0.9},
      {id:"risk-gauge-dd",   label:"Drawdown",          val:d.daily_loss_pct, max:d.max_drawdown_pct, warnAt:0.5,critAt:0.8},
      {id:"risk-gauge-heat", label:"Portfolio Heat",    val:heat.total,       max:d.max_heat,         warnAt:0.6,critAt:0.85},
      {id:"risk-gauge-corr", label:"Correlation Exposure",val:d.corr_exposure_pct,max:100,            warnAt:0.4,critAt:0.7},
    ];
    gauges.forEach(function(g){
      var el=document.getElementById(g.id);
      if(!el) return;
      el.innerHTML='<div class="risk-gauge-label">'+esc(g.label)+'</div>'+barHTML(g.val,g.max,g.warnAt,g.critAt);
    });
  }

  function checkRow(label,ok,detail,subtext){
    var cls=ok?"risk-check-ok":"risk-check-fail";
    return '<div class="risk-check-row '+cls+'">'+
      '<span class="risk-check-icon">'+(ok?"✅":"❌")+'</span>'+
      '<div class="risk-check-body">'+
        '<span class="risk-check-label">'+esc(label)+'</span>'+
        (detail!=null?'<span class="risk-check-detail">'+esc(detail)+'</span>':'')+
        (subtext?'<div class="risk-check-sub">'+esc(subtext)+'</div>':'')+
      '</div>'+
    '</div>';
  }

  function renderChecks(d){
    var el=document.getElementById("risk-checks");
    if(!el) return;
    var cb=!!d.circuit_breaker;
    var cons=d.consecutive_losses||0;
    var consMax=d.max_consecutive_losses||5;
    var consDays=d.consecutive_loss_days||0;
    var vixOk=!d.vix_status||d.vix_status==="NORMAL"||d.vix_status==="LOW";
    var vixLabel=d.vix!=null?("India VIX: "+d.vix.toFixed(1)+" ("+( d.vix_status||"N/A")+")") : "India VIX: Data unavailable";
    var dailyOk=d.daily_loss_pct<(d.daily_loss_limit||4)*0.9;
    var heatTotal=(d.heat||{}).total||0;
    var heatOk=heatTotal<(d.max_heat||6)*0.9;
    var corrOk=(d.corr_exposure_pct||0)<70;
    var ddOk=(d.daily_loss_pct||0)<(d.max_drawdown_pct||10)*0.8;

    el.innerHTML=
      checkRow("Circuit Breaker",!cb,cb?"ACTIVE — trading halted by engine":"Armed — ready",
        "Fires after "+consMax+" consecutive losing days. Auto-resets with cooldown period.")+
      checkRow("Daily Loss Limit",dailyOk,
        pct(d.daily_loss_pct)+" of "+pct(d.daily_loss_limit)+" limit",
        "Config: RISK.max_daily_loss_pct")+
      checkRow("Drawdown Limit",ddOk,
        "Max allowed: "+pct(d.max_drawdown_pct)+" · Current: "+pct(d.daily_loss_pct),
        "Config: PAPER_TRADING.max_drawdown_pct")+
      checkRow("Portfolio Heat",heatOk,
        pct(heatTotal,2)+" of "+pct(d.max_heat)+" max ("+pct(d.emergency_heat)+" = emergency close)",
        "Source: risk/portfolio_risk_engine.py — aggregated across all open positions")+
      checkRow("Correlation Limit",corrOk,
        "USD correlation: "+pct(d.corr_exposure_pct)+" — max 2% per corr group",
        "Config: RISK.max_correlated_exposure_pct")+
      checkRow("VIX Filter",vixOk,vixLabel,
        "Thresholds: HIGH=20, EXTREME=28 → engine reduces size / halts new entries")+
      checkRow("Consecutive Loss Streak",cons<consMax,
        cons+" of "+consMax+" max · "+consDays+" losing day(s)",
        "Config: RISK.circuit_breaker_losing_days");
  }

  function renderHeatGrid(d){
    var el=document.getElementById("risk-heat-grid");
    if(!el) return;
    var heat=((d.heat||{}).by_market)||{};
    var maxH=d.max_heat||6;
    var keys=Object.keys(heat);
    if(!keys.length){el.innerHTML='<div class="india-na-block">No open positions — portfolio heat is 0%</div>';return;}
    el.innerHTML=keys.map(function(mkt){
      var v=heat[mkt]||0;
      var col=ratioColor(v/maxH,0.5,0.8);
      var bw=Math.min(100,v/maxH*100);
      return '<div class="risk-heat-item">'+
        '<div class="risk-heat-mkt">'+esc(mkt)+'</div>'+
        '<div class="risk-bar-track" style="flex:1">'+
          '<div class="risk-bar-fill" style="width:'+bw.toFixed(1)+'%;background:'+col+'"></div>'+
        '</div>'+
        '<span class="risk-heat-pct" style="color:'+col+'">'+pct(v,2)+'</span>'+
      '</div>';
    }).join('');
  }

  function renderCurrencyExposure(d){
    var el=document.getElementById("risk-ccy-grid");
    if(!el) return;
    var ccy=((d.heat||{}).by_currency)||{};
    var maxCcy=40;
    var keys=Object.keys(ccy);
    if(!keys.length){el.innerHTML='<div class="india-na-block">No position data</div>';return;}
    el.innerHTML=keys.map(function(c){
      var v=ccy[c]||0;
      var col=ratioColor(v/maxCcy,0.5,0.8);
      var bw=Math.min(100,v/maxCcy*100);
      return '<div class="risk-heat-item">'+
        '<div class="risk-heat-mkt" style="width:55px;font-family:monospace">'+esc(c)+'</div>'+
        '<div class="risk-bar-track" style="flex:1">'+
          '<div class="risk-bar-fill" style="width:'+bw.toFixed(1)+'%;background:'+col+'"></div>'+
        '</div>'+
        '<span class="risk-heat-pct" style="color:'+col+'">'+pct(v,2)+'</span>'+
      '</div>';
    }).join('');
  }

  function renderFlags(d){
    var el=document.getElementById("risk-flags");
    if(!el) return;
    var flags=d.flags||[];
    if(!flags.length){el.innerHTML='<div class="india-na-block" style="color:var(--success)">✅ No active risk flags</div>';return;}
    el.innerHTML=flags.map(function(f){
      return '<div class="risk-flag '+(f.ok?"risk-flag-ok":"risk-flag-warn")+'">'+(f.ok?"✅":"⚠️")+" "+esc(f.text)+'</div>';
    }).join('');
  }

  function renderWinRate(d){
    var el=document.getElementById("risk-winrate");
    if(!el) return;
    if(d.live_win_rate==null){el.innerHTML='<div class="india-na-block">Win rate: need ≥50 closed trades</div>';return;}
    var live=d.live_win_rate, bt=d.backtest_win_rate||62;
    var ok=live>=bt*0.8;
    var col=ok?"var(--success)":"var(--danger)";
    el.innerHTML=
      '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:var(--space-3)">'+
        '<div style="text-align:center"><div style="font-size:11px;color:var(--text-muted);margin-bottom:4px">Live Win Rate</div><div style="font-size:22px;font-weight:700;color:'+col+'">'+pct(live)+'</div></div>'+
        '<div style="text-align:center"><div style="font-size:11px;color:var(--text-muted);margin-bottom:4px">Backtested</div><div style="font-size:22px;font-weight:700;color:var(--text-muted)">'+pct(bt)+'</div></div>'+
        '<div style="text-align:center"><div style="font-size:11px;color:var(--text-muted);margin-bottom:4px">Status</div><div style="font-size:16px;font-weight:700;color:'+col+'">'+(ok?"✅ On track":"⚠️ Flagged")+'</div></div>'+
      '</div>';
  }

  var _lastData=null;
  function render(d){
    _lastData=d;
    renderStatus(d);
    renderGauges(d);
    renderChecks(d);
    renderHeatGrid(d);
    renderCurrencyExposure(d);
    renderFlags(d);
    renderWinRate(d);
    var ts=document.getElementById("risk-last-updated");
    if(ts) ts.textContent="Updated: "+new Date().toLocaleTimeString();
    if(typeof lucide!=="undefined") lucide.createIcons();
  }

  async function refresh(){
    try{
      var res=await fetch("/api/risk");
      if(!res.ok) throw new Error("HTTP "+res.status);
      render(await res.json());
    }catch(e){
      console.error("[Risk]",e);
      var ts=document.getElementById("risk-last-updated");
      if(ts) ts.textContent="Fetch failed: "+e.message;
    }
  }

  function subscribeSSE(){
    function handle(data){
      if(!_lastData) return;
      if(data.circuit_breaker!=null) _lastData.circuit_breaker=data.circuit_breaker;
      if(data.daily_pnl_pct!=null)   _lastData.daily_loss_pct=Math.abs(Math.min(0,data.daily_pnl_pct));
      if(data.consecutive_losses!=null) _lastData.consecutive_losses=data.consecutive_losses;
      var cb=_lastData.circuit_breaker;
      var dloss=_lastData.daily_loss_pct||0, dlimit=_lastData.daily_loss_limit||4;
      _lastData.overall_status=cb?"HALTED":dloss>=dlimit*0.9?"CRITICAL":dloss>=dlimit*0.6?"WARNING":"NORMAL";
      render(_lastData);
    }
    if(window.connMgr&&typeof window.connMgr.on==="function"){window.connMgr.on("risk",handle);return;}
    var es=new EventSource("/api/stream");
    es.addEventListener("risk",function(e){try{handle(JSON.parse(e.data));}catch(_){}});
    es.onerror=function(){};
    window._riskSSE=es;
  }

  var _timer=null;
  function onActive(){refresh();subscribeSSE();_timer=_timer||setInterval(refresh,8000);}
  function onInactive(){if(_timer){clearInterval(_timer);_timer=null;}if(window._riskSSE){window._riskSSE.close();window._riskSSE=null;}}

  document.addEventListener("DOMContentLoaded",function(){
    var pg=document.getElementById("page-risk");
    if(!pg) return;
    new MutationObserver(function(){pg.classList.contains("active")?onActive():onInactive();}).observe(pg,{attributes:true,attributeFilter:["class"]});
    if(pg.classList.contains("active")) onActive();
  });

  window.riskPage={refresh:refresh};
})();
