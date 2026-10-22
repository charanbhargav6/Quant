/**
 * CRAVE Quant Dashboard — Mock Data Provider
 * =============================================
 * Returns realistic-looking mock data for all API endpoints.
 * Clearly separated from the real API provider.
 *
 * TO SWITCH TO REAL DATA: Change DATA_MODE = 'api' in data-service.js
 *
 * IMPORTANT: This file must never be imported in production.
 * All mock data is clearly labelled to prevent confusion.
 */

'use strict';

const MockProvider = {

  // ── Helper: add noise to a base value ──────────────────────
  _jitter(base, pct = 0.02) {
    return base * (1 + (Math.random() - 0.5) * 2 * pct);
  },

  _rand(min, max) {
    return Math.random() * (max - min) + min;
  },

  _pick(arr) {
    return arr[Math.floor(Math.random() * arr.length)];
  },

  // ── /api/command_center ─────────────────────────────────────
  async getCommandCenter() {
    await this._delay(60);
    const equity = 52_847.33;
    return {
      _mock: true,
      connection: { mt5: false, ts: Date.now() / 1000 },
      equity:          this._jitter(equity),
      balance:         this._jitter(50_000),
      today_pnl:       this._jitter(1_247.80),
      unrealized:      this._jitter(892.40),
      open_positions:  3,
      alerts: [
        { level: 'amber', text: 'USD — FOMC Minutes in 45 min · high impact · entries auto-paused' },
        { level: 'green', text: 'Circuit breakers: armed — all systems nominal' },
      ],
      top_movers: [
        { symbol: 'BTCUSDT',  price: 68_420.10, day_pnl:  412.80, direction: 'buy' },
        { symbol: 'EURUSD=X', price: 1.08_42,   day_pnl:  189.30, direction: 'buy' },
        { symbol: 'XAUUSD=X', price: 2_648.20,  day_pnl: -78.40,  direction: 'sell' },
      ],
      performance: {
        win_rate:      61.4,
        profit_factor: 2.18,
        expectancy_r:  0.417,
        total_trades:  87,
      },
      ping_ms:    12,
      server_time: new Date().toISOString().slice(11, 19) + ' UTC',
    };
  },

  // ── /api/portfolio ─────────────────────────────────────────
  async getPortfolio() {
    await this._delay(80);
    return {
      _mock: true,
      connection: { mt5: false },
      accounts: [
        {
          name: 'MT5 — XM-Live01', id: '84723901', type: 'real',
          balance: 50_000, equity: 52_847, profit: 2847, currency: 'USD',
          free_margin: 48_200, margin: 1847, margin_level: 286.2, leverage: 100,
        },
        {
          name: 'Paper Engine', id: 'PAPER', type: 'paper',
          balance: 10_000, equity: 11_423, profit: 1423, currency: 'USD',
        },
        {
          name: 'MT5 — FTMO-Demo', id: '98012344', type: 'prop_firm',
          balance: 100_000, equity: 103_210, profit: 3210, currency: 'USD',
          free_margin: 99_800, margin: 200, margin_level: 1200, leverage: 30,
        },
      ],
      total_equity: 167_480,
      exposure: [
        { symbol: 'BTCUSDT', pct: 45.2 },
        { symbol: 'EURUSD=X', pct: 32.1 },
        { symbol: 'XAUUSD=X', pct: 22.7 },
      ],
      db_stats: {
        live:  { trades: 54,  win_rate: 61.4 },
        paper: { trades: 128, win_rate: 58.2 },
      },
    };
  },

  // ── /api/accounts ──────────────────────────────────────────
  async getAccounts() {
    await this._delay(50);
    return {
      _mock: true,
      connection: { mt5: false },
      accounts: [
        {
          id: '84723901', name: 'XM-Live01', type: 'real',
          balance: 50_000, equity: 52_847, profit: 2847, currency: 'USD',
          free_margin: 48_200, margin: 1847, margin_level: 286.2, leverage: 100,
          open_positions: 2, connected: false, active: false,
          server: 'XM-Live01',
          today_pnl: 1247.80, overall_pnl: 2847,
          drawdown_pct: 1.8, risk_status: 'ok',
          strategies_enabled: ['orderflow_btc', 'trend_pa_forex'],
        },
        {
          id: 'PAPER', name: 'Paper Engine', type: 'paper',
          balance: 10_000, equity: 11_423, profit: 1423, currency: 'USD',
          open_positions: 1, connected: true, active: true,
          today_pnl: 84.20, overall_pnl: 1423, drawdown_pct: 0.4, risk_status: 'ok',
          strategies_enabled: ['all'],
        },
        {
          id: '98012344', name: 'FTMO Challenge', type: 'prop_firm',
          balance: 100_000, equity: 103_210, profit: 3210, currency: 'USD',
          free_margin: 99_800, margin: 200, margin_level: 1200, leverage: 30,
          open_positions: 0, connected: false, active: false,
          today_pnl: 0, overall_pnl: 3210, drawdown_pct: 0.8, risk_status: 'ok',
          strategies_enabled: ['trend_pa_forex', 'orderflow_btc'],
        },
      ],
      active_login: 'PAPER',
    };
  },

  // ── /api/strategies ────────────────────────────────────────
  async getStrategies() {
    await this._delay(70);
    return {
      _mock: true,
      strategies: [
        {
          id: 'orderflow_btc',
          name: 'OrderFlow — Crypto (BTC/ETH/SOL)',
          description: 'Pure Order Flow footprint proxy. POC/VAH/VAL levels with delta confirmation.',
          instruments: ['BTC-USD', 'ETH-USD', 'SOL-USD'],
          timeframe: 'M15', style: 'Intraday',
          live_ready: true, paper_only: false,
          eligible: 'Live Ready',
          trades_db: 34,
          static_backtest: { win_rate: 49.9, expectancy_r: 0.417, expectancy_std: 0.215, rr: 2.0 },
          max_drawdown: -12.4,
          today_pnl: 412.80, overall_pnl: 2840,
          last_trade: '2026-10-03 18:42',
        },
        {
          id: 'trend_pa_forex',
          name: 'Trend/Price-Action — Forex (EURUSD)',
          description: 'EMA50/200 trend filter + ADX + EMA21 pullback entries. STABLE WFO verdict.',
          instruments: ['EURUSD=X'],
          timeframe: 'M15', style: 'Intraday',
          live_ready: true, paper_only: false,
          eligible: 'Live Ready',
          trades_db: 20,
          static_backtest: { win_rate: 36.1, expectancy_r: 0.191, expectancy_std: 0.095, rr: 2.0 },
          max_drawdown: -8.7,
          today_pnl: 189.30, overall_pnl: 940,
          last_trade: '2026-10-03 14:15',
        },
        {
          id: 'trend_pa_gold',
          name: 'Trend/Price-Action — Gold (GC=F)',
          description: 'EMA trend filter + Keltner/RSI pullback on Gold. INCONSISTENT WFO — paper only.',
          instruments: ['XAUUSD=X'],
          timeframe: 'M15', style: 'Intraday',
          live_ready: false, paper_only: true,
          eligible: 'Backtest Data',
          trades_db: 0,
          static_backtest: { win_rate: 32.2, expectancy_r: 0.112, expectancy_std: 0.134, rr: 2.0 },
          max_drawdown: -18.3,
          today_pnl: -78.40, overall_pnl: -320,
          last_trade: '2026-10-02 11:30',
        },
        {
          id: 'structure_silver',
          name: 'Structure (SMC/ICT) — Silver',
          description: 'FVG, Order Block, liquidity sweep, BOS/CHoCH on Silver. INCONSISTENT — under review.',
          instruments: ['XAGUSD=X'],
          timeframe: 'M15', style: 'Intraday',
          live_ready: false, paper_only: true,
          eligible: 'Backtest Data',
          trades_db: 0,
          static_backtest: { win_rate: 31.7, expectancy_r: -0.080, expectancy_std: 0.148, rr: 2.0 },
          max_drawdown: -22.1,
          today_pnl: 0, overall_pnl: -180,
          last_trade: '2026-10-01 09:00',
        },
        {
          id: 'mean_reversion_ranging',
          name: 'Mean Reversion — Ranging Regime',
          description: 'Bollinger-band extreme + RSI/volume exhaustion. Staged for WFO — not live-ready.',
          instruments: [],
          timeframe: 'M15', style: 'Intraday',
          live_ready: false, paper_only: true,
          eligible: 'Backtest Data',
          trades_db: 0,
          static_backtest: { win_rate: null, expectancy_r: null, rr: 1.5 },
          max_drawdown: null,
          today_pnl: 0, overall_pnl: 0,
          last_trade: null,
        },
      ],
    };
  },

  // ── /api/markets ───────────────────────────────────────────
  async getMarkets() {
    await this._delay(80);
    return {
      _mock: true,
      connection: { mt5: false },
      matrix: [
        { symbol: 'EURUSD=X', label: 'EUR/USD', price: 1.08423, bid: 1.08421, ask: 1.08425, spread: 0.4, change: 0.142, bias: 'BUY', regime: 'TRENDING' },
        { symbol: 'GBPUSD=X', label: 'GBP/USD', price: 1.31847, bid: 1.31844, ask: 1.31850, spread: 0.6, change: -0.089, bias: 'SELL', regime: 'RANGING' },
        { symbol: 'USDJPY=X', label: 'USD/JPY', price: 149.823, bid: 149.820, ask: 149.826, spread: 0.6, change: 0.214, bias: 'BUY', regime: 'TRENDING' },
        { symbol: 'AUDUSD=X', label: 'AUD/USD', price: 0.64821, bid: 0.64819, ask: 0.64823, spread: 0.4, change: -0.312, bias: 'SELL', regime: 'RANGING' },
        { symbol: 'XAUUSD=X', label: 'Gold', price: 2648.20, bid: 2648.10, ask: 2648.30, spread: 2.0, change: -0.189, bias: 'NO_TRADE', regime: 'VOLATILE' },
        { symbol: 'XAGUSD=X', label: 'Silver', price: 31.842, bid: 31.838, ask: 31.846, spread: 0.4, change: 0.321, bias: 'NO_TRADE', regime: 'RANGING' },
        { symbol: 'BTCUSDT',  label: 'BTC/USDT', price: 68420.10, bid: 68418, ask: 68422, spread: 4.0, change: 1.842, bias: 'BUY', regime: 'TRENDING' },
        { symbol: 'ETHUSDT',  label: 'ETH/USDT', price: 2641.80, bid: 2641.50, ask: 2642.10, spread: 0.6, change: 0.924, bias: 'BUY', regime: 'TRENDING' },
        { symbol: 'SOLUSDT',  label: 'SOL/USDT', price: 168.42, bid: 168.40, ask: 168.44, spread: 0.02, change: 2.140, bias: 'BUY', regime: 'TRENDING' },
        { symbol: 'NIFTY_FUT', label: 'NIFTY 50 Fut', price: 26084, bid: 26083, ask: 26085, spread: 2.0, change: -0.412, bias: 'NO_TRADE', regime: 'RANGING' },
        { symbol: 'BANKNIFTY_FUT', label: 'BANK NIFTY Fut', price: 53842, bid: 53840, ask: 53844, spread: 4.0, change: -0.284, bias: 'NO_TRADE', regime: 'RANGING' },
      ],
      news: [
        { title: 'FOMC Meeting Minutes', currency: 'USD', impact: 'high', time_utc: '18:00 UTC', time_ist: '23:30 IST', diff_min: 45, soon: true },
        { title: 'US Initial Jobless Claims', currency: 'USD', impact: 'medium', time_utc: '12:30 UTC', time_ist: '18:00 IST', diff_min: -210, soon: false },
        { title: 'ECB Economic Bulletin', currency: 'EUR', impact: 'medium', time_utc: '09:00 UTC', time_ist: '14:30 IST', diff_min: -540, soon: false },
      ],
    };
  },

  // ── /api/execution ─────────────────────────────────────────
  async getExecution() {
    await this._delay(70);
    return {
      _mock: true,
      connection: { mt5: false },
      orders: [
        {
          id: 48291042, symbol: 'BTCUSDT', direction: 'buy', volume: 0.05,
          entry: 67_842.10, current: 68_420.10, sl: 67_200, tp: 69_600,
          profit: 289.20, profit_dollars: 289.20, profit_pct: 0.57,
          swap: -0.42, pips_moved: 578.0,
          open_time: '2026-10-03 10:22', comment: 'orderflow_btc A+',
          strategy_id: 'OrderFlow — Crypto',
        },
        {
          id: 48291106, symbol: 'EURUSD=X', direction: 'buy', volume: 1.0,
          entry: 1.08241, current: 1.08423, sl: 1.07980, tp: 1.09100,
          profit: 182.00, profit_dollars: 182.00, profit_pct: 0.36,
          swap: 0.12, pips_moved: 18.2,
          open_time: '2026-10-03 12:08', comment: 'trend_pa_forex A',
          strategy_id: 'Trend/PA — Forex',
        },
        {
          id: 48291218, symbol: 'XAUUSD=X', direction: 'sell', volume: 0.10,
          entry: 2654.40, current: 2648.20, sl: 2665.00, tp: 2630.00,
          profit: 62.00, profit_dollars: 62.00, profit_pct: 0.12,
          swap: -1.20, pips_moved: 62.0,
          open_time: '2026-10-03 14:52', comment: 'Manual',
          strategy_id: 'Manual',
        },
      ],
      closed_trades: this._generateClosedTrades(30),
      fill_stats: { total_live: 54, fill_rate: 78.4 },
    };
  },

  _generateClosedTrades(n) {
    const symbols = ['BTCUSDT', 'EURUSD=X', 'XAUUSD=X', 'ETHUSDT', 'GBPUSD=X', 'SOLUSDT'];
    const strategies = ['OrderFlow — Crypto', 'Trend/PA — Forex', 'Manual', 'Trend/PA — Gold'];
    const trades = [];
    for (let i = 0; i < n; i++) {
      const pnl = (Math.random() - 0.38) * 400;
      const days = Math.floor(Math.random() * 30);
      const d = new Date(); d.setDate(d.getDate() - days);
      trades.push({
        id: 48290000 + i,
        symbol: this._pick(symbols),
        direction: Math.random() > 0.5 ? 'buy' : 'sell',
        volume: +(Math.random() * 0.5 + 0.01).toFixed(2),
        entry: 0,
        exit: 0,
        profit_dollars: +pnl.toFixed(2),
        profit_pct: +(pnl / 500 * 100).toFixed(2),
        strategy_id: this._pick(strategies),
        close_time: d.toISOString().slice(0, 16).replace('T', ' '),
      });
    }
    return trades.sort((a, b) => b.close_time.localeCompare(a.close_time));
  },

  // ── /api/risk ──────────────────────────────────────────────
  async getRisk() {
    await this._delay(60);
    return {
      _mock: true,
      connection: { mt5: false },
      daily_loss_pct: 0.82,
      daily_loss_limit: 4.0,
      overall_loss: 1_284.40,
      corr_exposure_pct: 33.3,
      circuit_breaker: false,
      consecutive_losses: 1,
      consecutive_loss_days: 0,
      flags: [
        { ok: true,  text: 'Circuit breakers: armed — no active halt' },
        { ok: true,  text: 'Consecutive losses: 1 of 5 max' },
        { ok: true,  text: 'Live win rate 61.4% vs 62.0% backtested — on track' },
        { ok: false, text: 'High-impact news in 45 min — USD: FOMC Meeting Minutes' },
      ],
      live_win_rate: 61.4,
      backtest_win_rate: 62.0,
      prop_firm: '',
      accounts: [{ name: 'XM-Live01', profit: 2847 }],
      exposure: [
        { symbol: 'BTCUSDT', pct: 45 },
        { symbol: 'EURUSD=X', pct: 33 },
        { symbol: 'XAUUSD=X', pct: 22 },
      ],
      total_equity: 52_847,
    };
  },

  // ── /api/intelligence ──────────────────────────────────────
  async getIntelligence() {
    await this._delay(70);
    return {
      _mock: true,
      council: [
        { time: '18:22:04', agent: 'Quant',      text: 'BTCUSDT showing volume profile POC retest at 68,100. Delta positive (+842). A+ setup forming.' },
        { time: '18:22:11', agent: 'Sentiment',  text: 'Macro conditions supportive. BTC ETF flows positive this week. Risk-on environment.' },
        { time: '18:22:18', agent: "Devil's Advocate", text: 'FOMC minutes in 45 min — potential volatility spike. Liquidity grab risk above 68,500.' },
        { time: '18:22:25', agent: 'Risk',       text: 'Drawdown at 0.82% — within limits. Position sizing appropriate for current regime.' },
        { time: '18:22:31', agent: 'Director',   text: '✅ EXECUTE — SLx1.2 TPx2.0. Conviction: high. Monitor FOMC window carefully.' },
      ],
      signals: [
        { time: '2026-10-03 18:22', symbol: 'BTCUSDT',  direction: 'buy',  grade: 'A+', confidence: 84, traded: true,  skip_reason: null },
        { time: '2026-10-03 14:08', symbol: 'EURUSD=X', direction: 'buy',  grade: 'A',  confidence: 71, traded: true,  skip_reason: null },
        { time: '2026-10-03 11:30', symbol: 'XAUUSD=X', direction: 'sell', grade: 'B',  confidence: 58, traded: false, skip_reason: 'News window — high impact event in 30min' },
        { time: '2026-10-03 09:15', symbol: 'GBPUSD=X', direction: 'sell', grade: 'B+', confidence: 64, traded: false, skip_reason: 'Regime: RANGING — trend strategy disabled' },
        { time: '2026-10-02 17:42', symbol: 'SOLUSDT',  direction: 'buy',  grade: 'A',  confidence: 78, traded: true,  skip_reason: null },
      ],
      ai_track: { wins: 31, losses: 12 },
    };
  },

  // ── /api/compliance ────────────────────────────────────────
  async getCompliance() {
    await this._delay(60);
    return {
      _mock: true,
      chain_verified: true,
      chain_count: 32,
      audit: [
        { time: '2026-10-03 18:22', event: 'Order opened — BTCUSDT buy 0.05 lots | Council: execute', actor: 'Live Bot', account: 'MT5 #84723901', grade: 'A+', r_mult: null },
        { time: '2026-10-03 14:08', event: 'Order opened — EURUSD=X buy 1.0 lots | Council: execute', actor: 'Live Bot', account: 'MT5 #84723901', grade: 'A', r_mult: null },
        { time: '2026-10-03 11:30', event: 'Signal blocked — XAUUSD=X sell: News window — high impact event in 30min', actor: 'Risk Engine', account: '—', grade: '—', r_mult: null },
        { time: '2026-10-02 18:14', event: 'Order closed — SOLUSDT buy +2.1R | outcome: tp2_hit', actor: 'Live Bot', account: 'MT5 #84723901', grade: 'A', r_mult: 2.1 },
        { time: '2026-10-02 14:32', event: 'Order closed — BTCUSDT buy -1.0R | outcome: sl_hit', actor: 'Live Bot', account: 'MT5 #84723901', grade: 'B', r_mult: -1.0 },
      ],
      rule_checks: [
        { ok: true, text: 'Broker: No hedging violations this session' },
        { ok: true, text: 'News-trading restriction: auto-paused during high-impact windows' },
        { ok: true, text: 'FIFO close-order rule: compliant' },
      ],
    };
  },

  // ── /api/admin ─────────────────────────────────────────────
  async getAdmin() {
    await this._delay(40);
    return {
      _mock: true,
      connection: { mt5: false },
      git_hash: 'bee4cd2',
      db_size_mb: 4.82,
      nodes: [{ name: 'Laptop', status: 'active', cpu_pct: 12.4, ram_pct: 48.2 }],
      heartbeat: new Date().toISOString(),
      engine_mode: 'PAPER',
      prop_firm: '',
      account_size: '10000',
      mt5_login: '',
    };
  },

  // ── /api/news ──────────────────────────────────────────────
  async getNews() {
    await this._delay(40);
    return {
      _mock: true,
      events: [
        { title: 'FOMC Meeting Minutes',     currency: 'USD', impact: 'high',   time_utc: '18:00 UTC', time_ist: '23:30 IST', diff_min: 45,   soon: true },
        { title: 'US Initial Jobless Claims', currency: 'USD', impact: 'medium', time_utc: '12:30 UTC', time_ist: '18:00 IST', diff_min: -210, soon: false },
        { title: 'ECB Economic Bulletin',    currency: 'EUR', impact: 'medium', time_utc: '09:00 UTC', time_ist: '14:30 IST', diff_min: -540, soon: false },
        { title: 'RBI Policy Decision',      currency: 'INR', impact: 'high',   time_utc: '04:30 UTC', time_ist: '10:00 IST', diff_min: -840, soon: false },
        { title: 'BOE Interest Rate',        currency: 'GBP', impact: 'high',   time_utc: '12:00 UTC', diff_min: 2880, soon: false },
      ],
    };
  },

  // ── POST /api/chat ─────────────────────────────────────────
  async sendChat(message, context) {
    await this._delay(1200);
    const responses = {
      default: "I'm Jarvis, the CRAVE trading AI. I'm running in **mock mode** — connect the real engine to get live portfolio data. I can answer questions about positions, strategies, risk, and performance.",
      position: "You currently have **3 open positions**: BTCUSDT (buy, +0.57%), EURUSD/X (buy, +0.36%), and XAUUSD=X (sell, +0.12%). Total unrealized P&L: **+$533.20**.",
      strategy: "The **OrderFlow Crypto** strategy is performing best this week with a win rate of **61.4%** and expectancy of **+0.417R**. The Trend/PA Forex strategy on EURUSD is also live-ready with **+0.191R** expectancy.",
      risk: "Risk status is **nominal**. Daily loss: **0.82%** of 4% limit. Circuit breakers armed. One FOMC news event approaching in 45 minutes — entries will auto-pause.",
    };
    const msg = message.toLowerCase();
    let reply = responses.default;
    if (msg.includes('position') || msg.includes('open') || msg.includes('trade')) reply = responses.position;
    else if (msg.includes('strategy') || msg.includes('performance')) reply = responses.strategy;
    else if (msg.includes('risk') || msg.includes('drawdown') || msg.includes('circuit')) reply = responses.risk;
    return { reply, model: 'mock', _mock: true };
  },

  // ── Equity curve data ──────────────────────────────────────
  async getEquityCurve(days = 30) {
    await this._delay(50);
    const data = [];
    let equity = 48_000;
    const now = new Date();
    for (let i = days; i >= 0; i--) {
      const d = new Date(now); d.setDate(d.getDate() - i);
      equity += (Math.random() - 0.38) * 400;
      data.push({ date: d.toISOString().slice(0, 10), equity: +equity.toFixed(2) });
    }
    return { _mock: true, data };
  },

  // ── P&L Calendar ──────────────────────────────────────────
  async getPnLCalendar() {
    await this._delay(50);
    const cells = [];
    const now = new Date();
    for (let i = 29; i >= 0; i--) {
      const d = new Date(now); d.setDate(d.getDate() - i);
      const dow = d.getDay();
      if (dow === 0 || dow === 6) continue;
      const pnl = (Math.random() - 0.42) * 800;
      cells.push({ date: d.toISOString().slice(0, 10), pnl: +pnl.toFixed(2) });
    }
    return { _mock: true, cells };
  },

  // ── Options data ───────────────────────────────────────────
  async getOptions() {
    await this._delay(80);
    return {
      _mock: true,
      underlying: 'NIFTY',
      spot: 26084,
      vix: 14.82,
      chain: [
        { strike: 25900, ce_ltp: 284.5, ce_oi: 1_240_000, ce_delta: 0.72, ce_iv: 14.2, pe_ltp: 48.3,  pe_oi: 890_000, pe_delta: -0.28, pe_iv: 15.1 },
        { strike: 26000, ce_ltp: 198.3, ce_oi: 2_100_000, ce_delta: 0.61, ce_iv: 14.8, pe_ltp: 82.1,  pe_oi: 1_420_000, pe_delta: -0.39, pe_iv: 15.8 },
        { strike: 26100, ce_ltp: 124.7, ce_oi: 3_400_000, ce_delta: 0.49, ce_iv: 15.2, pe_ltp: 138.4, pe_oi: 2_800_000, pe_delta: -0.51, pe_iv: 16.2 },
        { strike: 26200, ce_ltp: 72.4,  ce_oi: 2_840_000, ce_delta: 0.37, ce_iv: 15.8, pe_ltp: 212.8, pe_oi: 1_100_000, pe_delta: -0.63, pe_iv: 16.9 },
        { strike: 26300, ce_ltp: 38.2,  ce_oi: 1_920_000, ce_delta: 0.25, ce_iv: 16.4, pe_ltp: 298.4, pe_oi: 680_000,   pe_delta: -0.75, pe_iv: 17.8 },
      ],
    };
  },

  // ── Utility ────────────────────────────────────────────────
  _delay(ms) {
    return new Promise(r => setTimeout(r, ms));
  },
};

// Make available globally
window.MockProvider = MockProvider;
