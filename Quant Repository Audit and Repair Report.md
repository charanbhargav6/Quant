# Quant Repository Audit and Repair Report

**Repository:** [charanbhargav6/Quant](https://github.com/charanbhargav6/Quant)  
**Review date:** 24 August 2026  
**Scope:** Runtime failure diagnosis, paper/live execution plumbing, broker routing, risk gates, strategy status, and conservative strategy staging.

> I am an AI, not a licensed financial advisor—this is technical analysis of a trading system, not guaranteed investment advice. Trading can result in loss of capital.

## Executive conclusion

The repository was not failing for one isolated strategy bug. Its primary failure was **runtime plumbing drift**: the README and service file described a paper mode and readiness command that the current Python entry point did not implement, while the live loop was hard-wired toward live broker routing. The paper engine module had been deleted even though several modules still imported it. In addition, the service referenced `/home/ubuntu/CRAVE`, the actual checkout was `Quant`, and the service passed `--paper` even though `run_bot.py` rejected that argument.

The repository also contained a broker-truth mismatch. The instrument table routes forex and metals through **MT5**, but the market metadata and example environment file described them as **Alpaca**. On Linux, WSL, or an AWS host without a supported running MT5 terminal, that path can start the bot but repeatedly fail at connection or order placement. The router’s former exception fallback also risked bypassing centralized account and readiness checks.

The strongest existing validation evidence is limited to two strategy/instrument combinations: `orderflow_btc` on BTC-USD and `trend_pa_forex` on EURUSD=X. The recorded WFO report marks Gold, Silver, GBPUSD, USDJPY, and AUDUSD as inconsistent or negative. The current paper state contains 29 trades with a 13.8% win rate, -0.015R expectancy, and a -0.61 Sharpe estimate, so the readiness gate correctly remains failed.

## What was found

| Area | Finding | Consequence |
|---|---|---|
| Paper mode | `core/paper_trading.py` was missing although `position_tracker.py`, `trade_recap.py`, and other code imported it. `config.PAPER_TRADING` was also absent. | Paper accounting, readiness, and reporting could fail at runtime. |
| CLI | `--readiness` was documented but rejected by `run_bot.py`; `--paper` was used by the service but was not accepted. | The documented operating and validation workflow could not be followed. |
| Mode selection | The runner printed `LIVE`, and the loop routed directly to the broker router without a paper execution branch. | A fresh checkout did not have a coherent safe simulation path. |
| Exception handling | Router exceptions fell through to `_live_execute()`. | A centralized-router failure could bypass broker/account/readiness controls. |
| Broker routing | `INSTRUMENTS` routes forex/gold to MT5, while `MARKETS` and `.env.example` described Alpaca. | Users could configure the documented broker and still receive no orders. |
| MT5 dependency | Forex/metals execution depends on the MetaTrader 5 Python package and a running terminal on a supported host. | Linux/WSL/AWS deployments can start but cannot execute MT5 orders. |
| Readiness gate | `ENFORCE_LIVE_READY_GATE` defaulted to false. | Failed or untested strategies could be treated as executable if account toggles allowed them. |
| Emergency close API | Risk and security callers passed `is_paper=...` to `BrokerRouter.execute()`, whose signature did not accept it. | Paper emergency closes could raise a `TypeError` instead of closing the simulated position. |
| State paths | Several modules calculated the repository root one or more levels too high; the AWS node also referenced `/home/ubuntu/CRAVE`. | State, caches, models, and logs could be split across unrelated directories. |
| Duplicate entries | Repeated five-minute scans were not globally prevented from opening another position on the same instrument. | Position stacking could magnify exposure beyond the intended risk model. |

## Changes implemented

| File | Change | Why it matters |
|---|---|---|
| `core/paper_trading.py` | Restored the deleted simulator; fixed limit-order direction handling; added paper entry and close execution; preserved equity, R-multiple, and readiness accounting. | Re-establishes an actual paper-trading path used by the existing callers. |
| `config/config.py` | Added `TRADING_MODE`, `PAPER_TRADING`, `MIN_TRADES_FOR_LIVE`; aligned forex/gold market metadata to MT5. | Creates one runtime mode source of truth and removes broker ambiguity. |
| `run_bot.py` | Added mutually exclusive `--paper` and `--live`; added `--readiness`; paper is the safe default; CLI overrides environment mode. | Makes the documented commands operational and prevents accidental live startup. |
| `core/trading_loop.py` | Routes paper entries to the simulator; fails closed on live router exceptions; uses paper equity for sizing; logs the selected mode; prevents duplicate instrument entries; tags the new MR strategy. | Keeps paper and live execution separate, safer, and more observable. |
| `brokers/broker_router.py` | Added optional `is_paper` compatibility and paper entry/close dispatch. | Repairs risk/security emergency-close calls and prevents accidental live dispatch for paper requests. |
| `core/strategy_registry.py` | Defaults live-readiness enforcement to on; corrected per-instrument documentation; staged `mean_reversion_ranging` as not live-ready. | Unvalidated strategies now fail closed in live mode. |
| `engines/mean_reversion_engine.py` | Aligned the minimum RR requirement to 1.5:1, matching `RiskAgent`. | Prevents the strategy from generating setups that the global validator later rejects. |
| `crave.service`, `.env.example`, `README.md` | Corrected path, flags, variable names, broker documentation, and setup instructions. | Removes deployment and onboarding drift. |
| `backtesting/run_full_backtest.py`, `engines/compounding_engine.py`, `engines/economic_calendar.py`, `ml/backtest_runner.py`, `security/api_sentinel.py` | Corrected project-root fallbacks. | Keeps derived state, caches, and auxiliary outputs in the checked-out project. |
| `tests/test_paper_mode.py` | Added regression tests for paper fills, limit orders, paper readiness, and emergency closes. | Prevents the repaired mode plumbing from regressing. |

## Strategy status and recommendation

The repository’s recorded walk-forward report contains three non-overlapping folds from 10 July through 24 August 2026. Its results should be treated as **short-window evidence**, not a profitability guarantee. Time-series validation should preserve temporal order and evaluate decisions using only information available at the decision point [1] [2].

| Strategy | Instrument | Recorded WFO result | Current live status | Recommendation |
|---|---|---:|---|---|
| `orderflow_btc` | BTC-USD | 3/3 folds profitable; mean +0.417R; max concurrent 23 | Live-ready in registry | Keep only with the implemented concurrency cap, realistic fees/slippage, and forward paper monitoring. |
| `trend_pa_forex` | EURUSD=X | 3/3 folds profitable; mean +0.191R | Live-ready in registry | Keep as the only validated Trend/PA forex candidate; revalidate on a longer, rolling sample. |
| `trend_pa_gold` | GC=F/XAUUSD=X | 2/3 folds profitable; mean +0.112R; one negative fold | Disabled | Do not repair by simply lowering confidence. Re-test with volatility/session/news filters and realistic MT5 costs. |
| `structure_silver` | SI=F/XAGUSD=X | 1/3 folds profitable; mean -0.080R | Disabled | Keep disabled. The current evidence is negative. |
| `trend_pa_forex_gbp` | GBPUSD=X | 1/3 folds profitable; mean +0.013R | Disabled | Keep disabled; the mean edge is effectively zero. |
| `trend_pa_forex_jpy` | USDJPY=X | 1/3 folds profitable; mean -0.095R | Disabled | Keep disabled; the recorded mean is negative. |
| `trend_pa_forex_aud` | AUDUSD=X | 2/3 folds profitable; mean +0.033R with high variance | Disabled | Keep disabled; the worst fold was -0.451R and variance is too high. |
| `mean_reversion_ranging` | Instrument-specific WFO pending | Not tested | Staged, not live-ready | Paper-test and validate separately per instrument before any live enablement. |

### Strategy work performed

I did not promote a new strategy to live trading merely because it sounded plausible. Instead, the existing ranging-market fallback is now explicitly named `mean_reversion_ranging`, its RR floor is aligned with the global risk validator, and its registry flag is false until it earns instrument-specific walk-forward evidence. This is a safer improvement than tuning the losing pairs until an in-sample report looks attractive.

The most important strategy improvement is therefore **selection discipline**: keep validated strategies isolated by instrument, prevent duplicate entries, cap order-flow concurrency, and reject failed or unknown strategy IDs in live mode. A future strategy research branch should compare, per instrument, trend continuation, range mean reversion, and volatility-breakout variants using the same data, same costs, same entry timing, and rolling out-of-sample windows.

## Validation performed

The repaired repository passed syntax compilation and the deterministic regression suite:

```text
compileall: passed
23 tests passed in 11.74 seconds
```

The passing set includes the existing data-integrity, backtest-validity, strategy, data, router tests, plus the new paper-mode regression tests. The complete repository suite was also attempted, but it did not complete within 7 minutes and was interrupted after no tests completed; the remaining suite includes tests that depend on external services, credentials, or long-running behavior. That is an environment/test-harness limitation, not evidence that all of those tests are failing.

The repaired paper readiness command now runs successfully. Against the repository’s current saved paper state, it reports:

| Metric | Current saved value |
|---|---:|
| Paper trades | 29 |
| Win rate | 13.8% |
| Expectancy | -0.015R |
| Sharpe estimate | -0.61 |
| Maximum drawdown | 1.32% |
| Readiness | Failed |

## How to run the repaired workflow

```bash
# Install dependencies
python -m pip install -r requirements.txt

# Configure safe paper mode
cp .env.example .env
# Keep TRADING_MODE=paper while testing

# Run checks
python run_bot.py --status
python run_bot.py --readiness

# Start paper mode
python run_bot.py --paper

# Only after independent review, longer paper testing, and broker verification
python run_bot.py --live
```

For forex and metals, the current code path is MT5, not Alpaca. A live deployment therefore needs a compatible MT5 terminal, correct `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`, and a verified symbol mapping. The repository should not be expected to place MT5 trades from a Linux host that lacks that terminal environment.

## Remaining work before live trading

The next engineering milestone should be a proper end-to-end paper test over fresh market data, with a report that records signal count, fill assumptions, slippage, fees, max simultaneous heat, per-instrument results, and skipped-signal reasons. The WFO harness should then be extended beyond the current short three-fold window and should include a censor gap, strict next-bar execution, realistic broker costs, and a genuinely untouched out-of-sample period.

The system should also add explicit health metrics to the dashboard: latest data timestamp, last successful scan per symbol, last signal timestamp, last router decision, last broker error, account match count, and current mode. That will make “not trading” diagnosable as a specific gate or infrastructure failure rather than an absence of visible orders.

## References

[1]: https://arxiv.org/html/2512.12924v1 "Interpretable Hypothesis-Driven Trading: A Rigorous Walk-Forward Validation Framework for Market Microstructure Signals"

[2]: https://otexts.com/fpp3/tscv.html "Forecasting: Principles and Practice — Time series cross-validation"

[3]: https://github.com/charanbhargav6/Quant/blob/main/README.md "Quant README"

[4]: https://github.com/charanbhargav6/Quant/blob/main/core/trading_loop.py "Quant trading loop"

[5]: https://github.com/charanbhargav6/Quant/blob/main/brokers/broker_router.py "Quant broker router"

[6]: https://github.com/charanbhargav6/Quant/blob/main/docs/wfo_phase4_results.md "Quant Phase 4 walk-forward results"

## Delivery disclosure

**Basis:** This review uses the repository’s own signal, risk, routing, and WFO definitions; no profitability claim has been inferred beyond the recorded report. **Time:** Code and saved reports were reviewed as of 24 August 2026; the saved paper-state file itself has a last-updated timestamp of 27 July 2026. **Assumptions:** Paper mode is the default, live readiness is fail-closed, one open position per instrument is the conservative default, and new strategies require separate WFO before live enablement. **Sources and confidence:** Findings are based on local source inspection, repository tests, the saved WFO report, and the cited time-series validation references; confidence is high for the runtime/plumbing defects and moderate for strategy-quality conclusions because the available WFO window is short. **Compliance:** This is research and analysis only, not personalized financial advice.
