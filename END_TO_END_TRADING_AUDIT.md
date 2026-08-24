# Quant End-to-End Trading Audit and Patch Report

**Audit scope.** This review covers the four attached engine-log rotations (`engine.log` through `engine.log.3`), the current repository checkout, account onboarding and persistence, MT5/Binance/Zerodha routing, strategy allocation, historical walk-forward evidence, ML and LLM-council gates, order-flow/footprint behavior, and safe multi-strategy operation. No order was sent and no live position was modified.

## Executive conclusion

The historical logs show that the engine was not failing for one isolated reason. It was operating as a partially integrated system: MT5 did connect repeatedly, but the account/process/database state was not always aligned; Binance market-data and execution paths were mixed with MT5/CFD symbols; the legacy global-credential execution path could attempt Binance orders without an API key; LLM responses were often unavailable or malformed; and close paths referenced a factory name absent from the deployed router. These conditions make historical “filled” lines insufficient evidence that the intended account, strategy, or risk configuration was the one actually used.

The repository has now been patched to make the critical boundaries explicit and fail closed. Account onboarding verifies broker identity before persistence, fresh databases automatically receive account/process columns, profile names are validated, new profiles default to paper mode, live mode requires explicit confirmation, Binance symbols and USD-M clients are normalized, non-Binance order-book requests are blocked, the legacy live wrapper routes through the centralized broker router, old close callers have a compatibility alias, and the LLM council requires complete model-backed evidence before approval.

> **Important limitation:** the sandbox cannot verify the user’s current account because it has no MT5 terminal, no configured account credentials, and no Binance/Zerodha credentials. The attached logs prove that a historical MetaQuotes-Demo session connected, but they do not prove that the current account is connected now. The provided read-only verifier is the final step to run on the machine that owns the terminal and credentials.

## 1. Log evidence

The logs span **10 June 2026 through 24 August 2026**. The normalized analysis found the following high-impact patterns:

| Observed pattern | Count | Interpretation | Patch/status |
|---|---:|---|---|
| Binance OHLCV `exchangeInfo` failures | 282 | Repeated exchange metadata/network failure or wrong Binance client/path. | Data client now uses USD-M futures and normalized symbols; failures remain observable. |
| Legacy ExecutionAgent Binance API-key crashes | 215 | A global data client was being used as an order client without authenticated credentials. | Legacy path now blocks; live loop uses the verified account-aware router. |
| MT5 execution errors | 130 | Live execution failures, including stale credential and terminal problems. | Centralized router and account verification required. |
| MT5 IPC timeout on initialize | 116 | Process-wide terminal contention or unstable terminal session, especially under multi-account attempts. | Existing clean shutdown/reconnect behavior retained; process-per-account remains required. |
| MT5 `InvalidToken` errors | 114 | Stored Fernet secret could not be decrypted, commonly due to changed/missing encryption key or stale plaintext records. | Account-specific failures must be surfaced; do not mark account connected. |
| MT5 terminal not running | 63 | Data requests were attempted without a usable terminal. | Readiness must fail before live operation. |
| Binance order-book requests for HDFCBANK | 59 | Indian equity sent to Binance order-book API. | Exchange-gated order-book method now returns unavailable without making request. |
| Binance order-book requests for XAUUSD=X | 40 | MT5/CFD gold sent to Binance API. | Same exchange gate. |
| Director malformed JSON | 100+ across variants | LLM output was not reliably machine-readable. | Strict object/decision/numeric validation; malformed output rejects. |
| Invalid Gemini API-key post-mortems | 22 | A configured or stale Gemini credential was invalid. | Missing/invalid providers no longer create approval evidence. |
| Missing broker-router factory import | 12 SL/TP close errors plus stale-position variants | Deployed caller imported `get_broker_router`, repository exposed only `get_router`. | Compatibility alias added. |
| TradingLoop missing `_get_ohlcv` | 11 | Older exit path referenced a method that did not exist. | Current exit path uses the shared data-router fallback. |
| Scanner `None` unpack errors | 2+ | Scanner/data failure was not always converted to a structured no-score result. | Per-symbol scanner containment remains active; this should be monitored in the next run. |

The historical log also contains repeated `MT5_PASSWORD did not decrypt` warnings. The engine then fell back to treating the value as plaintext. This is compatible with legacy accounts but is not a clean credential state. Re-add or migrate each account using one stable `ACCOUNT_ENCRYPTION_KEY`; never copy an encrypted password between machines without the same key.

The logs repeatedly report **Funding Pips account size `$10,000`** while the connected MetaQuotes-Demo account reports approximately **$3,000.03** balance/equity. Those are not necessarily contradictory: a prop-firm challenge may be nominally $10,000 while the connected demo account is funded with $3,000. However, the engine must store them separately. The onboarding patch now treats `ACCOUNT_SIZE` as the nominal challenge size and `BROKER_VERIFIED_BALANCE` as the broker-returned balance.

## 2. Account lifecycle: required behavior

