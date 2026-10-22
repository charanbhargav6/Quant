/**
 * CRAVE Quant Dashboard — Connection Manager
 * ===========================================
 * Wraps the engine's /api/stream (Server-Sent Events) endpoint.
 *
 * States:
 *   CONNECTING    → opening EventSource
 *   LIVE          → heartbeats arriving within STALE_THRESHOLD_MS
 *   STALE         → heartbeat overdue but connection open
 *   RECONNECTING  → EventSource closed, attempting reconnect
 *   DISCONNECTED  → max retries exceeded or engine unreachable
 *
 * Usage:
 *   ConnMgr.init({ streamUrl: 'http://127.0.0.1:8765/api/stream' });
 *   ConnMgr.on('heartbeat', (data) => { ... });
 *   ConnMgr.on('pnl',       (data) => { ... });
 *   ConnMgr.on('position',  (data) => { ... });
 *   ConnMgr.on('risk',      (data) => { ... });
 *   ConnMgr.on('market',    (data) => { ... });
 *   ConnMgr.on('broker_status', (data) => { ... });
 *   ConnMgr.on('status_change', ({ from, to }) => { ... });
 *
 * The UI must NEVER show "LIVE" unless ConnMgr.state === 'LIVE'.
 */

'use strict';

const ConnMgr = (() => {

  // ── Config ─────────────────────────────────────────────────────────────
  const STALE_THRESHOLD_MS  = 6_000;   // no heartbeat for 6s → STALE
  const RECONNECT_DELAYS_MS = [2000, 4000, 8000, 15000, 30000];  // backoff
  const MAX_RECONNECT       = 12;      // then DISCONNECTED

  // ── Internal state ──────────────────────────────────────────────────────
  let _state         = 'DISCONNECTED';
  let _streamUrl     = '';
  let _es            = null;           // EventSource instance
  let _lastHeartbeat = 0;              // epoch ms
  let _lastEventTs   = null;           // ISO string from engine
  let _reconnectIdx  = 0;
  let _reconnectTimer= null;
  let _stalenessTimer= null;
  let _handlers      = {};             // eventName → [fn, ...]
  let _mockMode      = false;
  let _mockTimer     = null;

  // ── Public state labels ─────────────────────────────────────────────────
  const STATE_LABEL = {
    CONNECTING:   'Connecting',
    LIVE:         'LIVE',
    STALE:        'STALE',
    RECONNECTING: 'RECONNECTING',
    DISCONNECTED: 'DISCONNECTED',
  };

  const STATE_CSS = {
    CONNECTING:   'connecting',
    LIVE:         'live',
    STALE:        'stale',
    RECONNECTING: 'reconnecting',
    DISCONNECTED: 'offline',
  };

  // ── Event emitter ───────────────────────────────────────────────────────
  function on(event, handler) {
    if (!_handlers[event]) _handlers[event] = [];
    _handlers[event].push(handler);
    return ConnMgr;  // chainable
  }

  function off(event, handler) {
    if (!_handlers[event]) return;
    _handlers[event] = _handlers[event].filter(h => h !== handler);
  }

  function emit(event, data) {
    (_handlers[event] || []).forEach(fn => {
      try { fn(data); } catch (e) { console.error('[ConnMgr] handler error:', e); }
    });
  }

  // ── State machine ───────────────────────────────────────────────────────
  function _setState(next) {
    if (_state === next) return;
    const prev = _state;
    _state = next;
    console.log(`[ConnMgr] ${prev} → ${next}`);
    emit('status_change', { from: prev, to: next });
    _updateUI(next);
  }

  // ── UI update (writes to header chips) ─────────────────────────────────
  function _updateUI(state) {
    const dot  = document.getElementById('data-status-dot');
    const text = document.getElementById('data-status-text');
    const chip = document.getElementById('data-status-chip');
    if (!dot || !text || !chip) return;

    // Remove all state classes
    chip.className = `status-chip ${STATE_CSS[state] || ''}`;
    dot.className  = `status-dot ${STATE_CSS[state] || ''}`;
    text.textContent = STATE_LABEL[state] || state;

    // Show last data timestamp in header when stale/reconnecting
    if ((state === 'STALE' || state === 'RECONNECTING') && _lastEventTs) {
      const age = _humanAge(_lastEventTs);
      text.textContent = `${STATE_LABEL[state]} (${age})`;
    }

    // Update mock-mode bar
    const bar = document.getElementById('data-mode-bar');
    if (bar && !_mockMode) {
      if (state === 'LIVE') {
        bar.style.display = 'none';
      } else if (state === 'STALE') {
        bar.style.display = '';
        bar.className = 'data-mode-bar warn';
        bar.innerHTML = `<i data-lucide="alert-triangle" width="14" height="14"></i>
          <strong>STALE DATA</strong> — Engine heartbeat overdue. Last update: ${_humanAge(_lastEventTs)}.`;
        if (typeof lucide !== 'undefined') lucide.createIcons();
      } else if (state === 'RECONNECTING') {
        bar.style.display = '';
        bar.className = 'data-mode-bar warn';
        bar.innerHTML = `<i data-lucide="refresh-cw" width="14" height="14"></i>
          <strong>RECONNECTING</strong> — Attempting to reach engine...`;
        if (typeof lucide !== 'undefined') lucide.createIcons();
      } else if (state === 'DISCONNECTED') {
        bar.style.display = '';
        bar.className = 'data-mode-bar error';
        bar.innerHTML = `<i data-lucide="wifi-off" width="14" height="14"></i>
          <strong>DISCONNECTED</strong> — Engine unreachable. Data frozen at ${_humanAge(_lastEventTs)}.
          <button onclick="ConnMgr.reconnect()" class="btn btn-secondary btn-sm" style="margin-left:12px">Retry</button>`;
        if (typeof lucide !== 'undefined') lucide.createIcons();
      }
    }
  }

  // ── Staleness watchdog ──────────────────────────────────────────────────
  function _startStalenessWatchdog() {
    _stopStalenessWatchdog();
    _stalenessTimer = setInterval(() => {
      const age = Date.now() - _lastHeartbeat;
      if (_state === 'LIVE' && age > STALE_THRESHOLD_MS) {
        _setState('STALE');
      }
    }, 1000);
  }

  function _stopStalenessWatchdog() {
    if (_stalenessTimer) { clearInterval(_stalenessTimer); _stalenessTimer = null; }
  }

  // ── EventSource lifecycle ───────────────────────────────────────────────
  function _openStream() {
    if (_es) { _es.close(); _es = null; }
    _setState('CONNECTING');

    try {
      _es = new EventSource(_streamUrl);
    } catch (e) {
      console.error('[ConnMgr] EventSource failed:', e);
      _scheduleReconnect();
      return;
    }

    // ── SSE events ──────────────────────────────────────────────────────
    _es.addEventListener('connected', e => {
      _reconnectIdx = 0;
      _lastHeartbeat = Date.now();
      _setState('LIVE');
      _startStalenessWatchdog();
      try {
        const d = JSON.parse(e.data);
        _lastEventTs = d.ts;
        emit('connected', d);
      } catch (_) {}
    });

    _es.addEventListener('heartbeat', e => {
      _lastHeartbeat = Date.now();
      if (_state !== 'LIVE') _setState('LIVE');
      try {
        const d = JSON.parse(e.data);
        _lastEventTs = d.ts;
        emit('heartbeat', d);
      } catch (_) {}
    });

    _es.addEventListener('pnl', e => {
      _lastHeartbeat = Date.now();
      if (_state !== 'LIVE') _setState('LIVE');
      try { emit('pnl', JSON.parse(e.data)); } catch (_) {}
    });

    _es.addEventListener('position', e => {
      _lastHeartbeat = Date.now();
      try { emit('position', JSON.parse(e.data)); } catch (_) {}
    });

    _es.addEventListener('risk', e => {
      _lastHeartbeat = Date.now();
      try { emit('risk', JSON.parse(e.data)); } catch (_) {}
    });

    _es.addEventListener('market', e => {
      _lastHeartbeat = Date.now();
      try { emit('market', JSON.parse(e.data)); } catch (_) {}
    });

    _es.addEventListener('broker_status', e => {
      _lastHeartbeat = Date.now();
      try { emit('broker_status', JSON.parse(e.data)); } catch (_) {}
    });

    // Native EventSource error (connection dropped)
    _es.onerror = () => {
      _es.close();
      _es = null;
      _stopStalenessWatchdog();
      if (_state !== 'DISCONNECTED') {
        _setState('RECONNECTING');
        _scheduleReconnect();
      }
    };
  }

  function _scheduleReconnect() {
    if (_reconnectTimer) return;
    if (_reconnectIdx >= MAX_RECONNECT) {
      _setState('DISCONNECTED');
      return;
    }
    const delay = RECONNECT_DELAYS_MS[Math.min(_reconnectIdx, RECONNECT_DELAYS_MS.length - 1)];
    _reconnectIdx++;
    console.log(`[ConnMgr] Reconnect #${_reconnectIdx} in ${delay}ms...`);
    _reconnectTimer = setTimeout(() => {
      _reconnectTimer = null;
      _openStream();
    }, delay);
  }

  // ── Mock mode (no engine running) ───────────────────────────────────────
  function _startMockMode() {
    _mockMode = true;
    _setState('LIVE');  // mock is always "live" (labelled differently in bar)
    _startStalenessWatchdog();
    _lastHeartbeat = Date.now();

    // Simulate heartbeats every 2s so the staleness watchdog stays happy
    _mockTimer = setInterval(() => {
      _lastHeartbeat = Date.now();
      _lastEventTs   = new Date().toISOString();
      emit('heartbeat', {
        ts:          _lastEventTs,
        server_time: new Date().toISOString().slice(11, 19) + ' UTC',
        mt5:         false,
        binance:     false,
        zerodha:     false,
        ws_status:   {},
        clients:     0,
      });
    }, 2000);
  }

  // ── Helpers ─────────────────────────────────────────────────────────────
  function _humanAge(isoTs) {
    if (!isoTs) return 'unknown';
    const diff = (Date.now() - new Date(isoTs).getTime()) / 1000;
    if (diff < 60)    return `${Math.floor(diff)}s ago`;
    if (diff < 3600)  return `${Math.floor(diff / 60)}m ago`;
    return `${Math.floor(diff / 3600)}h ago`;
  }

  // ── Public API ──────────────────────────────────────────────────────────
  function init(opts = {}) {
    _streamUrl = opts.streamUrl || `${window.CRAVE_API_BASE || ''}/api/stream`;
    _mockMode  = opts.mock || (window.CRAVE_DATA_MODE === 'mock');

    if (_mockMode) {
      _startMockMode();
    } else {
      _openStream();
    }

    // Expose on window for dev console access
    window._connMgr = ConnMgr;
  }

  function reconnect() {
    _reconnectIdx = 0;
    if (_reconnectTimer) { clearTimeout(_reconnectTimer); _reconnectTimer = null; }
    if (_mockTimer)      { clearInterval(_mockTimer); _mockTimer = null; }
    _openStream();
  }

  function getState()       { return _state; }
  function getLastTs()      { return _lastEventTs; }
  function getLastAgeMs()   { return Date.now() - _lastHeartbeat; }
  function getLastAgeLabel(){ return _humanAge(_lastEventTs); }

  function isMock()         { return _mockMode; }

  return { init, on, off, reconnect, getState, getLastTs, getLastAgeMs, getLastAgeLabel, isMock };
})();

window.ConnMgr = ConnMgr;
