# Phase 6a — Hybrid Confidence-Gate Fix: A/B Walk-Forward Validation

Generated 2026-08-15T15:03:31.933596+00:00 by run_phase6_hybrid_confidence_ab.py

Validates commit e6eace2's B+ confidence-penalty fix directly through engines/hybrid_strategy.py's blended HybridAdapter — the path Phase 4's isolated-adapter re-run never touched. Both variants below run on identical fold windows and identical underlying price data; only the confidence number attached to B+-tier, OF-unconfirmed signals differs between them.

## EURUSD=X (live gate: 45%)

| Fold | Window | Signals (pre→post) | ΔSignals | WR (pre→post) | Expectancy R (pre→post) | ΔExpectancy R |
|---|---|---|---|---|---|---|
| 1 | 2026-06-30 to 2026-07-15 | 47→52 | +5 | 31.9%→32.7% | +0.232→+0.284 | +0.052 |
| 2 | 2026-07-15 to 2026-07-30 | 34→41 | +7 | 32.4%→36.6% | -0.083→-0.033 | +0.05 |
| 3 | 2026-07-30 to 2026-08-14 | 46→50 | +4 | 13.0%→16.0% | -0.480→-0.364 | +0.116 |

**pre_fix verdict:** INCONSISTENT — treat as unproven / possibly curve-fit (mean expectancy -0.11R)

**post_fix verdict:** INCONSISTENT — treat as unproven / possibly curve-fit (mean expectancy -0.038R)

**Net effect of fix: +16 signals across all folds, avg Δexpectancy +0.073R/fold — PASS — reclaimed B+ trades hold expectancy, safe to ship for this symbol**

## GBPUSD=X (live gate: 40%)

| Fold | Window | Signals (pre→post) | ΔSignals | WR (pre→post) | Expectancy R (pre→post) | ΔExpectancy R |
|---|---|---|---|---|---|---|
| 1 | 2026-06-30 to 2026-07-15 | 32→78 | +46 | 6.2%→15.4% | -0.748→-0.372 | +0.376 |
| 2 | 2026-07-15 to 2026-07-30 | 46→87 | +41 | 43.5%→39.1% | +0.275→+0.175 | -0.1 |
| 3 | 2026-07-30 to 2026-08-14 | 35→86 | +51 | 17.1%→33.7% | -0.400→-0.022 | +0.378 |

**pre_fix verdict:** INCONSISTENT — treat as unproven / possibly curve-fit (mean expectancy -0.291R)

**post_fix verdict:** INCONSISTENT — treat as unproven / possibly curve-fit (mean expectancy -0.073R)

**Net effect of fix: +138 signals across all folds, avg Δexpectancy +0.218R/fold — PASS — reclaimed B+ trades hold expectancy, safe to ship for this symbol**

## USDJPY=X (live gate: 40%)

| Fold | Window | Signals (pre→post) | ΔSignals | WR (pre→post) | Expectancy R (pre→post) | ΔExpectancy R |
|---|---|---|---|---|---|---|
| 1 | 2026-06-30 to 2026-07-15 | 14→69 | +55 | 21.4%→11.6% | -0.304→-0.467 | -0.163 |
| 2 | 2026-07-15 to 2026-07-30 | 10→77 | +67 | 10.0%→18.2% | -0.660→-0.320 | +0.34 |
| 3 | 2026-07-30 to 2026-08-14 | 1→36 | +35 | 0.0%→22.2% | -1.040→-0.104 | +0.936 |

**pre_fix verdict:** INCONSISTENT — treat as unproven / possibly curve-fit (mean expectancy -0.668R)

**post_fix verdict:** INCONSISTENT — treat as unproven / possibly curve-fit (mean expectancy -0.297R)

**Net effect of fix: +157 signals across all folds, avg Δexpectancy +0.371R/fold — PASS — reclaimed B+ trades hold expectancy, safe to ship for this symbol**

## GC=F (live gate: 40%)

| Fold | Window | Signals (pre→post) | ΔSignals | WR (pre→post) | Expectancy R (pre→post) | ΔExpectancy R |
|---|---|---|---|---|---|---|
| 1 | 2026-06-30 to 2026-07-15 | 73→79 | +6 | 6.8%→6.3% | -0.714→-0.740 | -0.026 |
| 2 | 2026-07-15 to 2026-07-30 | 111→129 | +18 | 34.2%→31.0% | +0.162→+0.035 | -0.127 |
| 3 | 2026-07-30 to 2026-08-14 | 114→128 | +14 | 19.3%→17.2% | -0.199→-0.245 | -0.046 |

**pre_fix verdict:** INCONSISTENT — treat as unproven / possibly curve-fit (mean expectancy -0.25R)

**post_fix verdict:** INCONSISTENT — treat as unproven / possibly curve-fit (mean expectancy -0.317R)

**Net effect of fix: +38 signals across all folds, avg Δexpectancy -0.066R/fold — REVIEW — reclaimed B+ trades measurably drag expectancy down; consider a partial fix (e.g. raise the B+ floor slightly) rather than the full penalty removal for this symbol**

## SI=F (live gate: 40%)

| Fold | Window | Signals (pre→post) | ΔSignals | WR (pre→post) | Expectancy R (pre→post) | ΔExpectancy R |
|---|---|---|---|---|---|---|
| 1 | 2026-06-30 to 2026-07-15 | 44→57 | +13 | 56.8%→54.4% | +0.283→+0.200 | -0.083 |
| 2 | 2026-07-15 to 2026-07-30 | 61→67 | +6 | 29.5%→31.3% | +0.036→+0.080 | +0.044 |
| 3 | 2026-07-30 to 2026-08-14 | 89→98 | +9 | 27.0%→25.5% | -0.085→-0.132 | -0.047 |

**pre_fix verdict:** INCONSISTENT — treat as unproven / possibly curve-fit (mean expectancy 0.078R)

**post_fix verdict:** INCONSISTENT — treat as unproven / possibly curve-fit (mean expectancy 0.049R)

**Net effect of fix: +28 signals across all folds, avg Δexpectancy -0.029R/fold — REVIEW — reclaimed B+ trades measurably drag expectancy down; consider a partial fix (e.g. raise the B+ floor slightly) rather than the full penalty removal for this symbol**
