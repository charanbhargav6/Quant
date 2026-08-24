# ML, LLM Council, Order-Flow, Footprint, and Long-Horizon Trading Audit

**Repository:** [charanbhargav6/Quant](https://github.com/charanbhargav6/Quant)  
**Audit date:** 24 August 2026  
**Author:** **Manus AI**

> **Finance disclosure:** I am not a licensed financial advisor—this is technical research and analysis, not guaranteed advice; investing and trading carry risk you bear.

## Executive conclusion

The repository contains substantial ML, LLM, sentiment, self-tuning, order-flow, and strategy code, but several components were not operating as their documentation implied. The most important issue was not model quality; it was **control-flow safety and data validity**.

The LLM council had a fail-open Director fallback: if all model calls failed or the JSON verdict could not be parsed, it returned `decision=execute`. The council’s open-trade monitor also called a nonexistent `self.director.llm` object. The ML path was not using a trained model in the audited environment: the XGBoost model file was absent and the active classifier reported `rules only`. The deep LSTM weights file existed, but PyTorch was not installed in the audited environment and `USE_DEEP_REGIME` was false. The order-flow outputs on the repository’s real EURUSD, GBPUSD, and XAUUSD data all returned `C / No volume data`; the implementation is an OHLCV heuristic, not a true footprint or order-book engine.

I applied safety-oriented changes. The council now fails closed on unavailable or malformed verdicts, unknown decisions are rejected, and open-trade reviews use the council’s supported call path and default to `hold`. Missing or insufficient delta data now returns `WAIT` instead of `ENTER`, and the trading loop rejects a signal when the delta gate has no usable feed. Jarvis `NO_TRADE` is now an actual veto. The strategy evolver no longer stages heuristic LLM proposals by default and no longer auto-promotes staged parameters unless explicitly enabled. ML signal timestamps now use the market-feed timestamp when available, and LSTM validation includes a purge gap around future-label windows.

These changes make the repository safer and more honest about what is working. They do **not** establish that any strategy is profitable or make live trading appropriate.

## Runtime audit results

The audit was performed without placing trades or calling a broker. It used the repository’s local code and real bundled OHLCV data.

| Component | Observed state | Assessment |
|---|---|---|
| LLM council | Gemini, Groq, OpenRouter, and the user-supplied council OpenAI-compatible key were unset in the audit environment; the council entered its fallback path. | Previously unsafe because fallback approved. Now fail-closed after the patch. |
| Built-in sandbox OpenAI key | Present in the sandbox environment, but not exposed to or reused as a user repository credential. | Not evidence that the user’s council is configured. The repository needs its own approved provider key or endpoint. |
| XGBoost regime model | `ml/models/regime_classifier.pkl` absent. | Active regime classifier used rules only. No trained XGBoost inference occurred. |
| Deep LSTM regime model | `ml/models/deep_regime_lstm.pth` present, but `USE_DEEP_REGIME=false`; PyTorch was unavailable in the audited sandbox. | Not active by default and not independently validated for this repository’s current data. |
| Real-data regime outputs | EURUSD `VOLATILE`, GBPUSD `TRENDING_UP`, XAUUSD `RANGING` on the last 500 local M15 bars. | Rule outputs are operational but should be treated as a regime filter, not a profitability signal. |
| Order-flow proxy | EURUSD, GBPUSD, and XAUUSD returned `grade=C`, `score=0`, `reason=No volume data`, and null delta. | No usable footprint evidence in the bundled files. |
| Strategy evolver | It generated LLM diagnoses and heuristic proposal scores, but its “test” step was not a real backtest. | It could stage changes without empirical validation. Now disabled by default. |

The absence of volume is especially important. A footprint chart requires information about executed trades and, ideally, the aggressor side; a candle’s open and close cannot reliably reconstruct which trades lifted the offer or hit the bid. The existing proxy assigns volume using candle geometry, so its “delta” is an estimate, not observed order flow.

## LLM council: what was wrong and what it should become

### Existing architecture

The council has Quant, Sentiment, Devil’s Advocate, Risk, and Director roles. It makes up to five sequential model calls: two pitches, two critiques, and one verdict. The default configuration assigns Gemini to the Director and Groq to the other roles, with Ollama as a local fallback. Before this audit, the code did not have a usable OpenAI-compatible provider path, even though the broader repository referenced OpenAI keys elsewhere.

The most serious defect was the Director fallback. When every provider was unavailable, or when the response was malformed, it returned:

```json
{"decision": "execute", "reasoning": "Fallback: LLM unavailable, default approval"}
```

That is the opposite of a safe design. An LLM is not a source of truth for broker risk, price levels, or execution permission. It should be an optional, auditable advisory layer over deterministic signal and risk gates. If the advisory layer is unavailable, the system should either reject the new trade or use a separately defined deterministic policy—not silently approve.

The post-entry council monitor was also broken. `evaluate_open_trade()` called `self.director.llm._client`, but `DirectorAgent` and `BaseAgent` do not define `llm`. The exception was swallowed and returned `hold`, so it usually did not move a position, but the intended review never reached a model.

### Safe council protocol for swing and positional research

For longer-horizon trades, the council should not debate vague chart opinions. It should receive a frozen, timestamped evidence packet generated by deterministic code. That packet should include the instrument, timeframe, signal timestamp, higher-timeframe trend, volatility state, entry, invalidation level, target, expected holding period, spread, estimated slippage, event-risk calendar, correlation exposure, and actual order-flow evidence when available.

Each role should return strict JSON with `vote`, `confidence`, `reasons`, `invalidations`, `data_timestamp`, and `horizon`. The Director should not be allowed to rewrite the stop or target freely. It should return only `execute`, `reject`, or `hold`, while deterministic code retains control of risk sizing and levels. A proposed swing trade should be rejected if any hard condition fails: stale data, missing price/volume evidence, invalid RR, event blackout, excessive correlation, maximum drawdown breach, or disagreement on direction between the rule engine and the risk manager.

The required logic is closer to this:

```text
if deterministic_risk_gate_fails: reject
if data_is_stale_or_incomplete: reject
if council_unavailable_or_invalid: reject
if risk_manager_vote != approve: reject
if director != execute: reject
otherwise: send to the existing paper/live gate
```

A council should be evaluated by **incremental value**, not by how persuasive its prose sounds. Compare the strategy with and without the council over identical timestamps and costs. Measure changes in expectancy, drawdown, trade count, latency, and false vetoes. If the council does not improve the distribution after costs, remove it from the entry path and retain it for research or post-trade review.

## ML pipeline assessment

### Rule/XGBoost regime classifier

The default active path is `RegimeClassifier` unless `USE_DEEP_REGIME=true`. It runs rule-based regime detection first and uses a persisted XGBoost model only when a trained model is loaded and its prediction confidence reaches the configured threshold. In the audit environment, `regime_classifier.pkl` was missing, so the classifier reported `trained=false` and `using=rules only`.

The training pipeline is structurally better than a random split because it uses time-ordered validation and has a minimum validation-accuracy gate. However, accuracy is not the correct production objective by itself. Regime classification should be evaluated by the **downstream trading impact**: conditional expectancy, drawdown, turnover, and stability by instrument and market regime. A model can improve classification accuracy while selecting regimes that do not improve trading returns.

### Deep LSTM path

The LSTM uses a 24-bar sequence of four normalized inputs: open, high, low, and volume. Its labels inspect the next 24 bars to assign trending, ranging, or volatile outcomes. The labels are appropriate as future outcomes for supervised learning, but the validation split must respect their 24-bar forward horizon. I added a purge gap of 24 sequences between the training tail and validation tail.

The LSTM is not trained on the repository’s own completed trade outcomes, despite the broader ML documentation describing paper-trade learning. It is trained from downloaded Yahoo hourly data for a small fixed symbol set. That can be a valid research model, but it should be described as **market-state classification from external OHLCV**, not as a learned model of this bot’s trade outcomes.

The deep path should not be enabled simply because a `.pth` file exists. Before promotion, record model hash, training data range, label distribution, per-symbol confusion matrix, balanced accuracy, calibration, and downstream trading metrics. Keep the rule classifier as a fallback, but log every fallback event so the operator knows when the ML model is not actually deciding.

### Feature-engineering issues fixed or still requiring work

The feature logger previously used the process wall clock for `utc_hour`, `day_of_week`, and `signal_time`. This can disagree with the actual market bar when data arrives late or is replayed. The code now uses the feed timestamp when available. The feature extractor also uses a swing-proximity helper that scans the supplied frame. It is safe only when the frame ends at the signal timestamp; a backtest must never pass future rows into that helper.

The training table must be audited for duplicate signals, unresolved outcomes, position-management changes, and missingness. A model trained on a mixture of strategy versions without a version field can learn implementation changes rather than market behavior. Add `strategy_version`, `data_source`, `feature_timestamp`, `outcome_close_timestamp`, and `execution_cost` to every training row before using ML for sizing or entry selection.

## Order-flow and footprint assessment

The active `engines/orderflow_strategy.py` builds a volume profile by assigning each candle’s total volume to its typical price. It approximates delta by classifying the last three candles as bullish or bearish according to candle direction. The auxiliary `intelligence/order_flow.py` uses candle-range geometry to estimate signed delta. Both are useful as lightweight heuristics when only OHLCV exists, but neither is a true footprint.

The bundled local data has no usable volume for the audited instruments, so the live output is correctly now treated as unavailable rather than positive confirmation. The original path could proceed when data was missing; that was changed to wait or reject. The trading loop also rejects a signal when the lower-timeframe feed needed for the delta gate is missing.

For a real footprint implementation, the system needs at least timestamped trades with price, size, and aggressor side. For a real depth view, it needs bid/ask levels, quantities, sequence numbers, and a correct local-book reconstruction. The exact data source must match the traded venue. A Binance futures feed is not a footprint for an MT5 CFD symbol, and a COMEX futures footprint is not identical to a broker’s XAUUSD CFD.

## API and data-provider choices

The repository cannot “get” secret API keys automatically. The user must create them with the provider, review permissions and terms, then provide them through the environment or a connector. Never place keys in source code, a Git repository, a URL, or a command line that may be logged.

| Requirement | Practical option | What it provides | Key or account requirement |
|---|---|---|---|
| LLM debate | Gemini, Groq, OpenRouter, or an OpenAI-compatible endpoint | Role-based text analysis and structured verdicts | User-created provider key; the patch adds `COUNCIL_OPENAI_API_KEY`, `COUNCIL_OPENAI_BASE_URL`, and `COUNCIL_OPENAI_MODEL`. |
| MT5 forex/gold ticks | MetaTrader 5 terminal integration | Tick history and, when broker supplies it, market-depth subscription | MT5 terminal/account login; no separate generic LLM-style API key. Broker support varies. |
| Binance crypto market data | Binance public WebSocket market streams | Trades, aggregate trades, book ticker, and depth streams | Public market-data streams generally do not require trading credentials; account/order actions do. |
| Exchange futures historical/live order book | Databento | Trades, order book schemas, timestamps, side, action, sequence, and replay | User-created Databento API key; paid coverage depends on dataset and plan. |
| News sentiment | NewsAPI, GNews, or a curated RSS/primary-event feed | Headlines for a macro context layer | User-created key for paid/API sources; RSS availability and terms vary. |

The official MetaTrader documentation lists `copy_ticks_range`, `market_book_add`, and `market_book_get` for Python integration, but the market-depth functions only help when the connected terminal and broker provide depth [1]. Databento documents live subscriptions for trades and order-book schemas and exposes fields such as event timestamp, side, price, size, action, and sequence [2]. Binance documents public derivatives WebSocket market streams and depth subscriptions, which are appropriate for Binance-listed instruments rather than MT5 CFDs [3].

### Recommended model assignments

Use deterministic code for price, risk, event filters, and position sizing. If the council is retained for a paper-only advisory gate, use a lower-cost model for Quant, Sentiment, and Devil’s Advocate, and a stronger structured-output model for Director. The repository’s raw HTTP clients should be upgraded to strict schema validation and provider-specific token parameters before production use. Do not let model output directly set risk beyond bounded, deterministic checks.

A practical starting configuration is:

```text
Quant:           low-cost fast model
Sentiment:       low-cost fast model, or deterministic event classifier
DevilsAdvocate:  independent low-cost model/provider
Risk:            deterministic risk engine plus optional model critique
Director:        stronger structured-output model
```

Independence matters more than the number of agents. Five calls to the same model with the same evidence do not constitute five independent opinions. At least one role should be deterministic and at least one should be required to find a falsifiable invalidation condition.

## Other strategy paths

The repository contains several strategy families, but “implemented” does not mean “validated.” The previous strategy work found the following current posture:

| Strategy | Current assessment | Required next step |
|---|---|---|
| Trend price action | Some EURUSD evidence was stronger than other forex symbols; GBPUSD, USDJPY, and AUDUSD were inconsistent in the recorded WFO report. | Re-run per-symbol WFO with current costs and event filters. |
| Mean reversion | The RR gate was corrected to use the declared final target. Local candidate tests remained negative out of sample. | Keep paper-only; audit exits and collect a clean forward sample. |
| Order-flow proxy | Functional as an OHLCV heuristic, but not a footprint and currently has no usable local volume. | Replace with venue-matched trade/depth ingestion or keep disabled. |
| Crypto order-flow adapter | Strategy logic exists, but it must be tested with actual Binance futures data and concurrency/cost assumptions. | Validate on exchange-native trades and depth; do not transfer MT5 conclusions to crypto. |
| Deep LSTM regime | Optional model path; not active by default and not trained on the bot’s trade outcomes. | Purged validation, calibration, per-symbol results, and downstream uplift test. |
| Strategy evolver | Previously proposed changes using heuristic scores rather than real backtests. | Keep proposal staging disabled until a real WFO adapter is connected. |
| Dynamic TP and council monitor | Dynamic-TP code is present; open-trade council path was structurally broken and is now repaired to hold safely on failure. | Benchmark against fixed exits before allowing extensions or council overrides. |

## Long-term swing/positional architecture

For swing or positional trades, use a slower decision cadence and separate the entry timeframe from the monitoring timeframe. A candidate can be generated on the daily or 4-hour chart, confirmed on the 1-hour chart, and monitored with daily event and risk checks. The holding-period label must be defined before testing, for example a 3–20 trading-day horizon, and all costs, overnight financing, gaps, and corporate or macro events must be represented.

The minimum deterministic gate should be:

1. The signal is generated from a timestamped, closed higher-timeframe bar.
2. The market regime and directional bias agree across the selected timeframes.
3. The stop is placed at a structural invalidation level and the final target passes the RR floor after spread, slippage, and financing assumptions.
4. The instrument is not inside a defined event blackout or liquidity-risk window.
5. The portfolio has room for the trade after correlation and concentration limits.
6. The LLM council, if enabled, can only approve or reject the already-defined trade; it cannot invent levels or override hard risk limits.
7. Any unavailable data, model timeout, schema failure, or disagreement produces `reject` for a new entry and `hold` for an open position.

## Validation plan before any live promotion

The next validation cycle should use six or more chronological folds with a purge gap appropriate to the label horizon. Report each fold separately, plus the pooled result. Include spread, slippage, swap/funding, commissions, latency, gap behavior, partial fills, and the actual dynamic-TP lifecycle. Do not select a strategy solely because its latest fold is positive.

For the council, run an ablation study with the same deterministic signals: no council, one council model, and full council. Record latency and token cost in addition to trading metrics. Promote only if the council improves net expectancy or materially reduces drawdown without causing unacceptable missed trades.

For ML, compare rules-only versus ML-enabled routing on the same timestamps. Require a model artifact hash, training cutoff, feature schema version, per-instrument metrics, calibration, and a rollback file. Retraining should create a candidate artifact and report; it should not replace the active model automatically.

## Changes applied in this audit

The following source-level changes are included in the delivered patch bundle:

| File or area | Change |
|---|---|
| `intelligence/agent_council.py` | Fail-closed verdict; repaired open-trade call; configurable OpenAI-compatible provider; no mocked approval count. |
| `core/trading_loop.py` | Council exceptions reject; unknown council decisions reject; Jarvis `NO_TRADE` vetoes; missing delta feed rejects; signal timestamps are feed-aligned. |
| `intelligence/order_flow.py` | Missing/insufficient volume or candles return `WAIT`, not `ENTER`. |
| `intelligence/strategy_evolver.py` | Heuristic staging disabled by default; automatic promotion disabled by default. |
| `ml/train_lstm.py` | Added purge gap around future-label validation boundary. |
| `ml/feature_engineering.py` | Uses supplied signal timestamp for time features when available. |
| `.env.example` | Added safe toggles and user-supplied council provider settings without credentials. |
| `tests/test_safe_ai_gates.py` | Regression coverage for fail-closed council, hold-on-error, missing-volume wait, and disabled heuristic evolver. |
| `tools/audit_ml_council.py` | Reproducible no-trade audit of model presence, real-data regimes, and order-flow availability. |

## References

[1]: https://www.mql5.com/en/docs/python_metatrader5 "MQL5 Reference: Python Integration"

[2]: https://databento.com/docs/api-reference-live "Databento: API reference — Live"

[3]: https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/public "Binance Developer Documentation: Public USDⓈ-M Futures WebSocket Market Streams"

## Delivery disclosure

**Basis:** Repository source inspection, no-trade runtime audit, real bundled OHLCV parquet data, chronological strategy evaluations, and deterministic regression tests. **Time:** Audit performed on 24 August 2026; local price files and model artifacts were evaluated as found in the checkout, with no claim that they represent current live market conditions. **Assumptions:** Missing volume is treated as unavailable rather than neutral confirmation; unavailable or malformed LLM decisions reject new entries; open-trade model failures hold positions; API credentials must be user-created and permission-limited. **Sources & Confidence:** High confidence in the identified fail-open council, broken open-trade path, rules-only ML status, and OHLCV-only order-flow limitation because each was observed directly in source and runtime output. Moderate confidence in provider suitability because venue coverage, pricing, and broker permissions must be checked for the user’s actual account. **Compliance:** This is research and analysis only, not personalized financial advice.
