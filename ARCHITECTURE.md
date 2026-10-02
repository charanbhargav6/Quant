# Quant Architecture

**Inspected baseline:** GitHub branch main at commit bee4cd265df021a23e3966dcf8ec4cdd9331b408.

**Scope:** Static inspection of the tracked source tree and implementation paths. No trading process, broker connection, runtime database, or test suite was run. No .env file or credential value was read; only configuration variable names in .env.example were in scope. This report describes code, not proof that optional services are configured or reachable.

## System shape and actual data flow

The launch modes are not equivalent. run_bot.py defaults to paper mode unless --live or TRADING_MODE=live is selected. run_crave.py starts quant_server.py and a separate run_bot.py --live subprocess. crave_master.py manages profile-specific bot/server processes. The Flask server and bot therefore have separate Python memory, even when they share files and a SQLite path.

The normal trading path in core/trading_loop.py is approximately:

MarketDataRouter / DataAgent -> trading-loop exit handling and entry/session/news/regime gates -> strategy selection and normalized signal -> RiskAgent and portfolio/risk checks -> council approval -> PaperTradingEngine OR BrokerRouter -> selected broker -> PositionTracker -> JSON position state and SQLite position mirror. On close, PositionTracker removes the open position, records the closed trade in SQLite, updates streak and paper-equity state as applicable, and triggers Telegram/post-trade work.

That is not the entire system flow:

- The existing browser dashboard is quant_engine_ui.html. It polls quant_server.py over HTTP. The server assembles views from MT5, SQLite, configuration, news and streak state; it does not consume a browser WebSocket feed or a live Supabase dashboard stream.
- Paper trades use PositionTracker, but the existing command-center and execution APIs query MT5 positions. The portfolio endpoint reports paper equity but does not return tracked paper open positions.
- intelligence/news_trader.py has an alternate direct MT5 order path. Its execution function calls MT5Agent.place_order rather than BrokerRouter.execute and does not register the resulting position through PositionTracker in that function.
- The options loop computes option signals and supports manual review/Telegram status. No /options_confirm handler was found; the inspected loop does not automatically execute those signals.
- Supabase usage is limited to specific ML backtest writes and watchdog/check queries described below. The README's always-on dashboard-pusher description is not backed by a dashboard/ directory or runtime pusher in the tracked tree.

## Component inventory

### 1. Main entry points
- **FILE/PATH:** run_bot.py; run_crave.py; crave_master.py; quant_server.py.
- **PURPOSE:** Start the bot, the local Flask UI/API, or profile-managed bot/server instances.
- **INPUTS:** CLI flags, profile selection, configuration and environment variable names read by the application.
- **OUTPUTS:** Trading-loop process, Flask process, and local HTTP service.
- **DEPENDS ON:** config/config.py, core runtime modules, installed provider libraries.
- **USED BY:** Operators and deployment/service launchers.
- **DASHBOARD RELEVANCE:** quant_server.py is the existing HTTP integration seam. run_crave.py launches it separately from the bot and explicitly selects live mode.

### 2. Trading loop
- **FILE/PATH:** core/trading_loop.py; run_bot.py.
- **PURPOSE:** Manage exit processing, entry gates, scanning, signal evaluation, risk checks and execution cycles.
- **INPUTS:** Market data, current positions, enabled instruments/strategies, trading mode and runtime configuration.
- **OUTPUTS:** Accepted/rejected signals, execution calls, position lifecycle updates and operational logs.
- **DEPENDS ON:** MarketDataRouter/DataAgent, strategy components, risk controls, council, BrokerRouter or PaperTradingEngine, PositionTracker.
- **USED BY:** run_bot.py runtime.
- **DASHBOARD RELEVANCE:** It is the authority for trading decisions; a dashboard should observe its state rather than call or reproduce its decision logic.

### 3. Strategy engines
- **FILE/PATH:** core/strategy_agent.py; core/strategy_registry.py; strategy_adapters.py; engines/.
- **PURPOSE:** Select and run strategies and normalize strategy-specific output for downstream validation.
- **INPUTS:** OHLCV/market context, instrument configuration, regimes, enabled strategy definitions and current state.
- **OUTPUTS:** Candidate trade signals and strategy metadata/readiness.
- **DEPENDS ON:** Market-data interfaces, strategy implementations and registry/configuration.
- **USED BY:** TradingLoop, backtests, strategy/API views and BrokerRouter eligibility checks.
- **DASHBOARD RELEVANCE:** Registry/readiness is reusable for display; it is not a substitute for live execution state.

