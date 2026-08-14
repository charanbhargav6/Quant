import pandas as pd
import logging
from core.strategy_agent import StrategyAgent
from engines.orderflow_strategy import analyze_orderflow

logger = logging.getLogger("crave.hybrid_strategy")

class HybridStrategyAgent(StrategyAgent):
    """
    Combines SMC and Order Flow strategies.
    
    v11.1 FIX: Previous version was too restrictive — required SMC A+ OR
    OrderFlow A/A+ to take any trade. In live markets, SMC rarely produces
    A+ signals (needs perfect confluence of 7+ factors). This caused the
    bot to score Grade C and 0% confidence on EVERY instrument for days
    straight, taking zero trades.
    
    New Rule:
    - SMC B+ or above → tradeable (core SMC signal is good enough)
    - Order Flow A/A+ → tradeable (pure order flow signal)
    - SMC + OF confluence → grade boost (best possible trade)
    - SMC B or below AND no OF → Grade C (reject)
    """
    
    def analyze_market_context(self, symbol: str, df: pd.DataFrame, i: int, 
                               fvg_catalog: list, ob_catalog: list, structure: list, 
                               macro_news: str = "", **kwargs) -> dict:
        
        # 1. Get SMC analysis
        smc_context = super().analyze_market_context(
            symbol, df, i, fvg_catalog, ob_catalog, structure, macro_news
        )
        
        # Extract optimization params
        bins = kwargs.get("vp_bins", 50)
        delta_threshold = kwargs.get("delta_threshold", 20)
        
        # 2. Get Order Flow analysis
        of_context = analyze_orderflow(df, i, lookback=50, bins=bins, delta_threshold=delta_threshold)
        
        smc_grade = smc_context.get("Structure_Score", "C")
        of_grade = of_context.get("grade", "C")
        smc_conf = smc_context.get("Confidence_Pct", 0)
        
        # Extract letter grade from string like "Grade B+" or "A+"
        smc_letter = smc_grade
        for g in ("A+", "A", "B+", "B", "C"):
            if g in smc_grade:
                smc_letter = g
                break
        
        # ── Qualification rules ───────────────────────────────────────────
        # SMC qualifies if B+ or above (was: A+ only — too strict)
        take_smc = smc_letter in ("A+", "A", "B+")
        # Order Flow qualifies if A+ or A
        take_of  = of_grade in ("A+", "A")
        
        # ── If BOTH qualify → maximum confluence ──────────────────────────
        if take_smc and take_of:
            smc_context["Structure_Score"] = "A+"
            smc_context["Confidence_Pct"] = max(smc_conf, 90)
            smc_context.setdefault("Confidence_Breakdown", {})
            smc_context["Confidence_Breakdown"]["OrderFlow"] = "Confirmed by Order Flow"
            logger.info(f"[Hybrid] {symbol}: SMC({smc_letter}) + OF({of_grade}) = A+ confluence")
        
        # ── If only SMC qualifies → keep SMC grade as-is ─────────────────
        elif take_smc and not take_of:
            # SMC signal stands on its own. Keep the raw grade and confidence.
            #
            # FIX (confidence-gate dead zone): a flat -5pt penalty here was
            # applied to EVERY non-OF-confirmed signal, including B+ — but
            # B+ raw scores run 35-49 and live CONFIDENCE_GATES sit at
            # 40-45%. After -5, almost the entire B+ band landed at 30-39%,
            # permanently below gate, even though this class's own docstring
            # says "SMC B+ or above -> tradeable". Confirmed in ~3 weeks of
            # live logs: 30/35/40/45% were the ONLY confidence values ever
            # observed, and B+-alone signals essentially never executed.
            # A/A+ tier (50+ raw) has enough headroom above every gate to
            # absorb the penalty without changing the pass/fail outcome, so
            # only apply it there — B+ is the intended floor and should not
            # be discounted below its own qualifying threshold.
            smc_context.setdefault("Confidence_Breakdown", {})
            smc_context["Confidence_Breakdown"]["OrderFlow"] = (
                f"Not confirmed (OF grade: {of_grade})"
            )
            if smc_letter == "B+":
                smc_context["Confidence_Pct"] = smc_conf
            else:
                smc_context["Confidence_Pct"] = max(0, smc_conf - 5)
            logger.info(f"[Hybrid] {symbol}: SMC({smc_letter}) qualifies alone, OF={of_grade}")
        
        # ── If only OF qualifies → use OF direction and grade ────────────
        elif take_of and not take_smc:
            smc_context["Structure_Score"] = of_grade
            smc_context["Confidence_Pct"] = 80 if of_grade == "A+" else 60
            smc_context["Macro_Trend"] = of_context["direction"]
            smc_context["Confidence_Breakdown"] = {
                "OrderFlow": f"Grade {of_grade} (Delta: {of_context['delta_pct']:.1f}%)"
            }
            logger.info(f"[Hybrid] {symbol}: OF({of_grade}) overrides SMC({smc_letter})")
        
        # ── Neither qualifies → reject ───────────────────────────────────
        else:
            smc_context["Structure_Score"] = "C"
            smc_context["Confidence_Pct"] = 0
            logger.debug(f"[Hybrid] {symbol}: Neither SMC({smc_letter}) nor OF({of_grade}) qualify")

        return smc_context
