# Strategy Architecture for Multi-Account, Multi-Strategy Trading — Solution Research

## The problem

CRAVE's trading logic currently runs as one monolithic `HybridStrategyAgent` that
internally blends two signal sources (SMC/Structure pattern-reading and OrderFlow
volume analysis) into a single confidence/grade number, with a hardcoded
ticker-based branch that routes Gold to a separate, different engine
(`gold_strategy.py`). This makes two things impossible: (1) telling whether a
poor result (e.g. Gold, EURUSD underperforming a random baseline) comes from the
Structure logic, the OrderFlow logic, or both — the blend hides it — and (2)
assigning genuinely different strategies to different trading accounts, since
there was only ever one strategy class wired into the backtest/live pipeline at a
time. "Best" here means: an architecture that (a) lets each signal family be
independently validated and swapped without touching the execution engine, (b)
doesn't force artificial separation between concepts that are actually the same
thing (SMC vs. ICT), and (c) is proven in production trading systems, not just
theoretically appealing.

## Candidates considered

1. **Strategy design pattern / pluggable adapter interface** — a common, swappable
   `analyze(data) -> signal` interface behind which any strategy can sit.
2. **Full framework migration** (NautilusTrader, Backtrader, Zipline-reloaded,
   QuantConnect/LEAN) — replace the custom backtest/live stack entirely with an
   existing open-source engine.
3. **Keep the monolithic Hybrid blend, just retune its internal weights** —
   the "don't refactor, just tune the SMC/OrderFlow mix ratio" option.
4. **Machine-learning ensemble/stacking** (train a meta-model on top of
   multiple raw signal outputs) instead of hand-written qualification rules.
5. **Split "SMC" and "ICT" into two separate strategies**, as originally proposed.

Eliminated: (5) is dropped after verification — multiple independent trading-
education sources agree ICT is the original methodology and SMC is a simplified,
community-evolved subset of the *same* mechanics (Order Blocks, FVGs, liquidity
sweeps, BOS/CHoCH map 1:1 between the two)<cite index="40-1,40-2">SMC did not develop independently and arrive at the same conclusions as ICT — it was derived from ICT, and the fundamental premise and key concepts are identical between the two, just named differently</cite>. Building them as separate strategies would mostly
duplicate one engine under two labels, not add real diversity.

## Ranked options

### 1. Strategy design pattern with a pluggable adapter interface — WINNER

**What it is:** each signal family (Structure/SMC-ICT, OrderFlow, Trend/Price-Action)
implements a common interface (`prepare(data)` / `analyze(data, i) -> signal`) that
the backtest/live engine calls generically, without knowing which concrete
strategy it's running.

**Pros:**
- This is exactly the architecture production trading platforms converge on. A
  production-style platform walkthrough describes strategies as sitting "on top of
  the data layer... designed as modular plugins," each implementing a standard
  interface so they "can be swapped in or out without rewriting the engine," which
  "scales teams as well as systems"<cite index="8-1">Strategies sit on top of the data layer, and are designed as modular plugins... Each strategy implements a standard interface (generate_signals(data)) and can be swapped in or out without rewriting the engine... Modularity scales teams as well as systems</cite>.
- NautilusTrader — a serious production-grade open-source platform — is built
  around exactly this: a `Strategy` base class with a companion config class, where
  "once a strategy is defined, the same source code can be used for backtesting
  and live trading"<cite index="16-1">A trading strategy inherits from Strategy... There are two main parts of a Nautilus trading strategy: the strategy implementation itself... and the optional strategy configuration... Once a strategy is defined, the same source code can be used for backtesting and live trading</cite>. That "same code, backtest and live" principle is precisely
  what the earlier `verify_hybrid_backtest.py` work was already trying to achieve
  by hand — this pattern formalizes it instead of reinventing it per-strategy.
- Minimal disruption: wraps existing, working code (`strategy_agent.py`,
  `orderflow_strategy.py`, `gold_strategy.py`) rather than rewriting any of it.
- Directly enables the multi-account goal: a distinct, independently-validated
  strategy object per account is now a real thing to assign, not a hypothetical.

**Cons:**
- Doesn't by itself fix a bad underlying strategy (Gold's directional logic is
  still Gold's directional logic) — it only isolates and exposes the problem.
- Some duplicated boilerplate across adapters (acceptable; each is small).

**Evidence:** Medium platform-design walkthrough; NautilusTrader official docs.

**Fit for constraints:** Excellent — reuses 100% of existing strategy code,
required zero new trading logic, and was implemented and verified against the
real repo in this session (see refactor below).

### 2. Full framework migration (NautilusTrader / LEAN / Zipline-reloaded)

**What it is:** replace the custom Python backtest/live stack with an existing
open-source or commercial engine that already has event-driven parity,
multi-asset support, and a mature strategy interface.

**Pros:** NautilusTrader in particular is explicitly asset-class-agnostic across
"FX, Equities, Futures, Options, Crypto and Betting"<cite index="22-1">The platform is also universal, and asset-class-agnostic — with any REST API or WebSocket feed able to be integrated via modular adapters. It supports high-frequency trading across a wide range of asset classes and instrument types including FX, Equities, Futures, Options, Crypto and Betting</cite>, which lines up with the
stated ambition to eventually add stocks/futures/options. It would also solve the
research/live parity problem structurally instead of by careful manual mirroring.