### 4. Broker integrations
- **FILE/PATH:** brokers/broker_router.py; brokers/mt5_agent.py; brokers/binance_agent.py; brokers/zerodha_agent.py; broker/account manager modules.
- **PURPOSE:** Route validated entries and account operations to supported broker adapters.
- **INPUTS:** Validated signal, account/profile selection, symbol, quantity and order parameters.
- **OUTPUTS:** Broker result/status, broker ticket/order identifiers, fills and account/position reads.
- **DEPENDS ON:** Provider SDKs, account configuration and BrokerRouter routing rules.
- **USED BY:** TradingLoop and account/process-management flows. NewsTrader contains an alternate direct MT5 call path.
- **DASHBOARD RELEVANCE:** Existing views are MT5-centric; do not call broker SDKs from browser code or treat all adapters as one normalized account feed.

### 5. Market-data system
- **FILE/PATH:** data/market_data_router.py; core/data_agent.py; provider/data modules; interfaces/websocket_manager.py.
- **PURPOSE:** Request OHLCV/current-price data, use provider-specific sources and fallbacks, and cache candles.
- **INPUTS:** Symbol, timeframe, requested limit, exchange/asset metadata and provider connectivity.
- **OUTPUTS:** DataFrames, prices, option-chain data where available, and cached OHLCV.
- **DEPENDS ON:** Broker/data providers, yfinance fallbacks, database cache and optional WebSocket store.
- **USED BY:** TradingLoop, strategies, options and analysis/backtest paths.
- **DASHBOARD RELEVANCE:** /api/markets currently builds a configured MT5 instrument matrix; it is not a general feed for every provider/asset.

### 6. Position management
- **FILE/PATH:** core/position_tracker.py; State/crave_positions*.json; SQLite positions/trades tables.
- **PURPOSE:** Maintain open tracked positions and coordinate close-time persistence and downstream updates.
- **INPUTS:** Filled trade data, exit price, R multiple and outcome.
- **OUTPUTS:** Open-position state, closed-trade record, database updates, streak/paper updates and notifications.
- **DEPENDS ON:** JSON state, DatabaseManager, streak state, paper engine and notification/post-trade modules.
- **USED BY:** Standard live and paper execution paths.
- **DASHBOARD RELEVANCE:** This is the common tracked-position interface, but the current broker-oriented APIs do not expose it as a unified live-and-paper feed.

### 7. Order management
- **FILE/PATH:** brokers/broker_router.py; core/paper_trading.py; core/execution_agent.py.
- **PURPOSE:** Route a validated order to its execution boundary; simulate fills in paper mode.
- **INPUTS:** Risk-validated signal, market price and selected runtime mode.
- **OUTPUTS:** Broker fills/failures or simulated fills; standard successful paths register positions.
- **DEPENDS ON:** Broker adapter or PaperTradingEngine and the PositionTracker.
- **USED BY:** TradingLoop; some legacy/standalone execution helpers remain in the tree.
- **DASHBOARD RELEVANCE:** The UI close endpoint is a separate direct MT5 operation and does not call the normal PositionTracker close workflow.

### 8. Risk management
- **FILE/PATH:** core/risk_agent.py; core/portfolio_risk_engine.py; core/streak_state.py; prop-firm/risk guard modules.
- **PURPOSE:** Validate candidate trades, size exposure and enforce portfolio, daily-loss, streak and account constraints.
- **INPUTS:** Candidate signal, price/stop, account equity, open exposure, recent outcomes and configured limits.
- **OUTPUTS:** Approved/blocked validation, sizing and circuit-breaker/streak state.
- **DEPENDS ON:** Trading configuration, account/position information, database and persisted streak state.
- **USED BY:** TradingLoop and selected execution/adaptation paths; the direct NewsTrader path is separate and should not be assumed to pass through the same validation chain.
- **DASHBOARD RELEVANCE:** /api/risk is an informational summary, not the risk engine interface. It has a schema/query mismatch noted in DASHBOARD_INTEGRATION.md.

### 9. Portfolio management
- **FILE/PATH:** core/portfolio_risk_engine.py; core/profit_allocator.py; core/position_tracker.py; quant_server.py portfolio/command-center handlers.
- **PURPOSE:** Calculate portfolio constraints, allocation and presentation summaries from available account/trade state.
- **INPUTS:** Account equity, positions, strategy results, instrument metadata and risk limits.
- **OUTPUTS:** Exposure/risk summaries, allocation decisions and UI JSON summaries.
- **DEPENDS ON:** Broker reads, SQLite, PositionTracker and configuration.
- **USED BY:** Trading/risk paths and existing UI handlers.
- **DASHBOARD RELEVANCE:** No single normalized portfolio service spans all brokers plus paper positions; the API currently composes partial views.