The correct lifecycle is now:

1. Validate account type, broker, profile name, credentials, requested trading mode, and prop-firm selection.
2. Connect to the broker using a fresh account-specific adapter.
3. Read broker-returned account identity, server, balance, equity, and currency.
4. For MT5, require the returned login and server to match the submitted login and server exactly, case-insensitively for the server string.
5. Persist the encrypted credential only after verification succeeds.
6. Persist the real broker balance in the account record; persist the nominal prop challenge size separately in the profile environment.
7. Write the per-account profile environment with restrictive file permissions where supported.
8. Start the MT5 account through the process supervisor and wait for `/api/ping` before reporting the process as running.
9. Assign only validated strategies to the account; account toggles can narrow access but cannot override `live_ready=false`.
10. Continuously monitor health, last check time, restart count, and last error.

The current implementation has the account route and supervisor route registrars wired into `quant_server.py`. Fresh databases now apply the supervisor-column migration automatically. The read-only verifier reports all account rows with credentials redacted and does not expose secret values.

## 3. Connectivity verification

The verifier is `tools/verify_runtime.py`. Run it on the terminal host as follows:

```bash
python tools/verify_runtime.py
python tools/verify_runtime.py --probe
```

The `--probe` mode performs only read-only checks. It does **not** call `order_send`, `create_order`, or any close method. For MT5 it checks terminal initialization, account identity, server identity, account information, configured symbol presence/visibility, tick availability, and trade permission status. For Binance it checks public futures reachability and whether authenticated credentials are present. It also checks the account database, optional schema columns, dependencies, and strategy registry.

In the sandbox run on 24 August 2026:

| Check | Result |
|---|---|
| MT5 package | Missing; the sandbox cannot connect to a Windows MT5 terminal. |
| Binance public futures endpoint | Reachable; BTC price response was present. |
| Authenticated Binance credentials | Unset. |
| MT5 login/server environment | Unset. |
| Account rows in sandbox database | None. |
| Strategy registry | Loaded successfully. |

This is a valid infrastructure result, not a broker-account verification. The operator must run the verifier on the actual Windows/MT5 host after loading the correct profile environment.

## 4. Strategy allocation and historical validation

The current registry contains separate strategy identities rather than one opaque hybrid score. The recorded walk-forward report provides the following evidence:

| Strategy | Instrument | Fold evidence | Decision |
|---|---|---|---|
| `orderflow_btc` | BTC-USD/BTCUSDT family | 3/3 profitable folds; mean +0.417R, standard deviation 0.215R; latest fold +0.125R with 27.1% win rate. | Live-ready in registry, but the high concurrent-trade count requires exposure controls. |
| `trend_pa_forex` | EURUSD=X | 3/3 profitable folds; mean +0.191R, standard deviation 0.095R. | Live-ready in registry, subject to real account/data verification. |
| `trend_pa_gold` | GC=F/XAUUSD=X | 2/3 profitable; mean +0.112R; latest fold -0.077R. | Not live-ready. |
| `trend_pa_forex_gbp` | GBPUSD=X | 1/3 profitable; mean +0.013R. | Not live-ready. |
| `trend_pa_forex_jpy` | USDJPY=X | 1/3 profitable; mean -0.095R. | Not live-ready. |
| `trend_pa_forex_aud` | AUDUSD=X | 2/3 profitable; mean +0.033R with high variance 0.353R. | Not live-ready. |
| `structure_silver` | SI=F/XAGUSD=X | 1/3 profitable; mean -0.080R. | Not live-ready. |
| `mean_reversion_ranging` | Instrument-specific only | No current passing WFO evidence. | Explicitly paper-only. |
| `volatility_breakout_xau` | XAUUSD=X | Research candidate; earlier folds weak/negative, latest evidence promising but insufficient. | Explicitly paper-only. |

These are **recorded repository results**, not a fresh completed run from this sandbox. A new multi-strategy WFO attempt was stopped after several minutes with no output because the current network-bound yfinance path stalled. It is not honest to represent that stopped run as a completed backtest. The existing report remains useful as historical evidence, but the next promotion decision should use a locally cached, timestamped dataset and a bounded runner.

## 5. ML, LLM council, and self-generation

The ML regime classifier is not equivalent to a trained production model merely because `xgboost` is installed. The repository’s production artifact was absent in the earlier audit, so the active path was rules/fallback logic. The LSTM path is optional and now includes a purge gap around future-label validation, but it still needs enough point-in-time trade-outcome rows before it can be considered production evidence.

The LLM council should be treated as an **advisory veto layer**, not as the source of entry, stop, target, or position size. The patch now requires all roles—Quant, Sentiment, Devil’s Advocate, Risk, and Director—to return valid model-backed evidence. If a provider is missing, returns malformed JSON, returns an unknown decision, or emits non-finite multipliers, the council rejects. The deterministic risk engine remains authoritative.

