# Quant Latest-Push End-to-End Wiring Report

**Repository:** `charanbhargav6/Quant`  
**Audited commit:** `0f6fb22f7fb5ad26f8603766c757b33f4eb6e0cb` (`Apply Quant End-to-End Trading Audit patches for broker routing and credential isolation`)  
**Audit date:** 24 August 2026  
**Scope:** Read-only integration audit. No live orders, account mutations, credential disclosure, or remote push were performed.

## Executive verdict

The latest push is **substantially wired correctly**, but it was not fully ready to be treated as a clean end-to-end release. The core runtime path is connected: account verification precedes onboarding persistence, broker routing is centralized, Binance symbols are normalized for USD-M futures, non-Binance order-book requests are blocked, failed strategies are rejected by the live-readiness gate, the council fails closed on incomplete model responses, and the paper/live boundary is explicit. These conclusions are supported by successful compilation and the targeted runtime suite on the exact pushed commit.

Three issues remained. First, the pushed `add_xm_account.py` helper was unsafe and also unusable against the current database API: it wrote a placeholder account before broker verification, passed a nonexistent `balance=` keyword instead of `capital=`, labeled the account `connected` without identity proof, and deleted unrelated Binance rows. Second, the pushed strategy registry marked only two non-live-ready candidates as `paper_only`; the failed or untested Gold, Silver, GBPUSD, USDJPY, AUDUSD, and catch-all Forex entries lacked the same explicit metadata. Third, the news sentinel returned a different schema when no headlines matched an asset, omitting the `count` field that populated responses and the repository smoke test expected. The first issue is a functional and safety blocker; the second is a consistency gap; the third is a small but reproducible API/test integration defect.

A focused follow-up patch was prepared and validated in a fresh worktree based on the pushed commit. After applying it, the wiring checker returned `ok: true`, compilation succeeded, and **23 targeted tests passed**. The patch is attached separately; it has **not** been merged or pushed to GitHub.

## What was verified