### 10. Options system
- **FILE/PATH:** options/options_engine.py; options/options_loop.py; options/greeks_monitor.py; options/iv_calculator.py.
- **PURPOSE:** Read option-chain/volatility context, estimate Greeks and generate option ideas/signals.
- **INPUTS:** Option-chain/provider data, price/volatility context, India/VIX configuration and loop schedule.
- **OUTPUTS:** Greeks/IV/status and candidate option signals/manual-review messages.
- **DEPENDS ON:** Market data, configured option provider and Telegram.
- **USED BY:** run_bot.py options initialization/monitoring and command handlers.
- **DASHBOARD RELEVANCE:** Display the signal and its review status as non-executable information; no inspected /options_confirm command handler or automatic option order flow was found.

### 11. ML system
- **FILE/PATH:** ml/; core/database_manager.py; ml/backtest_runner.py.
- **PURPOSE:** Build features, classify regimes, learn from trade outcomes and run model/backtest tasks.
- **INPUTS:** Historical market data, stored signals/trades and labels/outcomes.
- **OUTPUTS:** Regime/feature results, model artifacts/metrics and ML backtest records.
- **DEPENDS ON:** Database, market data, configured ML libraries and optional Supabase client.
- **USED BY:** Strategy/trading analysis and offline/backtest flows.
- **DASHBOARD RELEVANCE:** ML results are not a live dashboard event stream; show source and freshness if surfaced.

### 12. Backtesting
- **FILE/PATH:** backtesting/; backtest-related scripts; ml/backtest_runner.py.
- **PURPOSE:** Run historical simulation, validation and optimization independently of the live broker loop.
- **INPUTS:** Historical OHLCV, strategy/parameter configuration and test windows.
- **OUTPUTS:** Performance summaries, local results/artifacts and selected ML/Supabase backtest rows.
- **DEPENDS ON:** Historical data, strategy adapters, database and optional ML dependencies.
- **USED BY:** Strategy research/readiness and developer workflows.
- **DASHBOARD RELEVANCE:** Backtest metrics must be labeled separately from live fills and account balances.

### 13. Paper trading
- **FILE/PATH:** core/paper_trading.py; State/crave_paper_state*.json; core/position_tracker.py.
- **PURPOSE:** Simulate fills/slippage and maintain paper equity/trade statistics while using the common position tracker.
- **INPUTS:** Risk-validated signal, current price and paper configuration.
- **OUTPUTS:** Simulated fill, paper position and persisted paper equity/statistics.
- **DEPENDS ON:** Configuration, paper JSON state and PositionTracker.
- **USED BY:** TradingLoop when paper mode is selected.
- **DASHBOARD RELEVANCE:** The existing portfolio API reports paper equity but not tracked paper open positions; execution API is MT5-only.

### 14. WebSocket/realtime system
- **FILE/PATH:** interfaces/websocket_manager.py; provider WebSocket clients; data/market_data_router.py.
- **PURPOSE:** Receive provider streams into a candle/price store and expose live-price/OHLCV helper methods.
- **INPUTS:** Binance kline/bookTicker and Alpaca bar/quote streams where configured.
- **OUTPUTS:** Internal CandleStore/current-price reads and a Telegram status string.
- **DEPENDS ON:** Provider credentials/configuration and a call to WebSocketManager.start().
- **USED BY:** MarketDataRouter can request cached live values; run_bot.py exposes a /ws status command.
- **DASHBOARD RELEVANCE:** No browser WebSocket route/event schema was found. No launcher call to start the manager was found in inspected tracked Python sources; do not assume feeds are active.

### 15. Supabase integration
- **FILE/PATH:** ml/backtest_runner.py; watchdog/check scripts; README.md.
- **PURPOSE:** Store/query selected ML backtest and operational status/trade data.
- **INPUTS:** ML backtest output and health/status queries.
- **OUTPUTS:** Insert into ml_backtest_results; watchdog/check code queries crave_system_status, crave_trades and crave_open_positions.
- **DEPENDS ON:** Supabase URL/key configuration and the remote schema.
- **USED BY:** ML runner and operational check/watchdog paths.
- **DASHBOARD RELEVANCE:** No tracked dashboard/ pusher module or SQL migrations/functions were found. Runtime position/trade writes to those tables were not established; README claims are not evidence of a functioning live feed.

### 16. Database and storage
- **FILE/PATH:** core/database_manager.py; Database/; data/trades.db used by ML backtest runner; State/ JSON files.
- **PURPOSE:** Persist trades, positions, signals, cached market data, account records and statistics.
- **INPUTS:** Trading/close events, account changes and cached bars.
- **OUTPUTS:** SQLite reads/writes, analytics and state used by runtime/API paths.
- **DEPENDS ON:** Filesystem, SQLite and configured profile paths.
- **USED BY:** TradingLoop, PositionTracker, market-data cache, ML and Flask API.
- **DASHBOARD RELEVANCE:** Use a server-side read model. The configured runtime DB path differs from empty database artifacts under data/; the ML backtest DB is separate.

