/**
 * CRAVE Quant Dashboard — Real API Provider
 * ==========================================
 * Calls the real quant_server.py Flask API.
 * Base URL is configurable — defaults to same origin (local Flask)
 * or can be set to a remote proxy URL.
 *
 * All methods match MockProvider's interface exactly.
 * Swap providers in data-service.js by changing DATA_MODE.
 */

'use strict';

const ApiProvider = {

  // ── Config ─────────────────────────────────────────────────
  // BASE_URL: '' means same origin (Flask serves dashboard from same server)
  // If served from a separate static server (e.g. python -m http.server 9090),
  // auto-detect and point to the real API at port 8765.
  _detectBase() {
    if (window.CRAVE_API_BASE) return window.CRAVE_API_BASE;
    const port = parseInt(window.location.port || '80', 10);
    // Common static-server ports that don't run the Flask API
    if (port === 9090 || port === 5500 || port === 3000 || port === 8080) {
      return window.location.protocol + '//' + window.location.hostname + ':8765';
    }
    return ''; // Same origin — Flask is serving the dashboard
  },

  get BASE_URL() { return this._detectBase(); },

  // ── Core fetch helper ──────────────────────────────────────
  async _get(path, timeoutMs = 20_000) {
    const res = await fetch(`${this.BASE_URL}${path}`, {
      method: 'GET',
      headers: { 'Content-Type': 'application/json' },
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (!res.ok) throw new Error(`API ${path} → HTTP ${res.status}`);
    return res.json();
  },

  async _post(path, body) {
    const res = await fetch(`${this.BASE_URL}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(30_000),
    });
    if (!res.ok) throw new Error(`API POST ${path} → HTTP ${res.status}`);
    return res.json();
  },

  // Active account — updated by accountChanged event from account-selector.js
  _activeAccount: 'ALL',

  // Account-scoped fetch: appends ?account=ID when a specific account is active
  async _getScoped(path) {
    const acct = this._activeAccount;
    if (acct && acct !== 'ALL') {
      const sep = path.includes('?') ? '&' : '?';
      return this._get(`${path}${sep}account=${encodeURIComponent(acct)}`);
    }
    return this._get(path);
  },


  // ── Endpoint implementations ───────────────────────────────
  async getCommandCenter() {
    return this._get('/api/command_center');
  },

  async getPortfolio() {
    return this._getScoped('/api/portfolio');
  },

  async getAccounts() {
    return this._get('/api/accounts');
  },

  async getAccountDetail(accountId) {
    return this._get(`/api/accounts/${accountId}`);
  },

  async getStrategies() {
    return this._get('/api/strategies');
  },

  async getStrategyDetail(sid) {
    return this._get(`/api/strategies/${sid}/detail`);
  },

  async getMarkets() {
    return this._get('/api/markets');
  },

  async getExecution() {
    return this._getScoped('/api/execution');
  },

  async getRisk() {
    return this._getScoped('/api/risk');
  },

  async getIntelligence() {
    return this._get('/api/intelligence');
  },

  async getCompliance() {
    return this._get('/api/compliance');
  },

  async getAdmin() {
    return this._get('/api/admin');
  },

  async getNews() {
    return this._get('/api/news');
  },

  async sendChat(message, context) {
    return this._post('/api/chat', { message, context });
  },

  async pushCouncilMessage(agent, text) {
    return this._post('/api/intelligence/council', { agent, text });
  },

  async getOptimizerProposal() {
    return this._get('/api/optimizer/propose');
  },

  async approveOptimizer(proposalId) {
    return this._post('/api/optimizer/approve', { proposal_id: proposalId });
  },

  async getPropFirms() {
    return this._get('/api/prop_firms');
  },

  async ping() {
    return this._get('/api/ping');
  },

  // ── New Phase 2/3/4 endpoints ─────────────────────────────────
  async getBotStatus() {
    return this._get('/api/bot_status');
  },

  async getReconcile() {
    return this._get('/api/reconcile');
  },

  async getIndia() {
    return this._get('/api/india');
  },

  async getOptions() {
    return this._get('/api/options');
  },

  // SSE stream URL (used by conn-mgr.js)
  getStreamUrl() {
    return `${this.BASE_URL}/api/stream`;
  },

  // ── Derived / analytics endpoints ─────────────────────────────
  async getEquityCurve(days = 30) {
    try {
      return await this._get(`/api/analytics/equity_curve?days=${days}`);
    } catch {
      // Endpoint pending — return empty safe shape
      return { data: [], _note: 'equity_curve endpoint pending', _live: true };
    }
  },

  async getPnLCalendar() {
    try {
      return await this._get('/api/analytics/pnl_calendar');
    } catch {
      return { cells: [], _note: 'pnl_calendar endpoint pending', _live: true };
    }
  },
};


window.ApiProvider = ApiProvider;

// ── Listen for account changes from account-selector.js ──────────────────
// When user switches account, update _activeAccount so next API
// call automatically includes ?account= scoping.
window.addEventListener('accountChanged', function(e) {
  if (window.ApiProvider) {
    window.ApiProvider._activeAccount = (e.detail && e.detail.id) ? e.detail.id : 'ALL';
    console.log('[ApiProvider] Active account:', window.ApiProvider._activeAccount);
  }
});
