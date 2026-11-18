/* Backtest tab. Mount:  BacktestPage.mount(document.getElementById('bt-root'))
 * Config (optional, before mount):  window.CRAVE_API_BASE = 'http://127.0.0.1:5000';  window.CRAVE_API_TOKEN = '...'
 * No mock data: if the API is unreachable the page says so. All dynamic text is HTML-escaped. */
(function (global) {
  'use strict';
  const PH = [['data', 'Data'], ['backtest', 'Backtest'], ['monte_carlo', 'Monte Carlo'], ['wfo', 'Walk-forward'], ['sensitivity', 'Sensitivity']];
  const TERMINAL = ['completed', 'negative', 'inconclusive', 'failed', 'cancelled'];
  const st = { root: null, caps: null, jobs: [], job: null, es: null, poll: null, hist: null, err: null, sel: null, raf: 0, mounted: false };
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const base = () => {
    const b = (global.CRAVE_API_BASE || '').replace(/\/$/, '');
    if (b) return b; return location.protocol.startsWith('http') ? '' : 'http://127.0.0.1:5000';
  };
  async function api(path, opt = {}) {
    const h = { 'Content-Type': 'application/json' }; if (global.CRAVE_API_TOKEN) h['X-CRAVE-Token'] = global.CRAVE_API_TOKEN;
    const r = await fetch(base() + path, { ...opt, headers: h });
    let j = null; try { j = await r.json(); } catch (e) { }
    if (!r.ok) throw new Error((j && j.error) || ('HTTP ' + r.status)); return j;
  }
  const num = (v, d = 2) => (v == null || isNaN(v)) ? '–' : Number(v).toFixed(d);
  const sgn = (v, d = 2, suf = '') => (v == null || isNaN(v)) ? '–' : `<span class="${v > 0 ? 'pos' : v < 0 ? 'neg' : ''}">${v > 0 ? '+' : ''}${Number(v).toFixed(d)}${suf}</span>`;
  const when = iso => { if (!iso) return '–'; const d = new Date(iso); return isNaN(d) ? esc(iso) : d.toLocaleString([], { month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit' }); };
  const pill = (s, txt) => `<span class="bt-pill ${esc(s || 'pending')}">${esc(txt || s || 'pending')}</span>`;
  const isRunning = j => j && (j.status === 'running' || j.status === 'queued');

  /* ---------- static launch panel (rendered once so inputs keep focus) ---------- */
  function shell() {
    st.root.innerHTML = `<div class="bt-root">
      <div id="bt-banner"></div>
      <div class="panel"><div class="panel-header"><div><div class="panel-title">Run a backtest</div>
        <div class="panel-subtitle">Real MT5 ticks &amp; spread → backtest → (if profitable) Monte Carlo → walk-forward → sensitivity</div></div></div>
        <div class="panel-body"><div class="bt-launch">
          <div class="bt-field"><label>Strategy</label><select id="bt-strat"></select></div>
          <div class="bt-field"><label>Symbol</label><input id="bt-sym" placeholder="auto"></div>
          <div class="bt-field"><label>Days</label><input id="bt-days" type="number" min="30" max="730" value="180"></div>
          <div class="bt-field"><label>Data</label><select id="bt-mode"><option value="ticks">Real ticks</option><option value="m1">M1 bars (faster)</option></select></div>
          <button class="bt-btn" id="bt-run">Run backtest</button></div>
          <div class="bt-hint" id="bt-hint"></div></div></div>
      <div id="bt-active"></div>
      <div class="panel"><div class="panel-header"><div><div class="panel-title">Last 15 backtests</div>
        <div class="panel-subtitle">Click a row to inspect its phases</div></div></div><div class="panel-body-flush bt-scroll" id="bt-history"></div></div></div>`;
    st.root.querySelector('#bt-run').onclick = run;
    st.root.querySelector('#bt-strat').onchange = e => { const s = (st.caps.strategies || []).find(x => x.ref === e.target.value); if (s) st.root.querySelector('#bt-sym').value = s.symbol || ''; hint(); };
  }
  function hint() {
    const s = (st.caps && st.caps.strategies || []).find(x => x.ref === st.root.querySelector('#bt-strat').value); const el = st.root.querySelector('#bt-hint'); if (!el) return;
    const c = (st.caps.cache || []).find(x => s && x.symbol === (st.root.querySelector('#bt-sym').value || s.symbol));
    el.innerHTML = (s && !s.backtestable ? `<span class="bt-bad">Not backtestable: ${esc(s.reason)}</span>` : '') +
      (c ? ` Cache: ${esc(c.days)} days (${esc(c.first)} → ${esc(c.last)}), volume = ${esc(c.volume_kind)}.` : ' No cached MT5 data for this symbol yet – it will be downloaded first.') +
      ' Needs MT5 terminal running on the bot machine.';
  }
  function fillCaps() {
    const sel = st.root.querySelector('#bt-strat'); const g = { registry: [], candidate: [] };
    (st.caps.strategies || []).forEach(s => g[s.kind === 'candidate' ? 'candidate' : 'registry'].push(s));
    const opt = s => `<option value="${esc(s.ref)}" ${s.backtestable ? '' : 'disabled'}>${esc(s.name)}${s.backtestable ? '' : ' (no adapter)'}</option>`;
    sel.innerHTML = `<optgroup label="Registered strategies">${g.registry.map(opt).join('')}</optgroup>` +
      (g.candidate.length ? `<optgroup label="Generated candidates">${g.candidate.map(opt).join('')}</optgroup>` : '');
    const first = (st.caps.strategies || []).find(s => s.backtestable); if (first) { sel.value = first.ref; st.root.querySelector('#bt-sym').value = first.symbol || ''; }
    st.root.querySelector('#bt-days').value = st.caps.defaults.days; hint();
  }

  /* ---------- active job ---------- */
  function equitySVG(eq) {
    if (!eq || eq.length < 2) return '<div class="bt-note">Equity curve appears when trades exist.</div>';
    const W = 520, H = 140, P = 8, ys = eq.map(p => p[1]), mn = Math.min(0, ...ys), mx = Math.max(0.001, ...ys);
    const X = i => P + (W - 2 * P) * i / (eq.length - 1), Y = v => H - P - (H - 2 * P) * (v - mn) / ((mx - mn) || 1);
    const pts = eq.map((p, i) => `${X(i).toFixed(1)},${Y(p[1]).toFixed(1)}`).join(' '); const up = ys[ys.length - 1] >= 0;
    return `<svg viewBox="0 0 ${W} ${H}" width="100%" height="140" preserveAspectRatio="none"><line x1="${P}" x2="${W - P}" y1="${Y(0)}" y2="${Y(0)}" stroke="var(--border-strong)" stroke-dasharray="3 4"/>
      <polyline fill="none" stroke="${up ? 'var(--green)' : 'var(--red)'}" stroke-width="2" points="${pts}"/></svg><div class="bt-note">Cumulative net R (after real spread)</div>`;
  }
  function checksTable(cs) {
    if (!cs || !cs.length) return '';
    return `<table class="bt-t"><thead><tr><th>Check</th><th class="r">Value</th><th class="r">Needed</th><th></th></tr></thead><tbody>` +
      cs.map(c => `<tr><td>${esc(c.name)}${c.note ? ` <span class="bt-note">· ${esc(c.note)}</span>` : ''}</td><td class="r">${num(c.value, 3)}</td><td class="r">${esc(c.op)} ${esc(num(c.threshold, 3))}</td><td class="${c.ok ? 'bt-ok' : 'bt-bad'}">${c.ok ? '✓' : '✗'}</td></tr>`).join('') + `</tbody></table>`;
  }
  function phaseDetail(j, key) {
    const p = (j.phases || {})[key]; if (!p) return '';
    let h = `<div class="bt-note"><b>${esc((PH.find(x => x[0] === key) || [0, key])[1])}</b> · ${pill(p.status)} ${p.reason ? '· ' + esc(p.reason) : ''}</div>` + checksTable(p.checks);
    const m = p.metrics || {};
    if (key === 'data' && m.quality) { const q = m.quality, d = m.download || {};
      h += `<div class="bt-note">${esc(q.bars)} bars · coverage ${num(q.coverage * 100, 1)}% · volume = <b>${esc(q.volume_kind)}</b>${q.volume_kind === 'tick_count' ? ' (tick count, not traded volume)' : ''} · ticks downloaded ${esc(d.ticks_total || 0)} · from ticks ${esc(d.from_ticks || 0)} / M1 fallback ${esc(d.from_m1 || 0)} days · suspicious gaps ${esc(q.suspicious_gaps)}</div>`; }
    if (key === 'backtest' && p.detail) h += `<div class="bt-note">Signals filtered: ${Object.entries(p.detail.skipped || {}).filter(e => e[1]).map(e => esc(e[0]) + ' ' + esc(e[1])).join(' · ') || 'none'}</div>`;
    if (key === 'wfo' && p.detail && p.detail.windows) h += `<div class="bt-scroll"><table class="bt-t"><thead><tr><th>#</th><th>In-sample from</th><th>OOS</th><th class="r">IS R</th><th class="r">OOS R</th><th class="r">OOS trades</th><th class="r">OOS default-params R</th><th class="r">params changed</th></tr></thead><tbody>` +
      p.detail.windows.map(w => `<tr><td>${esc(w.window)}</td><td>${esc(w.is_start)}</td><td>${esc(w.oos_start)} → ${esc(w.oos_end)}</td><td class="r">${sgn(w.is_exp, 3)}</td><td class="r">${sgn(w.oos_exp, 3)}</td><td class="r">${esc(w.oos_trades)}</td><td class="r">${sgn(w.oos_default_exp, 3)}</td><td class="r">${Object.keys(w.chosen || {}).length}</td></tr>`).join('') + `</tbody></table></div>` +
      (m.selection_note ? `<div class="bt-note">${esc(m.selection_note)}</div>` : '');
    if (key === 'sensitivity' && p.detail) {
      if (m.coverage_note) h += `<div class="bt-banner warn">${esc(m.coverage_note)}</div>`;
      h += `<div class="bt-scroll"><table class="bt-t"><thead><tr><th>Parameter</th><th class="r">Base</th><th>Shape</th><th>Neighbours (step → expectancy R)</th></tr></thead><tbody>` +
        (p.detail.params || []).map(r => `<tr><td>${esc(r.param)}${r.internal ? '' : ' <span class="bt-note">(universal)</span>'}</td><td class="r">${esc(r.base)}</td><td><span class="bt-cls ${esc(r.class)}">${esc(r.class)}</span></td><td>${(r.neighbours || []).map(n => `${n.step > 0 ? '+' : ''}${esc(n.step)}: ${n.expectancy_r == null ? '–' : sgn(n.expectancy_r, 2)}`).join(' &nbsp; ')}</td></tr>`).join('') + `</tbody></table></div>` +
        `<div class="bt-note"><b>Regimes</b> (does the plateau survive a regime change?)</div><table class="bt-t"><thead><tr><th>Regime</th><th class="r">Trades</th><th class="r">Expectancy R</th><th class="r">Neighbour median R</th><th></th></tr></thead><tbody>` +
        (p.detail.regimes || []).map(r => `<tr><td>${esc(r.regime)}</td><td class="r">${esc(r.trades)}</td><td class="r">${r.expectancy_r == null ? '–' : sgn(r.expectancy_r, 3)}</td><td class="r">${r.neighbour_median_r == null ? '–' : sgn(r.neighbour_median_r, 3)}</td><td>${!r.evaluated ? '<span class="bt-note">too few trades</span>' : r.survives ? '<span class="bt-ok">survives</span>' : '<span class="bt-bad">breaks</span>'}</td></tr>`).join('') + `</tbody></table>`; }
    return h;
  }
  function activeHTML() {
    const j = st.job; if (!j) return '<div class="panel"><div class="panel-body"><div class="bt-note">No job selected. Run a backtest or click one below.</div></div></div>';
    const ph = j.phases || {}, m = (ph.backtest || {}).metrics || {}, live = j.live || {};
    const cur = st.sel || j.current_phase || [...PH].reverse().map(x => x[0]).find(k => ph[k] && !['pending', 'skipped'].includes(ph[k].status)) || 'data';
    const steps = PH.map(([k, label], i) => { const p = ph[k] || { status: 'pending', progress: 0 };
      return `<div class="bt-step ${esc(p.status)} ${cur === k ? 'sel' : ''}" data-ph="${k}"><div class="n">${i === 0 ? 'Phase 0' : 'Phase ' + i}</div>
        <div class="t"><span>${esc(label)}</span>${pill(p.status)}</div><div class="bt-bar sm"><i style="width:${Math.round((p.status === 'pass' || p.status === 'fail' || p.status === 'inconclusive' ? 1 : p.progress || 0) * 100)}%"></i></div>
        <div class="m">${esc(p.status === 'running' ? (p.message || '') : p.status === 'skipped' ? 'not run' : '')}</div></div>`; }).join('');
    const vcls = j.verdict === 'ROBUST' ? 'ok' : (['NEGATIVE', 'ERROR', 'NO_DATA', 'LOOKAHEAD_DETECTED'].includes(j.verdict) || (j.verdict || '').startsWith('FAILED')) ? 'bad' : 'mid';
    const cn = j.council || {};
    const verdict = j.verdict ? `<div class="bt-verdict ${vcls}"><b>${esc(j.verdict)}</b><span>${esc(j.verdict_detail || '')}</span>${cn.status && cn.status !== 'not_needed' ? `<span class="bt-pill ${cn.status === 'received' ? 'pass' : 'pending'}">council: ${esc(cn.status)}</span>` : ''}</div>` +
      (cn.summary ? `<div class="bt-note"><b>Council:</b> ${esc(cn.summary)}</div>` : '') : '';
    const tiles = [['Trades', live.trades != null ? live.trades : m.trades, 0, false], ['Net R', live.cum_r != null ? live.cum_r : m.total_r, 2, true], ['Expectancy R', m.expectancy_r, 3, true], ['Profit factor', m.profit_factor, 2, false], ['Max DD %', m.max_dd_pct, 2, false], ['Return %', m.return_pct, 2, true]]
      .map(t => `<div class="bt-tile"><div class="l">${t[0]}</div><div class="v">${t[3] ? sgn(t[1], t[2]) : num(t[1], t[2])}</div></div>`).join('');
    return `<div class="panel"><div class="panel-body"><div class="bt-head"><div><div class="bt-title">${esc(j.strategy_name)} ${pill(j.status)}</div>
      <div class="bt-sub">${esc(j.symbol)} · ${esc(j.days)}d · ${esc(j.job_id)}</div></div>
      <div class="bt-overall"><div class="bt-bar"><i style="width:${num(j.overall_progress, 0)}%"></i></div><div class="bt-note">${num(j.overall_progress, 0)}% complete${j.current_phase ? ' · ' + esc(j.current_phase.replace('_', ' ')) : ''}</div></div>
      ${isRunning(j) ? '<button class="bt-btn danger" id="bt-cancel">Cancel</button>' : ''}</div>
      <div class="bt-steps" style="margin-top:12px">${steps}</div>
      <div class="bt-tiles" style="margin-top:12px">${tiles}</div>
      <div class="bt-two" style="margin-top:12px"><div class="bt-chart">${equitySVG(live.equity || m.equity)}</div>
        <div><div class="bt-log">${(j.log || []).slice(-8).map(l => `<div class="${esc(l.level)}">${esc((l.t || '').slice(11, 19))} ${esc(l.msg)}</div>`).join('') || '<div>waiting…</div>'}</div></div></div>
      <div style="margin-top:12px">${verdict}</div><div style="margin-top:10px">${phaseDetail(j, cur)}</div>
      ${j.error && j.status === 'failed' ? `<div class="bt-banner err" style="margin-top:10px">${esc(String(j.error).slice(0, 400))}</div>` : ''}</div></div>`;
  }
  function historyHTML() {
    const rows = st.jobs.map(j => { const s = j.summary || {}, p = j.phases || {};
      const pp = k => pill(p[k] || 'pending', (p[k] || 'pending') === 'pending' ? '·' : p[k]);
      return `<tr class="clk ${st.job && st.job.job_id === j.job_id ? 'sel' : ''}" data-id="${esc(j.job_id)}"><td>${when(j.created_at)}</td><td>${esc(j.strategy_name)}</td><td>${esc(j.symbol)}</td>
        <td>${esc(s.period_first || '')}${s.period_first ? ' → ' + esc(s.period_last) : esc(j.days + 'd')}</td><td class="r">${esc(s.trades != null ? s.trades : '–')}</td>
        <td class="r">${sgn(s.return_pct, 2, '%')}</td><td class="r">${num(s.max_dd_pct, 2)}%</td><td class="r">${num(s.profit_factor, 2)}</td><td class="r">${num(s.sharpe, 2)}</td>
        <td>${pp('backtest')}</td><td>${pp('monte_carlo')}</td><td>${pp('wfo')}</td><td>${pp('sensitivity')}</td><td>${pill(j.status, j.verdict || j.status)}</td></tr>`; }).join('');
    return `<table class="bt-t"><thead><tr><th>Started</th><th>Strategy</th><th>Symbol</th><th>Period</th><th class="r">Trades</th><th class="r">Return</th><th class="r">Max DD</th><th class="r">PF</th><th class="r">Sharpe</th><th>Backtest</th><th>MC</th><th>WFO</th><th>Sens.</th><th>Verdict</th></tr></thead><tbody>${rows || '<tr><td colspan="14" class="bt-note">No backtests yet.</td></tr>'}</tbody></table>`;
  }
  function render() {
    if (!st.mounted) return; cancelAnimationFrame(st.raf);
    st.raf = requestAnimationFrame(() => {
      const b = st.root.querySelector('#bt-banner'); if (!b) return;
      b.innerHTML = st.err ? `<div class="bt-banner err">${esc(st.err)}</div>` : '';
      const a = st.root.querySelector('#bt-active'); a.innerHTML = activeHTML();
      a.querySelectorAll('.bt-step').forEach(e => e.onclick = () => { st.sel = e.dataset.ph; render(); });
      const c = a.querySelector('#bt-cancel'); if (c) c.onclick = cancel;
      const h = st.root.querySelector('#bt-history'); h.innerHTML = historyHTML();
      h.querySelectorAll('tr.clk').forEach(r => r.onclick = () => { st.sel = null; open(r.dataset.id); });
    });
  }

  /* ---------- data flow ---------- */
  async function loadHistory() { try { const r = await api('/api/backtests?limit=15'); st.jobs = r.jobs || []; st.err = null; } catch (e) { st.err = `Backtest API unreachable at ${base() || location.origin} (${e.message}). Showing no data – nothing here is mocked.`; } render(); }
  function stop() { if (st.es) { st.es.close(); st.es = null; } if (st.poll) { clearInterval(st.poll); st.poll = null; } }
  function apply(snap) { st.job = snap; render(); if (snap && TERMINAL.includes(snap.status)) { stop(); loadHistory(); } }
  async function open(id) {
    stop(); try { apply(await api('/api/backtests/' + encodeURIComponent(id))); } catch (e) { st.err = e.message; render(); return; }
    if (!isRunning(st.job)) return;
    try {
      st.es = new EventSource(base() + '/api/backtests/' + encodeURIComponent(id) + '/stream');
      st.es.addEventListener('snapshot', e => { try { apply(JSON.parse(e.data)); } catch (x) { } });
      st.es.addEventListener('end', () => { stop(); loadHistory(); });
      st.es.onerror = () => { if (st.es) { st.es.close(); st.es = null; } startPoll(id); };
    } catch (e) { startPoll(id); }
  }
  function startPoll(id) { if (st.poll) return; st.poll = setInterval(async () => { try { apply(await api('/api/backtests/' + encodeURIComponent(id))); } catch (e) { } }, 2000); }
  async function run() {
    const q = s => st.root.querySelector(s), btn = q('#bt-run'); btn.disabled = true;
    try {
      const r = await api('/api/backtests/run', { method: 'POST', body: JSON.stringify({ strategy_ref: q('#bt-strat').value, symbol: q('#bt-sym').value.trim() || null, days: +q('#bt-days').value, data_mode: q('#bt-mode').value }) });
      st.err = null; st.sel = null; await loadHistory(); await open(r.job_id);
    } catch (e) { st.err = 'Could not start: ' + e.message; render(); } finally { btn.disabled = false; }
  }
  async function cancel() { if (st.job) { try { await api('/api/backtests/' + encodeURIComponent(st.job.job_id) + '/cancel', { method: 'POST' }); } catch (e) { st.err = e.message; render(); } } }
  async function mount(el) {
    st.root = el; st.mounted = true; shell(); render();
    try { st.caps = await api('/api/backtests/capabilities'); fillCaps(); } catch (e) { st.err = `Backtest API unreachable (${e.message}).`; render(); }
    await loadHistory();
    const live = st.jobs.find(isRunning) || st.jobs[0]; if (live) open(live.job_id);
    st.hist = setInterval(() => { if (document.visibilityState !== 'hidden') loadHistory(); }, 10000);
  }
  function unmount() { st.mounted = false; stop(); clearInterval(st.hist); }
  global.BacktestPage = { mount, unmount, _esc: esc, _state: st, _classify: null };
})(window);
