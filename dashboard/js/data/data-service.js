/**
 * CRAVE Quant Dashboard — Data Service
 * =====================================
 * Central data layer. Routes all calls to either MockProvider or ApiProvider.
 *
 * ┌─────────────────────────────────────────────────────┐
 * │  TO SWITCH TO REAL DATA:                            │
 * │  Change DATA_MODE below from 'mock' to 'api'        │
 * │  OR set window.CRAVE_DATA_MODE = 'api' before load  │
 * └─────────────────────────────────────────────────────┘
 *
 * The UI never talks to MockProvider or ApiProvider directly.
 * All page code calls DataService.getX() — the provider is opaque.
 */

'use strict';

// ── Mode configuration ──────────────────────────────────────
// 'mock' = use MockProvider (development, no engine needed)
// 'api'  = use ApiProvider  (production, real quant_server.py)
const DATA_MODE = window.CRAVE_DATA_MODE || 'mock';

const DataService = {

  _mode: DATA_MODE,
  _provider: null,

  // Error handler — called when a provider call fails
  _onError: null,

  // Staleness tracker — per-endpoint last-fetch timestamps
  _lastFetch: {},
  _cache: {},
  _cacheTTL: {
    getCommandCenter: 2_000,
    getPortfolio:     5_000,
    getAccounts:      10_000,
    getMarkets:       3_000,
    getExecution:     2_000,
    getRisk:          5_000,
    getIntelligence:  5_000,
    getCompliance:    30_000,
    getAdmin:         10_000,
    getNews:          120_000,
    getStrategies:    15_000,
    getEquityCurve:   60_000,
    getPnLCalendar:   60_000,
    getOptions:       10_000,
    getBotStatus:     4_000,   // 4s — bot status
    getReconcile:     30_000,  // 30s — reconciliation (expensive)
    getIndia:         5_000,   // 5s — india market data
  },

  init() {
    this._provider = DATA_MODE === 'api' ? window.ApiProvider : window.MockProvider;
    console.log(`[DataService] Mode: ${DATA_MODE} | Provider:`, this._provider);
    return this;
  },

  isMock() { return this._mode === 'mock'; },
  isLive()  { return this._mode === 'api'; },

  // ── Cache-aware fetch ───────────────────────────────────────
  async _fetch(method, ...args) {
    const key = method + JSON.stringify(args);
    const ttl = this._cacheTTL[method] || 5_000;
    const now = Date.now();
    if (this._cache[key] && now - this._lastFetch[key] < ttl) {
      return this._cache[key];
    }
    try {
      const data = await this._provider[method](...args);
      this._cache[key] = data;
      this._lastFetch[key] = now;
      return data;
    } catch (err) {
      console.error(`[DataService] ${method} failed:`, err);
      if (this._onError) this._onError(method, err);
      // Return stale cache if available
      if (this._cache[key]) return { ...this._cache[key], _stale: true };
      throw err;
    }
  },

  // ── Freshness helpers ───────────────────────────────────────
  getDataAge(method) {
    const last = this._lastFetch[method];
    if (!last) return null;
    return Date.now() - last;
  },

  getConnectionStatus() {
    const age = this.getDataAge('getCommandCenter');
    if (age === null) return 'disconnected';
    if (age < 5_000)  return 'live';
    if (age < 15_000) return 'stale';
    return 'reconnecting';
  },

  // ── Invalidate cache ────────────────────────────────────────
  invalidate(method) {
    const prefix = method || '';
    for (const key of Object.keys(this._cache)) {
      if (!prefix || key.startsWith(prefix)) {
        delete this._cache[key];
        delete this._lastFetch[key];
      }
    }
  },

  // ── Public API — mirrors provider interface ─────────────────

  async getCommandCenter()  { return this._fetch('getCommandCenter'); },
  async getPortfolio()      { return this._fetch('getPortfolio'); },
  async getAccounts()       { return this._fetch('getAccounts'); },

  async getAccountDetail(id) {
    return this._provider.getAccountDetail
      ? this._provider.getAccountDetail(id)
      : this._fetch('getAccounts');
  },

  async getStrategies()     { return this._fetch('getStrategies'); },

  async getStrategyDetail(sid) {
    return this._provider.getStrategyDetail
      ? this._provider.getStrategyDetail(sid)
      : {};
  },

  async getMarkets()        { return this._fetch('getMarkets'); },
  async getExecution()      { return this._fetch('getExecution'); },
  async getRisk()           { return this._fetch('getRisk'); },
  async getIntelligence()   { return this._fetch('getIntelligence'); },
  async getCompliance()     { return this._fetch('getCompliance'); },
  async getAdmin()          { return this._fetch('getAdmin'); },
  async getNews()           { return this._fetch('getNews'); },
  async getEquityCurve(days){ return this._fetch('getEquityCurve', days); },
  async getPnLCalendar()    { return this._fetch('getPnLCalendar'); },
  async getOptions()        { return this._fetch('getOptions'); },

  // Phase 2/3/4 new endpoints
  async getBotStatus()      { return this._fetch('getBotStatus'); },
  async getReconcile()      { return this._fetch('getReconcile'); },
  async getIndia()          { return this._fetch('getIndia'); },

  // SSE stream URL — used by conn-mgr to connect to correct host:port
  getStreamUrl() {
    if (this._provider.getStreamUrl) return this._provider.getStreamUrl();
    // Fallback: same origin
    const base = window.CRAVE_API_BASE || '';
    return base + '/api/stream';
  },

  // ── Write methods (not cached) ──────────────────────────────
  async sendChat(message, context) {
    return this._provider.sendChat(message, context);
  },

  async pushCouncilMessage(agent, text) {
    if (this._provider.pushCouncilMessage) {
      return this._provider.pushCouncilMessage(agent, text);
    }
    return { ok: true, _mock: true };
  },

  async getOptimizerProposal() {
    if (this._provider.getOptimizerProposal) {
      return this._provider.getOptimizerProposal();
    }
    return { status: 'ready', proposal: null, _mock: true };
  },

  // ── Formatting utilities ────────────────────────────────────

  fmt: {
    currency(v, currency = 'USD', compact = false) {
      if (v === null || v === undefined) return '—';
      const abs = Math.abs(v);
      if (compact && abs >= 1_000_000) return (v < 0 ? '-' : '') + '$' + (abs / 1_000_000).toFixed(2) + 'M';
      if (compact && abs >= 1_000)     return (v < 0 ? '-' : '') + '$' + (abs / 1_000).toFixed(1) + 'K';
      return new Intl.NumberFormat('en-US', { style: 'currency', currency, maximumFractionDigits: 2 }).format(v);
    },

    pct(v, decimals = 2) {
      if (v === null || v === undefined) return '—';
      const sign = v > 0 ? '+' : '';
      return `${sign}${(+v).toFixed(decimals)}%`;
    },

    pnlClass(v) {
      if (v > 0) return 'positive';
      if (v < 0) return 'negative';
      return 'neutral';
    },

    pnlColor(v) {
      if (v > 0) return 'text-green';
      if (v < 0) return 'text-red';
      return 'text-secondary';
    },

    number(v, decimals = 2) {
      if (v === null || v === undefined) return '—';
      return (+v).toFixed(decimals);
    },

    price(v, decimals = 5) {
      if (!v) return '—';
      const d = v >= 1000 ? 2 : v >= 10 ? 3 : 5;
      return (+v).toFixed(Math.min(decimals, d));
    },

    time(isoStr) {
      if (!isoStr) return '—';
      return isoStr.slice(0, 16).replace('T', ' ');
    },

    ago(isoStr) {
      if (!isoStr) return '—';
      const diff = (Date.now() - new Date(isoStr).getTime()) / 1000;
      if (diff < 60) return `${Math.floor(diff)}s ago`;
      if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
      if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
      return `${Math.floor(diff / 86400)}d ago`;
    },

    regime(r) {
      const map = { TRENDING: 'badge-blue', RANGING: 'badge-amber', VOLATILE: 'badge-red', '—': 'badge-muted' };
      return `<span class="badge ${map[r] || 'badge-muted'}">${r || '—'}</span>`;
    },

    bias(b) {
      const map = { BUY: 'badge-green', SELL: 'badge-red', NO_TRADE: 'badge-muted' };
      return `<span class="badge ${map[b] || 'badge-muted'}">${b || '—'}</span>`;
    },

    direction(d) {
      return `<span class="dir-badge ${d?.toLowerCase()}">${(d || '').toUpperCase()}</span>`;
    },

    liveStatus(status) {
      const map = { live: '🟢', stale: '🟡', reconnecting: '🟠', disconnected: '🔴' };
      return map[status] || '⚪';
    },
  },
};

// Init immediately
DataService.init();
window.DS = DataService;
