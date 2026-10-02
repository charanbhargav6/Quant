# Dashboard Integration Guide

**Inspected baseline:** GitHub branch main at commit bee4cd265df021a23e3966dcf8ec4cdd9331b408. This is a source-only integration analysis. No dashboard was built and no broker, trading process, runtime database, or test suite was run. No .env contents or credential values were read.

## Recommendation

Use the existing Flask service in quant_server.py as the integration boundary, not broker SDKs, strategy code, SQLite, or Supabase from the browser. For a future dashboard, add a deliberately read-only and authenticated server-side projection that combines the existing broker state with PositionTracker/paper state and persisted history. The current API is a useful local prototype surface, but it is not a complete live-and-paper or cross-broker contract.

## Current topology

- quant_server.py serves quant_engine_ui.html and JSON on 127.0.0.1, default port 8765. Flask-CORS is enabled; no authentication middleware was found in the inspected server paths.
- run_crave.py launches the Flask process and run_bot.py --live as separate subprocesses. The browser and bot therefore do not share Python memory.
- The UI makes HTTP fetches. It refreshes the active tab every 2 seconds and command center every 5 seconds. It does not open a browser WebSocket.
- A hosted/remote dashboard cannot treat 127.0.0.1 as the trading server: that address resolves on the browser user's machine. Use an authenticated backend/proxy if the dashboard is remote; do not expose the trading host or broker APIs directly.

## Existing read routes

Default methods are GET unless listed otherwise. These are route handlers found in quant_server.py and registered companion modules.

| Route | Observed data/behavior | Important boundary |
|---|---|---|
| GET /api/ping | Supervisor health/database check. | Liveness is not proof that the bot process or broker is healthy. |
| GET /api/command_center | Connection state, equity/balance, daily P&L, MT5 open-position count, alerts, movers, performance and server time. | Open positions are sourced from MT5; DB performance and streak/news summaries are separate inputs. |
| GET /api/portfolio | MT5 account summary/exposure, paper-equity account row and live/paper DB trade statistics. | Paper open positions are not returned; total_equity is populated from MT5 only. |
| GET /api/strategies | Strategy definitions/status and stored performance inputs. | Display metadata is not a live order/fill feed. |
| GET /api/strategies/{sid}/detail | Detail for one strategy. | Uses the server's strategy registry/DB context. |
| GET /api/markets | Configured MT5 instrument matrix with price/bid/ask/spread/change/bias/regime and news. | Not a unified market feed for Binance, Alpaca, Zerodha and every configured asset. |
| GET /api/execution | Open MT5 positions as orders, recent MT5 closed trades and DB fill statistics. | Despite the key name orders, active rows are MT5 positions; paper tracked positions are absent. |
| GET /api/risk | Daily loss, limits, streak/circuit status, exposure, account summary, flags and live/backtest win-rate display. | MT5-centric and contains the profit-column mismatch below. |
| GET /api/intelligence | Intelligence/news/council summary. | Aggregated server-side information, not a streaming event contract. |
| GET /api/compliance | Compliance/status view. | Read its response as a presentation summary, not as an execution authorization. |
| GET /api/accounts | Account list/status. | Account management data should not be exposed to an unauthenticated client. |
| GET /api/accounts/{acc_id} | Account detail. | Keep behind authenticated operator access. |
| GET /api/terminal | Terminal/log view. | Do not assume it is a stable or public API. |
| GET /api/optimizer/propose | Proposed optimizer changes. | Proposal only; approval is a separate mutation. |
| GET /api/news | Current server-side news/events. | No push stream; freshness depends on its underlying provider/cache. |
| GET /api/admin | Admin/runtime summary. | engine_mode is hard-coded to LIVE in the response handler; do not use it as authoritative mode. |
| GET /api/accounts/how_to_add | Account setup guidance. | Not needed by a read-only dashboard. |
| GET /api/prop_firms | Supported prop-firm metadata/rules. | Configuration reference, not account risk telemetry. |
| GET /api/accounts/{account_id}/process_status | Per-account process status from the supervisor routes. | Process supervision state is distinct from broker/account health. |
| GET /zerodha_redirect | OAuth callback route. | Account-authorization flow; not a dashboard data endpoint. |

Key response shapes observed in the source:

- /api/command_center returns connection, equity, balance, today_pnl, unrealized, open_positions, alerts, top_movers, performance, ping_ms and server_time.
- /api/portfolio returns connection, accounts, total_equity, exposure and db_stats. The paper account is an equity/statistics row, not a paper positions list.
- /api/markets returns connection, matrix and news.
- /api/execution returns connection, orders, closed_trades and fill_stats. Both open orders and closed trades are sourced from MT5; fill_stats is sourced from SQLite.
- /api/risk returns connection, daily_loss_pct, daily_loss_limit, overall_loss, corr_exposure_pct, circuit_breaker, consecutive_losses, flags, live_win_rate, backtest_win_rate, prop_firm, accounts, exposure and total_equity.

There is no single response that provides normalized positions across MT5, paper and every configured broker. Response keys are assembled directly in handlers; no dashboard-facing WebSocket schema was found.

## Existing control and account routes

These routes can mutate trading/account/process state. They should not be included in a new dashboard's default read-only client:

