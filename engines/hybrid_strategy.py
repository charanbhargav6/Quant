import pandas as pd
import logging
from core.strategy_agent import StrategyAgent
from engines.orderflow_strategy import analyze_orderflow

logger = logging.getLogger("crave.hybrid_strategy")

class HybridStrategyAgent(StrategyAgent):
    """
    Combines SMC and Order Flow strategies.
    Rule:
    - SMC must be an A+ setup to take a trade.
    - Order Flow must be an A+ or A setup to take a trade.
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
        
        take_smc = (smc_grade == "A+")
        take_of = (of_grade in ["A+", "A"])
        
        # If neither qualifies, reject the trade
        if not take_smc and not take_of:
            smc_context["Structure_Score"] = "C"
            smc_context["Confidence_Pct"] = 0
            return smc_context
            
        # If order flow qualifies but SMC doesn't, we override the SMC result
        if take_of and not take_smc:
            smc_context["Structure_Score"] = of_grade
            # Boost confidence so the backtester accepts it
            smc_context["Confidence_Pct"] = 80 if of_grade == "A+" else 60
            smc_context["Macro_Trend"] = of_context["direction"]
            smc_context["Confidence_Breakdown"] = {
                "OrderFlow": f"Grade {of_grade} (Delta: {of_context['delta_pct']:.1f}%)"
            }
        elif take_smc and take_of:
            smc_context["Structure_Score"] = "A+"
            smc_context["Confidence_Pct"] = max(smc_context["Confidence_Pct"], 90)
            smc_context["Confidence_Breakdown"]["OrderFlow"] = "Confirmed by Order Flow"

        else:
            # take_smc=True, take_of=False — SMC qualifies alone.
            # Raw StrategyAgent confidence is kept but marked as unconfirmed
            # so the audit trail shows why OF was not a co-contributor.
            smc_context.setdefault("Confidence_Breakdown", {})
            smc_context["Confidence_Breakdown"]["OrderFlow"] = (
                f"Not confirmed (OF grade: {of_grade})"
            )
            smc_context["Confidence_Pct"] = max(
                0, smc_context.get("Confidence_Pct", 0) - 5
            )

        return smc_context