The strategy evolver remains intentionally constrained. LLM-generated proposals may be useful research hypotheses, but a heuristic “score” is not a backtest. Automatic staging or promotion is disabled unless a real chronological backtest adapter proves the proposal out of sample with costs, slippage, and exposure limits.

## 6. Order flow and footprint reality check

The current order-flow implementation is not a true footprint for MT5 forex, gold, Indian equities, or yfinance data. It is an OHLCV-based proxy unless venue-specific trade/depth data is available. A real footprint requires timestamped trades or market-by-price/order-book events from the same venue where the order will execute. Binance USD-M futures can provide native trades/depth; MT5 can provide broker-specific ticks and, where supported, market book; Zerodha requires its own authenticated market-data path.

The patch prevents a false footprint interpretation by blocking order-book requests for symbols whose configured exchange is not Binance. Missing or ambiguous order flow must produce `WAIT`, not approval. This is essential: a “balanced” or unavailable book is not evidence for a trade.

## 7. Files changed in this patch

The key changes in this phase are:

- `core/account_connector.py`: MT5 login/server identity matching.
- `core/account_endpoints_patch.py`: profile validation, explicit paper default, explicit live confirmation, restrictive profile-file permissions, and separate nominal prop size versus broker balance.
- `core/database_manager.py`: automatic account-supervisor schema migration.
- `brokers/broker_router.py`: compatibility alias `get_broker_router`.
- `core/trading_loop.py`: centralized live execution through the broker router.
- `core/execution_agent.py`: unauthenticated legacy Binance execution is blocked.
- `core/data_agent.py`: Binance USD-M client and internal-symbol normalization; exchange-specific order-book gating.
- `brokers/binance_agent.py`: USD-M futures symbol normalization.
- `intelligence/agent_council.py`: complete-evidence requirement and strict verdict validation.
- `core/strategy_registry.py`: explicit paper-only flags for unvalidated candidates.
- `tools/analyze_engine_logs.py`: reproducible log aggregation.
- `tools/summarize_log_errors.py`: normalized root-cause ranking.
- `tools/verify_runtime.py`: read-only account, dependency, broker, symbol, and strategy verifier.
- `tests/test_end_to_end_runtime.py`: regression coverage for the onboarding and runtime guarantees.

## 8. Validation performed

The following checks passed after the main runtime patches:

```text
compileall: passed
focused end-to-end/runtime/council/router/execution/data tests: 21 passed
```

A later Binance-routing test run also passed:

```text
15 passed in 159.32s
```

After the final account-size and registry metadata edits, run the final command below before deployment:

```bash
python -m compileall -q core brokers engines intelligence ml tools tests
python -m pytest -q tests/test_end_to_end_runtime.py tests/test_safe_ai_gates.py tests/test_council.py tests/test_router.py tests/test_execution.py tests/test_paper_mode.py tests/test_volatility_breakout.py
```

## 9. Deployment checklist

Before any live mode is considered:

| Gate | Required evidence |
|---|---|
| Account identity | Read-only verifier shows requested login and server match broker response. |
| Credential state | No `InvalidToken` or plaintext-fallback warning; one stable encryption key is installed. |
| Terminal/process | MT5 terminal is running, process supervisor reports `running`, `/api/ping` responds, and health checks are current. |
| Data | Every enabled symbol has fresh bars/ticks from the same execution venue; no Binance requests for non-Binance assets. |
| Strategy | Account allocation contains only intended strategy IDs; `live_ready` gate remains on. |
| Backtest | At least three chronological out-of-sample folds with costs, slippage, concurrency, and a positive worst-fold expectancy. |
| ML | Model artifact exists, training rows meet the configured minimum, validation is purged, and fallback status is visible. |
| Council | Provider health is measured; malformed/unavailable roles reject; council cannot alter deterministic risk size. |
| Paper soak | At least 20–30 sessions of paper operation with no orphan positions, no duplicate entries, and reconciled fills/exits. |
| Live enablement | Explicit operator confirmation only after all preceding gates pass. |

## Final recommendation

Do not enable the full multi-strategy live portfolio from the attached logs. First deploy the patch, run the read-only verifier on the actual terminal host, migrate/re-add accounts with a stable encryption key, and perform a paper soak. The current safest candidate allocation is narrow: only the individually validated EURUSD and BTC order-flow strategies may remain eligible for later live review; gold, silver, GBPUSD, USDJPY, AUDUSD, mean reversion, and the new XAUUSD breakout remain paper-only or disabled until new local, cost-aware, chronological validation proves otherwise.

## References

1. Repository sources: `core/account_connector.py`, `core/account_endpoints_patch.py`, `core/process_supervisor.py`, `core/database_manager.py`, `brokers/broker_router.py`, `core/data_agent.py`, `intelligence/agent_council.py`.
2. Historical strategy evidence: `docs/wfo_phase4_results.md`.
3. Read-only verification tool: `tools/verify_runtime.py`.
4. Attached-log analysis outputs: `engine_log_analysis.out` and `normalized_log_errors.csv`.