### 17. State management
- **FILE/PATH:** config/config.py; State/; core/streak_state.py; infra/state_sync.py; infra/node_orchestrator.py.
- **PURPOSE:** Persist profile-specific positions, paper state, streaks and optional node synchronization.
- **INPUTS:** Runtime state changes, profile and optional state-sync configuration.
- **OUTPUTS:** JSON state files and, when explicitly configured/started, push/pull through a private GitHub state branch.
- **DEPENDS ON:** Filesystem, optional Git/GitHub setup and deployment orchestration.
- **USED BY:** Bot restart recovery, failover/deployment support and PaperTradingEngine.
- **DASHBOARD RELEVANCE:** State files are not a browser API. The emergency-stop threading.Event is process-local; a server-side set cannot itself signal a separately launched bot process.

### 18. Telegram interface
- **FILE/PATH:** interfaces/telegram_interface.py; command registrations in run_bot.py.
- **PURPOSE:** Send alerts and trade notifications and route operator commands.
- **INPUTS:** Runtime events, registered command names and Telegram configuration.
- **OUTPUTS:** Telegram messages and command callbacks.
- **DEPENDS ON:** Telegram provider/client configuration and runtime callbacks.
- **USED BY:** Bot loop, position close notifications, options status/review and operator controls.
- **DASHBOARD RELEVANCE:** Separate operator channel; it is not a browser WebSocket or a dashboard API.

### 19. Existing web/UI components
- **FILE/PATH:** quant_engine_ui.html; quant_server.py; core/account_endpoints_patch.py; core/supervisor_endpoints.py.
- **PURPOSE:** Serve a single-page operational UI and JSON routes for command center, portfolio, markets, strategies, execution, risk and management.
- **INPUTS:** Browser HTTP requests; MT5, SQLite, streak/configuration and news data.
- **OUTPUTS:** HTML/JSON and a small set of control actions.
- **DEPENDS ON:** Flask, Flask-CORS, broker/database helpers and loopback networking.
- **USED BY:** Local operator browser and account/process controls.
- **DASHBOARD RELEVANCE:** Existing UI polls the active tab every 2 seconds and command center every 5 seconds. Server binds to 127.0.0.1 by default; it is not a hosted dashboard backend contract.

### 20. Tests
- **FILE/PATH:** tests/; test_*.py; run_phase5_e2e_test.py; adapter/runtime test scripts.
- **PURPOSE:** Exercise selected components, routes, migrations, adapters and runtime scenarios.
- **INPUTS:** Test fixtures/mocks and application modules.
- **OUTPUTS:** Assertions and test reports.
- **DEPENDS ON:** Python dependencies and test configuration.
- **USED BY:** Developer/CI validation.
- **DASHBOARD RELEVANCE:** This documentation task did not execute tests; route/runtime observations are static and should be validated before building a client.

## Existing interfaces and endpoint families

quant_server.py serves HTML at / and registers JSON routes. Main read routes are GET /api/ping, /api/command_center, /api/portfolio, /api/strategies, /api/strategies/{sid}/detail, /api/markets, /api/execution, /api/risk, /api/intelligence, /api/compliance, /api/accounts, /api/accounts/{acc_id}, /api/terminal, /api/optimizer/propose, /api/news, /api/admin, /api/accounts/how_to_add, and /api/prop_firms. The supervisor module adds GET /api/accounts/{account_id}/process_status.

Control/mutation routes include POST /api/execution/close, /api/intelligence/council, /api/chat, /api/optimizer/approve, /api/admin/emergency_stop, /api/accounts/add, /api/accounts/{acc_id}/strategies, /api/accounts/{account_id}/launch, /api/accounts/{account_id}/stop, and /api/accounts/zerodha/complete; DELETE /api/accounts/{account_id}; plus the Zerodha OAuth callback /zerodha_redirect. Exact purposes and cautions are in DASHBOARD_INTEGRATION.md.

Reusable interfaces include MarketDataRouter methods, BrokerRouter.execute, RiskAgent validation, PositionTracker open/get_all/close, DatabaseManager query/save methods, the strategy registry, and Telegram command registration. These are server-side interfaces, not browser-safe APIs.

## Protected scope and dashboard extension point

For this request, only ARCHITECTURE.md and DASHBOARD_INTEGRATION.md are in scope. Do not edit run_bot.py, core/trading_loop.py, strategy implementations/registry/adapters, brokers/, risk modules, options execution, PositionTracker/PaperTradingEngine, quant_server.py, quant_engine_ui.html, configuration, deployment state, or credentials. No such files were changed.

The best existing connection point for a future dashboard is a deliberately read-only, authenticated server-side API beside quant_server.py, with a normalized snapshot assembled from broker state, PositionTracker/paper state and the database. The current API is useful as a prototype source but is not a complete cross-broker/paper contract. Do not expose its control/account endpoints by default. See DASHBOARD_INTEGRATION.md for the observed gaps and proposed boundary.
