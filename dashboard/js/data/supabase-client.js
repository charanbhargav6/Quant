/**
 * CRAVE Quant Dashboard — Supabase Client
 * ========================================
 * Handles all browser-side Supabase access.
 *
 * SECURITY MODEL
 * ──────────────
 * - URL + anon key fetched from /api/supabase/config (never hardcoded)
 * - Service role key never reaches the browser
 * - All writes blocked by RLS (INSERT policies return false for anon)
 * - All reads allowed by RLS policies on each table
 *
 * DATA SEPARATION
 * ───────────────
 *   REALTIME (Supabase Realtime channels)
 *     crave_system_status  → bot health, heartbeat, connection status
 *     crave_open_positions → live position P&L and price updates
 *     crave_nodes          → multi-node health
 *     crave_alerts         → active risk/news alerts
 *
 *   HISTORICAL (REST queries via Supabase JS SDK)
 *     crave_trades         → trade journal, analytics
 *     crave_daily_pnl      → P&L calendar, equity curve
 *     crave_paper_equity   → paper trading equity curve
 *     crave_portfolio_heat → exposure snapshots
 *     ml_backtest_results  → ML performance
 *
 * USAGE
 * ─────
 *   await SupabaseClient.init();
 *
 *   // Realtime (event-driven)
 *   SupabaseClient.on('system_status', (row) => { ... });
 *   SupabaseClient.on('position_update', (row) => { ... });
 *   SupabaseClient.on('alert', (row) => { ... });
 *
 *   // Historical (async queries)
 *   const trades = await SupabaseClient.getTrades({ days: 30, strategy: 'ema1115' });
 *   const curve  = await SupabaseClient.getEquityCurve({ days: 90 });
 *   const paper  = await SupabaseClient.getPaperEquityCurve({ hours: 24 });
 */

'use strict';

