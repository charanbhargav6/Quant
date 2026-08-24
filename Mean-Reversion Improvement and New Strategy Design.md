# Mean-Reversion Improvement and New Strategy Design

**Repository:** [charanbhargav6/Quant](https://github.com/charanbhargav6/Quant)  
**Review date:** 24 August 2026  
**Author:** **Manus AI**

> This is a technical research report, not personalized financial advice and not a guarantee of profitability. Trading systems can lose money.

## Executive recommendation

The existing mean-reversion strategy had a material implementation defect: it calculated the minimum-RR gate from **TP1 at the Bollinger midline**, even though the declared final target was **TP2 at the opposite Bollinger band**. Once the global risk floor was aligned to 1.5:1, this defect suppressed nearly every setup because TP1 was often below 1.5R. The code now evaluates the declared final target for RR.

I also tested a stricter band-reentry variant and a separate volatility-breakout hypothesis on the repository’s real EURUSD, GBPUSD, and XAUUSD 15-minute parquet data. The re-entry variants did not produce positive out-of-sample expectancy. The volatility-breakout candidate produced a positive result only in the most recent XAUUSD fold, while earlier folds were negative or approximately flat. Therefore, the new strategy is implemented as an **opt-in paper-only research path**, not as a live strategy.

The correct goal is not simply a higher win rate. A strategy with a higher win rate can still lose money if its average loss, costs, or exit distribution are unfavorable. The selection criterion should be positive net expectancy, stable profit factor, tolerable drawdown, and consistent results across chronological folds.

## Strategy 1: corrected mean reversion

The current mean-reversion entry remains a fade of a Bollinger extreme in a `RANGING` or `UNKNOWN` regime. A long setup requires price near the lower band, RSI at or below the oversold threshold, no high-volume rejection, and the existing micro-structure confirmation score. A short setup is symmetric.

The corrected RR calculation is:

```text
long_RR  = (TP2 - entry) / (entry - SL)
short_RR = (entry - TP2) / (SL - entry)
```

The strategy’s partial target remains the Bollinger midline, while the final target remains the opposite band. This makes the RR gate consistent with the position schema and with the platform-wide 1.5:1 risk validator.

The strategy is still **not live-ready**. The saved paper state in the repository contains 29 trades at a 13.8% win rate and -0.015R expectancy. That state may be stale relative to the current market data, but it is sufficient evidence that the strategy should not be enabled live without a fresh paper run and a full trade-by-trade audit.

## Strategy 2: optional band-reentry improvement

A natural attempt to improve win rate is to avoid entering while price is still outside the band. The candidate waits for a prior close outside the band, followed by a close back inside the band with a directional candle. Optional filters include EMA200 alignment, ADX, session hours, and a midline target.

The test did not support promoting this rule. On the recent 40% chronological test window, `mid_target_reentry_trend` remained negative on every tested instrument. A range-filtered re-entry variant also remained negative. The test therefore rejects the hypothesis as a production fix rather than tuning it further until a favorable result appears.

## Strategy 3: volatility breakout candidate

The new candidate is deliberately separate from mean reversion. It assumes that a confirmed Bollinger expansion with trend strength is more likely to continue than revert.

### Entry rules

The candidate is considered only after at least 220 OHLC bars are available. A long requires the current close to break above the current upper Bollinger band after the previous close was not above the previous upper band, a bullish candle body, RSI at least 55, ADX at least 25, and a body size of at least 0.5 ATR. Shorts are symmetric. The implementation also requires a genuine first-close breakout rather than repeatedly entering on every candle already outside the band.

### Risk and exits

The stop distance is the larger of 1.2 ATR and 25% of current Bollinger-band width. TP1 is 1R and TP2 is 2R. The candidate is sized at 0.5% nominal risk before the existing prop-firm multiplier. In the current implementation it is only available when `ENABLE_VOLATILITY_BREAKOUT=true` and `TRADING_MODE=paper`; its registry entry has `live_ready=false`.

### Evidence from four chronological folds

The evaluator used four contiguous periods from the most recent 16,000 XAUUSD, EURUSD, and GBPUSD M15 bars. The first fold was reserved as indicator warm-up; folds 2–4 are reported below. Costs were subtracted from R using a conservative four-pip round trip for forex and six cents for XAUUSD. Entries occur on the next bar’s open, not on the signal close.

| Instrument | Fold 2 expectancy | Fold 3 expectancy | Fold 4 expectancy | Interpretation |
|---|---:|---:|---:|---|
| EURUSD | -0.485R | -0.612R | -0.590R | Consistently negative; reject. |
| GBPUSD | -0.410R | -0.443R | -0.392R | Consistently negative; reject. |
| XAUUSD | -0.173R | -0.016R | **+0.131R** | Recent improvement, but not stable enough for live use. |

For XAUUSD, the latest fold produced 125 trades, a 41.6% win rate, 1.242 profit factor, and +0.131R average expectancy. The previous fold produced 106 trades at 37.7% win rate and -0.016R expectancy, while the earlier fold produced 121 trades at 29.8% win rate and -0.173R expectancy. This is a promising **research observation**, not proof of a persistent edge.

## Why the strategy is not enabled live

A strategy should be promoted only when it survives repeated out-of-sample tests, realistic execution costs, and a forward paper period. The available results show that the breakout edge is concentrated in the latest XAUUSD regime. That could represent a real regime dependency, a data artifact, or ordinary variation. Enabling it live now would convert uncertainty into financial exposure.

The repository uses chronological evaluation because time-series validation should respect temporal order and keep future observations out of the information set [1] [2]. The new evaluator enters on the next bar and subtracts explicit cost assumptions, but it is still a research harness rather than a full broker simulator. It does not model every MT5 spread change, slippage tail, news gap, partial-fill behavior, or the repository’s complete dynamic-TP lifecycle.

## How to paper-test the candidate

```bash
cp .env.example .env

# Keep this unchanged while testing
TRADING_MODE=paper

# Opt in to the XAUUSD breakout research branch
ENABLE_VOLATILITY_BREAKOUT=true

python run_bot.py --paper --readiness
python run_bot.py --paper
```

The candidate remains subject to the existing position uniqueness guard and risk validator. Do not set `TRADING_MODE=live` for this candidate. The strategy registry intentionally marks `volatility_breakout_xau` as not live-ready.

## Recommended promotion criteria

The candidate should remain paper-only until it completes at least six rolling out-of-sample folds across multiple volatility regimes, with no single recent fold carrying the whole result. A practical research threshold is positive net expectancy after broker-specific spread and slippage, profit factor above 1.10, no materially negative fold, and a paper/live shadow sample large enough to compare expected versus realized fills.

For the mean-reversion family, I recommend first fixing the data and exit accounting, then collecting a clean paper sample. If the corrected strategy remains negative, the right action is to disable it for that instrument—not to keep lowering RSI thresholds or increasing the target until the backtest looks attractive.

## Implementation files

The code changes are in `engines/mean_reversion_engine.py` and the new `engines/volatility_breakout_engine.py`. The trading-loop integration is opt-in and paper-only. The strategy registry, `.env.example`, README, and regression tests were updated accordingly. The local evaluation scripts are included in the delivered patch bundle so the experiments are reproducible.

## References

[1]: https://arxiv.org/html/2512.12924v1 "Interpretable Hypothesis-Driven Trading: A Rigorous Walk-Forward Validation Framework for Market Microstructure Signals"

[2]: https://otexts.com/fpp3/tscv.html "Forecasting: Principles and Practice — Time series cross-validation"

## Delivery disclosure

**Basis:** Repository source inspection, real bundled OHLCV parquet data, chronological next-bar evaluation, conservative cost deductions, and focused regression tests. **Assumptions:** M15 data is sorted chronologically; signal decisions use information through the signal bar; entry occurs at the next bar’s open; costs are approximate and intentionally conservative. **Confidence:** High that the mean-reversion RR defect and strategy integration behavior are correctly identified; moderate for the breakout research conclusion because the available history and fold design are not sufficient to establish a durable edge. **Compliance:** Research and engineering analysis only; no individualized trade recommendation.