| Method and route | Observed effect / caution |
|---|---|
| POST /api/execution/close | Closes an MT5 ticket directly, then pushes a council message. The handler does not call PositionTracker.close or reconcile the tracked JSON/SQLite position. |
| POST /api/admin/emergency_stop | Sets the server process's threading.Event and persists server-side streak state. run_crave.py runs the bot in another process, so that Event is not shared with the bot; this is not a reliable cross-process stop signal. The UI copy also says existing positions remain open. |
| POST /api/optimizer/approve | Applies an optimizer proposal. |
| POST /api/intelligence/council | Adds an operator/council message. |
| POST /api/chat | Sends a prompt to the server-side assistant. |
| POST /api/accounts/add; POST /api/accounts/{acc_id}/strategies | Account/strategy configuration actions; keep out of a read-only dashboard. |
| POST /api/accounts/{account_id}/launch; POST /api/accounts/{account_id}/stop | Process control. |
| POST /api/accounts/zerodha/complete; GET /zerodha_redirect | Account authorization flow. |
| DELETE /api/accounts/{account_id} | Deletes an account record. |

The precise route semantics should be rechecked before any future control surface is designed. The controls above are listed so they are not mistaken for harmless read endpoints.

## Realtime and Supabase findings

The code has provider WebSocket clients for Binance kline/bookTicker and Alpaca bar/quote data, plus an internal CandleStore and get_live_price/get_live_ohlcv helpers. No Flask WebSocket/SSE route, browser event names, or browser subscription code was found. WebSocketManager.start() exists, but no launcher call to start it was found in inspected tracked Python sources; treat provider streaming as unverified until its lifecycle is proven.

The UI's actual refresh mechanism is HTTP polling. For a first dashboard integration, polling a stable read API is closer to the current design than introducing a new socket protocol. If realtime is added later, define explicit event names, payload versions, connection lifecycle and authentication; do not expose provider sockets directly to the browser.

README.md describes an always-on Supabase dashboard pusher and tables including operational status/trades/positions. The tracked tree has no dashboard/ directory or Supabase SQL migrations/functions. Code references show ml/backtest_runner.py inserting ml_backtest_results; watchdog/check code queries crave_system_status, crave_trades and crave_open_positions. A live runtime feed of the bot's current trades/positions into Supabase was not established. Do not use Supabase as the current live dashboard source without first verifying that external schema and writer.

## Integration gaps to resolve before a dashboard relies on these APIs

1. **Paper positions are missing from the existing view contracts.** PaperTradingEngine opens positions through PositionTracker, but /api/command_center and /api/execution read MT5 positions. /api/portfolio adds paper equity but not paper open positions.
2. **Manual close can desynchronize tracked state.** /api/execution/close calls the MT5 agent directly and does not invoke PositionTracker.close, even though the normal close path also updates SQLite, streak and paper state.
3. **Emergency stop is process-local.** The Flask process and bot process are separate under run_crave.py. A threading.Event set in the server cannot itself signal the bot's event. The persisted streak write is not proof of immediate cross-process propagation.
4. **Mode display is not authoritative.** /api/admin returns the literal LIVE, while run_bot.py defaults to paper unless a live flag/environment setting is present.
5. **Risk response/schema mismatch.** core/database_manager.py defines closed-trade pnl_pct, not profit, and no profit-column migration was found. /api/risk queries SUM(profit) for overall_loss. Treat overall_loss as unreliable on a fresh schema until the query/schema contract is corrected and tested.
6. **Coverage is broker-specific.** Portfolio, markets and execution handlers use MT5-specific reads for key fields; the API does not normalize the other broker adapters into the same account/position schema.
7. **Transport/security boundary is local-only.** The server binds to loopback by default and has Flask-CORS but no observed auth middleware. Do not put it on a public interface or rely on browser-origin controls as authentication.
8. **Supabase live data is unverified.** README claims exceed the current tracked writer/query implementation; it is not a dependable dashboard source based on this snapshot.

## Recommended future integration boundary

Keep the dashboard read-only at first. Add or define a versioned server-side snapshot contract beside quant_server.py (or in a separate authenticated backend-for-frontend) that explicitly includes:

- observed_at timestamp, runtime mode from a verified source, service/broker health and source freshness;
- account identity/type and currency with explicit source labels;
- live and paper open positions as separate records, including stable ID, symbol, side, size, entry/current/stop/target, P&L and update time;
- closed-trade history with source/account, timestamps, realized P&L and strategy attribution;
- risk/streak summary with definition and freshness for each metric;
- strategy readiness/status and market snapshot with provider/source labels.

This is a recommendation, not an existing interface. Build the read model server-side from the broker and tracked/persisted state; do not let the browser open SQLite files, import Python modules, or send orders to a broker. Do not silently merge live and paper positions. Keep account/OAuth/delete/process-control and order-close/emergency-stop routes out of the dashboard's default permission set. A remote deployment needs an authenticated server-side proxy, TLS and explicit authorization before it can safely consume any account data.

## Files and behavior kept out of scope

For the requested documentation-only work, the only files to add are ARCHITECTURE.md and DASHBOARD_INTEGRATION.md. Do not change run_bot.py, core/trading_loop.py, strategy engines/registry/adapters, brokers/, risk modules, options execution, PositionTracker/PaperTradingEngine, quant_server.py, quant_engine_ui.html, configuration, .env files, or deployment state. No dashboard, trading, broker, risk, strategy, configuration or credential files were changed.