const SupabaseClient = (() => {

  // ── State ────────────────────────────────────────────────────────────────
  let _sb          = null;   // @supabase/supabase-js client
  let _configured  = false;
  let _channels    = {};     // name → RealtimeChannel
  let _handlers    = {};     // event → [fn, ...]
  let _initPromise = null;

  // ── Event emitter ─────────────────────────────────────────────────────────
  function on(event, handler) {
    if (!_handlers[event]) _handlers[event] = [];
    _handlers[event].push(handler);
    return SupabaseClient;
  }

  function off(event, handler) {
    if (!_handlers[event]) return;
    _handlers[event] = _handlers[event].filter(h => h !== handler);
  }

  function _emit(event, data) {
    (_handlers[event] || []).forEach(fn => {
      try { fn(data); } catch (e) { console.error('[SupabaseClient] handler error:', e); }
    });
  }

  // ── Init ──────────────────────────────────────────────────────────────────
  async function init() {
    if (_initPromise) return _initPromise;
    _initPromise = _doInit();
    return _initPromise;
  }

  async function _doInit() {
    // 1. Fetch config from server (anon key only — never hardcoded)
    let cfg;
    try {
      const res = await fetch('/api/supabase/config');
      cfg = await res.json();
    } catch (e) {
      console.warn('[SupabaseClient] Could not reach /api/supabase/config:', e.message);
      return false;
    }

    if (!cfg.configured) {
      console.info('[SupabaseClient] Supabase not configured — skipping. '
        + 'Set SUPABASE_URL + SUPABASE_KEY in .env to enable remote analytics.');
      return false;
    }

    // 2. Load Supabase JS SDK from CDN (lazy — only if configured)
    if (!window.supabase) {
      await _loadScript(
        'https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/dist/umd/supabase.min.js'
      );
    }

    // 3. Create client with anon key (RLS enforced)
    try {
      _sb = window.supabase.createClient(cfg.supabase_url, cfg.anon_key, {
        realtime: { params: { eventsPerSecond: 10 } },
        auth:     { persistSession: false },
      });
      _configured = true;
      console.info('[SupabaseClient] Initialised with anon key ✅');
    } catch (e) {
      console.error('[SupabaseClient] createClient failed:', e);
      return false;
    }

    // 4. Subscribe to realtime tables
    _subscribeRealtime(cfg.realtime_tables || []);

    return true;
  }

  function _loadScript(src) {
    return new Promise((resolve, reject) => {
      if (document.querySelector(`script[src="${src}"]`)) { resolve(); return; }
      const s = document.createElement('script');
      s.src = src;
      s.onload  = resolve;
      s.onerror = reject;
      document.head.appendChild(s);
    });
  }

  // ── Realtime subscriptions ────────────────────────────────────────────────
  function _subscribeRealtime(tables) {
    if (!_sb || !tables.length) return;

    // Quick reachability check: if the Supabase project is paused or DNS fails,
    // skip realtime silently rather than spamming WebSocket errors.
    const wsUrl = _sb.realtime?.endPoint || '';
    if (!wsUrl) {
      _doSubscribeRealtime(tables);
      return;
    }

    // Use a test image fetch as a lightweight DNS probe (no CORS needed)
    const probeUrl = wsUrl.replace('wss://', 'https://').replace('/realtime/v1/websocket', '/rest/v1/');
    fetch(probeUrl, { method: 'HEAD', mode: 'no-cors', signal: AbortSignal.timeout(4000) })
      .then(() => _doSubscribeRealtime(tables))
      .catch(() => {
        console.info('[SupabaseClient] Realtime skipped — Supabase project unreachable (DNS/network). '
          + 'Historical queries still available via REST.');
      });
  }

  function _doSubscribeRealtime(tables) {
    tables.forEach(table => {
      const ch = _sb.channel(`public:${table}`)
        .on('postgres_changes', { event: '*', schema: 'public', table }, payload => {
          _handleRealtimePayload(table, payload);
        })
        .subscribe(status => {
          if (status === 'SUBSCRIBED') {
            console.info(`[SupabaseClient] ✅ Realtime: ${table}`);
          } else if (status === 'CHANNEL_ERROR') {
            console.warn(`[SupabaseClient] ⚠️ Realtime channel error: ${table}`);
          }
        });
      _channels[table] = ch;
    });
  }

  function _handleRealtimePayload(table, payload) {
    const { eventType, new: row, old: oldRow } = payload;

    switch (table) {
      case 'crave_system_status':
        _emit('system_status', { ...row, _event: eventType });
        // Bridge into ConnMgr — update header chip from Supabase heartbeat too
        if (row && row.last_heartbeat) {
          const ageMs = Date.now() - new Date(row.last_heartbeat).getTime();
          if (ageMs < 60_000 && typeof ConnMgr !== 'undefined') {
            // Supabase confirms engine is alive — useful when SSE is down
            ConnMgr._onSupabaseHeartbeat?.(row);
          }
        }
        break;

      case 'crave_open_positions':
        if (eventType === 'DELETE') {
          _emit('position_closed', { trade_id: oldRow?.trade_id });
        } else {
          _emit('position_update', { ...row, _event: eventType });
        }
        break;

      case 'crave_nodes':
        _emit('node_update', { ...row, _event: eventType });
        break;

      case 'crave_alerts':
        _emit('alert', { ...row, _event: eventType });
        break;

      default:
        _emit(table, { ...row, _event: eventType });
    }
  }

  // ── Historical queries ────────────────────────────────────────────────────

  /**
   * Get closed trades.
   * @param {Object} opts
   * @param {number}  [opts.days=30]        - How many days back
   * @param {string}  [opts.strategy]       - Filter by strategy_id
   * @param {string}  [opts.symbol]         - Filter by symbol
   * @param {boolean} [opts.paper=false]    - Paper trades only
   * @param {number}  [opts.limit=200]      - Max rows
   */
  async function getTrades(opts = {}) {
    if (!_configured || !_sb) return [];
    const { days = 30, strategy, symbol, paper = false, limit = 200 } = opts;
    const since = new Date(Date.now() - days * 86_400_000).toISOString();

    try {
      let q = _sb.from('crave_trades')
        .select('*')
        .gte('close_time', since)
        .eq('is_paper', paper)
        .order('close_time', { ascending: false })
        .limit(limit);

      if (strategy) q = q.eq('strategy_id', strategy);
      if (symbol)   q = q.eq('symbol', symbol);

      const { data, error } = await q;
      if (error) throw error;
      return data || [];
    } catch (e) {
      console.warn('[SupabaseClient] getTrades failed:', e.message);
      return [];
    }
  }

  /**
   * Get daily P&L for equity curve.
   * @param {number} [days=90]
   * @param {string} [accountId='mt5']
   */
  async function getEquityCurve(opts = {}) {
    if (!_configured || !_sb) return [];
    const { days = 90, accountId = 'mt5' } = opts;
    const since = new Date(Date.now() - days * 86_400_000).toISOString().slice(0, 10);

    try {
      const { data, error } = await _sb.from('crave_daily_pnl')
        .select('session_date,close_equity,pnl_dollars,pnl_pct,trades_count,wins,losses,max_drawdown_pct,circuit_breaker_fired')
        .eq('account_id', accountId)
        .gte('session_date', since)
        .order('session_date', { ascending: true });

      if (error) throw error;
      return data || [];
    } catch (e) {
      console.warn('[SupabaseClient] getEquityCurve failed:', e.message);
      return [];
    }
  }

  /**
   * Get paper trading equity curve (intra-day snapshots).
   * @param {number} [hours=24]
   */
  async function getPaperEquityCurve(opts = {}) {
    if (!_configured || !_sb) return [];
    const { hours = 24 } = opts;
    const since = new Date(Date.now() - hours * 3_600_000).toISOString();

    try {
      const { data, error } = await _sb.from('crave_paper_equity')
        .select('ts,equity,balance,floating_pnl,drawdown_pct,open_positions')
        .gte('ts', since)
        .order('ts', { ascending: true });

      if (error) throw error;
      return data || [];
    } catch (e) {
      console.warn('[SupabaseClient] getPaperEquityCurve failed:', e.message);
      return [];
    }
  }

  /**
   * Get current open positions (initial load; realtime updates after that).
   */
  async function getOpenPositions() {
    if (!_configured || !_sb) return [];
    try {
      const { data, error } = await _sb.from('crave_open_positions')
        .select('*')
        .order('open_time', { ascending: false });
      if (error) throw error;
      return data || [];
    } catch (e) {
      console.warn('[SupabaseClient] getOpenPositions failed:', e.message);
      return [];
    }
  }

  /**
   * Get latest bot status.
   */
  async function getSystemStatus() {
    if (!_configured || !_sb) return null;
    try {
      const { data, error } = await _sb.from('crave_system_status')
        .select('*')
        .order('updated_at', { ascending: false })
        .limit(1)
        .single();
      if (error && error.code !== 'PGRST116') throw error;
      return data || null;
    } catch (e) {
      console.warn('[SupabaseClient] getSystemStatus failed:', e.message);
      return null;
    }
  }

  /**
   * Get latest portfolio heat snapshot.
   */
  async function getPortfolioHeat() {
    if (!_configured || !_sb) return null;
    try {
      const { data, error } = await _sb.from('crave_portfolio_heat')
        .select('*')
        .order('snapshot_ts', { ascending: false })
        .limit(1)
        .single();
      if (error && error.code !== 'PGRST116') throw error;
      if (!data) return null;
      // Parse JSONB arrays
      return {
        ...data,
        items:       typeof data.items === 'string'      ? JSON.parse(data.items)       : data.items,
        corr_groups: typeof data.corr_groups === 'string'? JSON.parse(data.corr_groups) : data.corr_groups,
      };
    } catch (e) {
      console.warn('[SupabaseClient] getPortfolioHeat failed:', e.message);
      return null;
    }
  }

  /**
   * Get node health list.
   */
  async function getNodes() {
    if (!_configured || !_sb) return [];
    try {
      const { data, error } = await _sb.from('crave_nodes').select('*');
      if (error) throw error;
      return data || [];
    } catch (e) {
      console.warn('[SupabaseClient] getNodes failed:', e.message);
      return [];
    }
  }

  /**
   * Get active alerts.
   */
  async function getActiveAlerts() {
    if (!_configured || !_sb) return [];
    try {
      const { data, error } = await _sb.from('crave_alerts')
        .select('*')
        .eq('is_active', true)
        .order('ts', { ascending: false })
        .limit(50);
      if (error) throw error;
      return data || [];
    } catch (e) {
      console.warn('[SupabaseClient] getActiveAlerts failed:', e.message);
      return [];
    }
  }

  /**
   * Get ML backtest results.
   * @param {number} [limit=20]
   */
  async function getMlBacktestResults(opts = {}) {
    if (!_configured || !_sb) return [];
    const { limit = 20 } = opts;
    try {
      const { data, error } = await _sb.from('ml_backtest_results')
        .select('*')
        .order('created_at', { ascending: false })
        .limit(limit);
      if (error) throw error;
      return data || [];
    } catch (e) {
      console.warn('[SupabaseClient] getMlBacktestResults failed:', e.message);
      return [];
    }
  }

  /**
   * Analytics: P&L by strategy (aggregated from crave_trades).
   * @param {number} [days=90]
   */
  async function getPnlByStrategy(opts = {}) {
    if (!_configured || !_sb) return [];
    const { days = 90 } = opts;
    const since = new Date(Date.now() - days * 86_400_000).toISOString();
    try {
      // Supabase doesn't support GROUP BY directly — fetch and aggregate client-side
      const { data, error } = await _sb.from('crave_trades')
        .select('strategy_id,profit_dollars,r_multiple,outcome')
        .gte('close_time', since)
        .eq('is_paper', false);
      if (error) throw error;

      // Aggregate
      const map = {};
      (data || []).forEach(t => {
        const sid = t.strategy_id || 'Unknown';
        if (!map[sid]) map[sid] = { strategy_id: sid, pnl: 0, trades: 0, wins: 0, total_r: 0 };
        map[sid].pnl     += (t.profit_dollars || 0);
        map[sid].total_r += (t.r_multiple || 0);
        map[sid].trades  += 1;
        if ((t.r_multiple || 0) > 0) map[sid].wins += 1;
      });

      return Object.values(map).map(s => ({
        ...s,
        pnl:         Math.round(s.pnl * 100) / 100,
        win_rate:    s.trades > 0 ? Math.round(s.wins / s.trades * 1000) / 10 : 0,
        avg_r:       s.trades > 0 ? Math.round(s.total_r / s.trades * 1000) / 1000 : 0,
      })).sort((a, b) => b.pnl - a.pnl);
    } catch (e) {
      console.warn('[SupabaseClient] getPnlByStrategy failed:', e.message);
      return [];
    }
  }

  // ── Status helpers ────────────────────────────────────────────────────────
  function isConfigured() { return _configured; }

  function getChannelStatus() {
    const out = {};
    Object.entries(_channels).forEach(([name, ch]) => {
      out[name] = ch.state || 'unknown';
    });
    return out;
  }

  // ── Public API ────────────────────────────────────────────────────────────
  return {
    init,
    on,
    off,
    isConfigured,
    getChannelStatus,
    // Realtime (subscriptions handle updates — these are initial loads)
    getOpenPositions,
    getSystemStatus,
    getActiveAlerts,
    getNodes,
    // Historical
    getTrades,
    getEquityCurve,
    getPaperEquityCurve,
    getPortfolioHeat,
    getPnlByStrategy,
    getMlBacktestResults,
  };
})();

window.SupabaseClient = SupabaseClient;

// Auto-init on page load (non-blocking)
document.addEventListener('DOMContentLoaded', () => {
  SupabaseClient.init().then(ok => {
    if (!ok) return;

    // Bridge Supabase position updates → UI position table row flash
    SupabaseClient.on('position_update', (row) => {
      // Re-emit as ConnMgr position event so the same handler runs
      if (typeof ConnMgr !== 'undefined') {
        document.dispatchEvent(new CustomEvent('crave:position', { detail: row }));
      }
    });

    // Bridge system status → ConnMgr staleness check
    SupabaseClient.on('system_status', (row) => {
      if (row.circuit_breaker && typeof _showToast !== 'undefined') {
        _showToast('⚡ Circuit breaker ACTIVE (Supabase)', 'error', 0);
      }
    });

    // Bridge active alerts → toast
    SupabaseClient.on('alert', (row) => {
      if (row.is_active && row.level === 'critical' && typeof _showToast !== 'undefined') {
        _showToast(`🔴 ${row.text}`, 'error', 0);
      } else if (row.is_active && row.level === 'warning' && typeof _showToast !== 'undefined') {
        _showToast(`⚠️ ${row.text}`, 'warn', 10000);
      }
    });
  });
});