| Area | End-to-end path checked | Result | Evidence |
|---|---|---:|---|
| Account onboarding | Request → broker verification → encrypted persistence → profile/process metadata | Pass in mocked tests; real broker still host-only | [`core/account_endpoints_patch.py`](core/account_endpoints_patch.py), [`core/account_connector.py`](core/account_connector.py) |
| MT5 identity | Submitted login/server compared with broker-returned account identity | Pass in code path and tests | [`core/account_connector.py`](core/account_connector.py) |
| Database schema | Fresh database automatically receives broker/profile/process columns | Pass | [`core/database_manager.py`](core/database_manager.py), `test_database_initialization_applies_account_migration` |
| Broker execution | Live wrapper routes through the centralized router with `is_paper=False` | Pass | [`core/trading_loop.py`](core/trading_loop.py#L1010-L1035), [`brokers/broker_router.py`](brokers/broker_router.py) |
| Data routing | Crypto routes to Binance USD-M symbols; Forex/metals remain MT5; Zerodha remains India-specific | Pass by import/static checks and routing tests | [`core/data_agent.py`](core/data_agent.py), [`brokers/binance_agent.py`](brokers/binance_agent.py) |
| Order book | Binance order-book calls are prevented for MT5/Zerodha instruments | Pass | [`core/data_agent.py`](core/data_agent.py), `test_order_book_is_not_requested_for_non_binance_exchange` |
| News API schema | Empty asset sentiment returns the same `count` field as populated results | Pass in offline regression test | [`intelligence/news_sentinel.py`](intelligence/news_sentinel.py), `test_news_sentinel_empty_asset_result_has_stable_schema` |
| Strategy gate | Unknown or failed strategy IDs cannot pass the default live-readiness gate | Pass | [`core/strategy_registry.py`](core/strategy_registry.py) |
| Orderflow exposure | `orderflow_btc` checks open positions against `MAX_CONCURRENT_ORDERFLOW_BTC` before signal computation | Pass; default limit is 8 | [`core/trading_loop.py`](core/trading_loop.py#L847-L862), [`core/position_tracker.py`](core/position_tracker.py#L493-L501) |
| Council and ML safety | Incomplete council responses fail closed; ML timestamp/purge-gap safeguards are present | Pass in targeted suite | [`intelligence/agent_council.py`](intelligence/agent_council.py), [`ml/feature_engineering.py`](ml/feature_engineering.py), [`ml/backtest_runner.py`](ml/backtest_runner.py) |
| Paper mode | Paper is the default and candidate strategies remain paper-only | Pass after metadata completion | [`core/paper_trading.py`](core/paper_trading.py), [`core/strategy_registry.py`](core/strategy_registry.py) |

The exact remote commit was tested in an isolated worktree at `/tmp/quant_remote`. It compiled with `python3 -m compileall -q core brokers engines intelligence ml tools tests`, and the original focused suite passed **21/21 tests**. This validates that the push itself is importable and that its existing regression coverage passes; it does not prove connection to a real MT5 terminal or a funded broker account.

## Defects found in the pushed commit

### 1. XM helper was unsafe and broken

The pushed helper persisted the account before verification and used the following incompatible contract: `DatabaseManager.add_account(... balance=10000.0, status="connected", ...)`. The database API accepts `capital`, not `balance`. It also used a fabricated placeholder balance and executed `DELETE FROM accounts WHERE broker='binance' OR login='your_binance_key'`, which could remove unrelated account records. The pushed file can be reviewed at the [commit’s `add_xm_account.py`](https://github.com/charanbhargav6/Quant/blob/0f6fb22f7fb5ad26f8603766c757b33f4eb6e0cb/add_xm_account.py).

The replacement helper now calls `verify_and_connect` first, requires numeric MT5 login input, persists only after broker-returned identity and balance are available, encrypts the password, passes `capital=float(result["balance"])`, uses `status="verified"`, validates the strategy JSON, records `broker='mt5'` and a stopped profile process, and never deletes other accounts. It sends no order. Direct tests cover both failed verification and successful persistence.

### 2. Paper-only registry metadata was incomplete

The pushed registry explicitly marked `mean_reversion_ranging` and `volatility_breakout_xau` as paper-only, but not the other `live_ready=False` definitions. That ambiguity could cause dashboards, allocation tools, or future strategy-generation logic to treat failed or untested entries inconsistently even though the default live gate still rejects them. The follow-up patch adds `paper_only=True` to Gold, Silver, GBPUSD, USDJPY, AUDUSD, and untested Forex definitions. The checker now confirms that every non-live-ready definition has explicit metadata.

### 3. The checker had a false positive, not a runtime defect

The first version of the wiring checker searched the `_live_execute` source block for the absence of the string `ExecutionAgent`. The correct method’s explanatory docstring mentioned that legacy class, so the assertion failed despite the implementation calling the centralized router. The checker was corrected to remove the docstring from that assertion and positively require `get_router` plus `is_paper=False`. The runtime code was not weakened or changed to satisfy a bad textual assertion.

## Follow-up patch contents

The focused patch [`QUANT_LATEST_PUSH_FOLLOWUP.patch`](QUANT_LATEST_PUSH_FOLLOWUP.patch) contains four changes relative to the exact pushed commit:

| File | Change |
|---|---|
| `add_xm_account.py` | Replace unsafe placeholder/deletion helper with verify-before-persist, encrypted, correct-schema, no-order onboarding. |
| `core/strategy_registry.py` | Add explicit `paper_only=True` to every remaining `live_ready=False` strategy. |
| `tools/check_latest_push_wiring.py` | Add the import/routing/registry/XM/Binance wiring checker and correct its live-router assertion. |
| `intelligence/news_sentinel.py` | Return `count: 0` for empty asset sentiment results so the API schema is stable. |
| `tests/test_end_to_end_runtime.py` | Add three direct regressions covering failed XM verification, verified XM persistence, and the empty news result schema. |

The patch was checked with `git apply --check` and then applied to a new worktree created from `origin/main`. On that patched copy, compilation succeeded, the checker returned `ok: true` for all checks, and the focused suite passed **23/23 tests**. A broader non-network/non-broker selection reached **53 passing tests**, but four legacy tests failed because this sandbox lacks the optional `MetaTrader5` and `psutil` packages; those failures occurred in tests that expect to patch or measure those dependencies and do not represent live-order execution. The machine-readable checker result is attached as [`latest_wiring_check.json`](latest_wiring_check.json).

## What remains unproven

Actual broker connectivity is not proven inside this Linux sandbox. The `MetaTrader5` package and a live MT5 terminal are unavailable here, and no credentials were requested, printed, or used. The mocked onboarding tests prove ordering and safety contracts, not broker connectivity. Before any live or even broker-backed paper startup, run the repository’s read-only verifier on the Windows terminal host with the intended profile loaded:

```bash
python tools/verify_runtime.py --probe --profile <profile>
```

That host-side check must confirm the expected MT5 login, exact server string, terminal initialization, account information, symbol visibility, and valid ticks. It should be treated as a prerequisite, not as evidence that this sandbox audit already connected. No fresh walk-forward optimization run was completed in this audit; existing WFO reports remain historical evidence only. The standalone `tests/test_news.py` smoke script also performs live feed refreshes and previously exposed the missing empty-result `count` field; the corrected branch is covered offline, while the external feed itself was not used as release evidence.

The registry currently leaves only the historically stable `orderflow_btc` and EURUSD Trend/PA entries as `live_ready=True`, while the other strategies are blocked from live execution. Even for `orderflow_btc`, the implemented concurrency limit and all downstream risk gates should be verified on the host with realistic position-state fixtures before enabling live mode.

## Recommended merge sequence

Apply the attached patch to the branch containing commit `0f6fb22`, rerun the 23-test focused suite and the full repository test suite, review the resulting diff, and only then commit and push. Do not run the old XM helper from the pushed commit. Do not enable live trading merely because the code compiles; first run the host-side verifier, confirm profile credentials are loaded through the encrypted/environment path, and retain paper mode until broker identity and data-symbol checks pass.

## References

[1]: https://github.com/charanbhargav6/Quant/commit/0f6fb22f7fb5ad26f8603766c757b33f4eb6e0cb "Quant latest pushed commit"
[2]: https://github.com/charanbhargav6/Quant/blob/0f6fb22f7fb5ad26f8603766c757b33f4eb6e0cb/add_xm_account.py "Unsafe XM helper in pushed commit"

> **Finance/trading disclosure:** This is software-integration research and analysis, not personalized financial advice. Live trading carries risk, and any decision to enable it remains the user’s responsibility.