**Cons:** Real migration cost — a comprehensive 2026 framework comparison notes
NautilusTrader "demands more infrastructure thinking" and is "typically slower to
iterate" for rapid strategy discovery<cite index="17-1">NautilusTrader is one of the most serious open-source options for production-parity trading in Python... it demands more infrastructure thinking, and it's typically slower to iterate when your goal is high-throughput discovery across large universes and parameter grids</cite>. Backtrader, the more beginner-friendly
alternative, is explicitly flagged as effectively unmaintained and not viable for
new production systems<cite index="18-1">Backtrader, once a popular choice for Python-based strategy development, has been effectively unmaintained for several years and fails to install cleanly on Python 3.10+ without patching... not a production candidate for new systems</cite>. Zipline-reloaded is equities-only with no
live-trading support<cite index="18-1">Zipline-reloaded is still equities-only and lacks native live-trading support. It remains a research tool rather than a deployment framework</cite> — a dead end given the multi-asset, live-trading goals here.

**Fit for constraints:** Poor right now — this would mean rewriting a working,
already-debugged live trading loop and risk engine from scratch to gain
architectural purity the adapter pattern already delivers at near-zero cost.
Revisit only if/when the system outgrows a hand-rolled engine (e.g. genuine
multi-venue, sub-second execution needs).

### 3. Keep the monolithic blend, just retune the SMC/OrderFlow mix ratio

**Pros:** Zero refactor cost.

**Cons:** Doesn't answer the actual open question (is Gold's problem Structure or
OrderFlow?) — it just changes a blend ratio you can't attribute. Also directly
contradicts documented ensemble best practice: ensembling only helps when
component strategies are diverse and individually sound — "do not try and
ensemble very poor individual strategies and expect miracle improvements... you
still have to put effort in designing the individual strategies"<cite index="51-1">Ensemble strategies help reduce overfitting... For example, use one strategy that looks at volume, one that looks at price action, and one that looks at spreads or intermarket signals. Do not try and ensemble very poor individual strategies and expect miracle improvements... you still have to put effort in designing the individual strategies</cite>. Retuning a
blend ratio without knowing which side is broken is exactly the failure mode this
principle warns against.

**Fit for constraints:** Rejected — doesn't solve the diagnostic problem or the
multi-account problem.

### 4. ML ensemble/stacking meta-model over raw signals

**Pros:** In principle can learn optimal combination weights from data rather than
hand-picked qualification rules.

**Cons:** Adds a second, harder-to-validate model on top of already-unvalidated
base signals — and this system has already been burned once by an insufficiently
validated backtest (the original Silver "+120% APPROVED" result). Retail
backtest-to-live failure is well-documented at scale: a study of 888 strategies
found in-sample backtest Sharpe ratios had "almost zero predictive power" for live
returns, and "the more a quant optimized a strategy, the worse it performed
live"<cite index="52-1">Quantopian's 888-strategy study: Sharpe ratios from backtests had almost zero predictive power for live returns. The more a quant optimized a strategy, the worse it performed live</cite>. Adding an ML layer before the base adapters are even validated
standalone would compound that exact risk.

**Fit for constraints:** Premature — worth revisiting only after each adapter has
independent walk-forward evidence of a real edge. Not now.

## Recommendation

**Go with the pluggable strategy-adapter pattern (Option 1).** It's the
architecture production systems actually use, it required no changes to any
existing, working strategy logic, and — importantly — it was implemented and
verified in this session, not just recommended in the abstract:

- New `strategy_adapters.py` defines the adapter contract and four concrete
  adapters: `HybridAdapter` (existing blend, kept as default for backward
  compatibility), `StructureAdapter` (SMC/ICT alone — these are treated as ONE
  family per the research above, not split in two), `OrderFlowAdapter`
  (OrderFlow alone, mirroring Hybrid's own A+/A-only qualification bar exactly),
  and `TrendPriceActionAdapter` (gold_strategy.py's trend-pullback logic,
  generalized — confirmed by reading its code that nothing in it is actually
  gold-specific, it was just gated to gold by a ticker check).
- `verify_hybrid_backtest.py` was refactored to call `adapter.analyze()`
  generically instead of a hardcoded `if is_gold: ... else: ...` branch.
- Verified two ways: (1) the refactored default path produces byte-identical
  output to the pre-refactor version on the same test data, confirming zero
  behavior change for existing callers; (2) all four adapters were run
  independently on the same instrument and produced genuinely different
  signal counts and stats, confirming they're truly independent rather than
  silently collapsing to one engine.

**What would change this recommendation:** if the system later needs genuine
multi-venue execution, sub-second latency, or dozens of simultaneous strategies
across many asset classes at institutional scale, migrating to NautilusTrader
(Option 2) becomes the right call — but that's a "later, if this actually scales
that far" decision, not a now decision.

**One important caveat carried over from the adapter design itself:** the
OrderFlow adapter's edge depends on `volume` being real executed trade volume.
Multiple sources describe genuine footprint/order-flow analysis as needing real
Level 2/executed-trade data and being "data-intensive," with unreliable feeds
"distorting results"<cite index="27-1">Data-Intensive Setup: Requires high-quality, tick-level data, otherwise unreliable feeds can distort results</cite> — this is most trustworthy for crypto (real exchange
volume) and weakest for MT5-fed FX/CFD symbols (commonly tick-count, not real
volume). Keep this in mind when deciding which account/instrument gets the
OrderFlow-adapter strategy in the multi-account plan.
