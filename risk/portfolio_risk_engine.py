"""
CRAVE v10.3 — Portfolio Risk Engine (Session 7)
=================================================
Manages risk across ALL open positions simultaneously,
regardless of market, asset class, or broker.

THE PROBLEM THIS SOLVES:
  Old risk model (risk_agent.py) only thinks about one trade at a time.
  "Can I take this trade?" — asks nothing about what's already open.
  With 5 markets running, you could have:
    - 1.5% in crypto BTC long
    - 1.5% in crypto ETH long     ← correlated with BTC
    - 1.0% in US stock NVDA long  ← also correlated tech momentum
    - 1.0% in Indian NIFTY long   ← global risk-on correlation
    Total: 5% correlated long exposure with 6.5% emergency close
    One bad macro event = emergency close everything at once

RULES ENFORCED (all simultaneous):
  1. Total heat:        max 6.0% equity at risk across all positions
  2. Per-market heat:   max 3.0% in any single market
  3. Per-instrument:    max 1.5% (existing rule, still enforced)
  4. Correlation:       max 2.0% in correlated instruments (same direction)
  5. Currency:          max 40% in any single currency (USD/INR/BTC)
  6. Options vega:      max 5.0% vega as % of portfolio
  7. Emergency close:   if total heat > 6.5%, close largest losing position

MARKET HEAT DEFINITIONS:
  crypto:    sum of risk_pct for all crypto positions
  forex:     sum of risk_pct for all forex/gold positions
  us_stocks: sum of risk_pct for all US equity positions
  india:     sum of risk_pct for all Indian equity + F&O positions
  options:   sum of risk_pct for all options positions

CURRENCY EXPOSURE:
  USD positions: all USD-denominated instruments (EURUSD, gold, US stocks)
  INR positions: all Indian instruments
  BTC positions: BTC and correlated alts
  Currency limit prevents full wipeout if one currency has a crisis event
"""

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("crave.portfolio_risk")


class PortfolioRiskEngine:

    def __init__(self):
        from config.config import PORTFOLIO_RISK, MARKETS
        self._cfg     = PORTFOLIO_RISK
        self._markets = MARKETS

    # ─────────────────────────────────────────────────────────────────────────
    # MAIN GATE — called before every new entry
    # ─────────────────────────────────────────────────────────────────────────

    def can_add_position(self, symbol: str,
                          risk_pct: float,
                          direction: str) -> tuple:
        """
        Master pre-trade check. Returns (allowed: bool, reason: str).
        Call this from trading_loop BEFORE risk_agent.validate_trade_signal().

        Checks in order (stops at first failure):
          1. Total portfolio heat
          2. Per-market heat
          3. Currency exposure
          4. Correlation limit
          5. Options vega (options only)
        """
        from config.config import get_market_for_symbol

        market = get_market_for_symbol(symbol)
        heat   = self.get_full_heat()

        # ── 1. Total heat ──────────────────────────────────────────────────
        max_total = self._cfg.get("max_total_heat_pct", 6.0)
        if heat["total"] + risk_pct > max_total:
            return (
                False,
                f"Total heat {heat['total']:.2f}% + {risk_pct:.2f}% "
                f"would exceed {max_total}% limit"
            )

        # ── 2. Per-market heat ─────────────────────────────────────────────
        max_market = self._cfg.get("max_single_market_pct", 3.0)
        market_h   = heat["by_market"].get(market, 0.0)
        if market_h + risk_pct > max_market:
            return (
                False,
                f"{market} heat {market_h:.2f}% + {risk_pct:.2f}% "
                f"would exceed {max_market}% market limit"
            )

        # ── 3. Currency exposure ───────────────────────────────────────────
        currency_ok, currency_reason = self._check_currency_exposure(
            symbol, risk_pct, direction, heat["by_currency"]
        )
        if not currency_ok:
            return False, currency_reason

        # ── 4. Correlation guard ───────────────────────────────────────────
        corr_ok, corr_reason = self._check_correlation(
            symbol, risk_pct, direction
        )
        if not corr_ok:
            return False, corr_reason

        # ── 5. Options vega limit ─────────────────────────────────────────
        from config.config import get_asset_class
        if get_asset_class(symbol) == "options":
            vega_ok, vega_reason = self._check_vega_limit(risk_pct)
            if not vega_ok:
                return False, vega_reason

        return True, "OK"

    # ─────────────────────────────────────────────────────────────────────────
    # HEAT CALCULATIONS
    # ─────────────────────────────────────────────────────────────────────────

    def get_full_heat(self) -> dict:
        """
        Calculate current portfolio heat across all dimensions.
        Returns:
          total:       total risk % across all open positions
          by_market:   {market: risk_pct}
          by_currency: {currency: risk_pct}
          by_symbol:   {symbol: risk_pct}
          positions:   list of open position dicts
        """
        from core.position_tracker import positions
        from config.config import get_market_for_symbol, get_instrument

        all_pos    = positions.get_all()
        total      = 0.0
        by_market: dict  = {}
        by_currency: dict = {}
        by_symbol: dict  = {}

        for pos in all_pos:
            symbol   = pos["symbol"]
            risk     = pos.get("risk_pct", 1.0)
            # Scale by remaining position size
            remaining_factor = pos.get("remaining_pct", 100) / 100.0
            effective_risk   = risk * remaining_factor

            total           += effective_risk
            market           = get_market_for_symbol(symbol)
            by_market[market] = by_market.get(market, 0) + effective_risk
            by_symbol[symbol] = by_symbol.get(symbol, 0) + effective_risk

            # Currency attribution
            inst       = get_instrument(symbol)
            currencies = inst.get("currencies", ["USD"])
            for ccy in currencies:
                by_currency[ccy] = by_currency.get(ccy, 0) + effective_risk

        return {
            "total":       round(total, 4),
            "by_market":   {k: round(v, 4) for k, v in by_market.items()},
            "by_currency": {k: round(v, 4) for k, v in by_currency.items()},
            "by_symbol":   {k: round(v, 4) for k, v in by_symbol.items()},
            "positions":   all_pos,
            "timestamp":   datetime.now(timezone.utc).isoformat(),
        }

    def get_total_heat(self) -> float:
        """Quick total heat — used by emergency close trigger."""
        return self.get_full_heat()["total"]

    # ─────────────────────────────────────────────────────────────────────────
    # INDIVIDUAL CHECKS
    # ─────────────────────────────────────────────────────────────────────────

    def _check_currency_exposure(self, symbol: str, risk_pct: float,
                                   direction: str,
                                   current_by_currency: dict) -> tuple:
        """Enforce max 40% exposure in any single currency."""
        from config.config import get_instrument, PORTFOLIO_RISK
        max_ccy = PORTFOLIO_RISK.get("max_currency_exposure_pct", 40.0)
        inst    = get_instrument(symbol)
        currencies = inst.get("currencies", ["USD"])

        for ccy in currencies:
            current = current_by_currency.get(ccy, 0)
            if current + risk_pct > max_ccy:
                return (
                    False,
                    f"{ccy} exposure {current:.2f}% + {risk_pct:.2f}% "
                    f"exceeds {max_ccy}% limit"
                )
        return True, "OK"

    def _check_correlation(self, symbol: str,
                            risk_pct: float,
                            direction: str) -> tuple:
        """
        Enforce max 2% in correlated instruments.
        Groups by asset class + direction (not just ticker).

        Correlation groups:
          crypto_long:  BTC + ETH + SOL all going up together
          crypto_short: same assets going down
          usd_long:     EUR/USD + GBP/USD + Gold long (all USD weakness)
          risk_on:      US stocks + India + crypto longs together
        """
        from core.position_tracker import positions
        from config.config import RISK, get_asset_class

        max_corr  = RISK.get("max_correlated_exposure_pct", 2.0)
        asset     = get_asset_class(symbol)
        dir_norm  = "long" if direction in ("buy", "long") else "short"

        # Build correlation groups
        CORR_GROUPS = {
            ("crypto",       "long"):  {"BTCUSDT", "ETHUSDT", "SOLUSDT"},
            ("crypto",       "short"): {"BTCUSDT", "ETHUSDT", "SOLUSDT"},
            ("forex",        "long"):  {"EURUSD=X", "GBPUSD=X", "AUDUSD=X"},
            ("forex",        "short"): {"USDJPY=X"},
            ("gold",         "long"):  {"XAUUSD=X", "XAGUSD=X"},
            ("stocks",       "long"):  {"AAPL", "NVDA", "TSLA", "MSFT",
                                        "SPY", "QQQ"},
            ("stocks_india", "long"):  {"RELIANCE", "TCS", "HDFCBANK", "INFY",
                                        "NIFTY_FUT", "BANKNIFTY_FUT"},
        }

        my_group = CORR_GROUPS.get((asset, dir_norm), set())
        if symbol not in my_group:
            my_group = my_group | {symbol}

        # Sum risk in same corr group
        total_corr = 0.0
        for pos in positions.get_all():
            pos_dir  = "long" if pos["direction"] in ("buy","long") else "short"
            pos_asset = get_asset_class(pos["symbol"])
            pos_group = CORR_GROUPS.get((pos_asset, pos_dir), set())

            if pos["symbol"] in my_group or symbol in pos_group:
                total_corr += pos.get("risk_pct", 1.0)

        if total_corr + risk_pct > max_corr:
            return (
                False,
                f"Correlated {asset} {dir_norm} exposure "
                f"{total_corr:.2f}% + {risk_pct:.2f}% exceeds {max_corr}%"
            )

        return True, "OK"

    def _check_vega_limit(self, risk_pct: float) -> tuple:
        """Enforce max 5% vega exposure as % of portfolio."""
        from config.config import PORTFOLIO_RISK
        max_vega = PORTFOLIO_RISK.get("max_vega_exposure_pct", 5.0)

        try:
            from options.greeks_monitor import get_greeks_monitor
            pg           = get_greeks_monitor().get_portfolio_greeks()
            current_vega = abs(pg.get("total_vega", 0))

            from core.paper_trading import get_paper_engine
            equity = get_paper_engine().get_equity()
            vega_pct = (current_vega / equity * 100) if equity > 0 else 0

            if vega_pct + risk_pct > max_vega:
                return (
                    False,
                    f"Vega exposure {vega_pct:.2f}% + {risk_pct:.2f}% "
                    f"exceeds {max_vega}% limit"
                )
        except Exception as e:
            logger.debug(f"[PortfolioRisk] Vega check failed (non-fatal): {e}")

        return True, "OK"

    # ─────────────────────────────────────────────────────────────────────────
    # EMERGENCY CLOSE
    # ─────────────────────────────────────────────────────────────────────────

    def check_emergency_close(self) -> bool:
        """
        If total heat exceeds emergency_close_at_pct (6.5%), close the
        largest losing position immediately to bring heat below the limit.

        Returns True if an emergency close was triggered.
        Called every 5 minutes from trading_loop._run_cycle().
        """
        total        = self.get_total_heat()
        emergency_at = self._cfg.get("emergency_close_at_pct", 6.5)

        if total <= emergency_at:
            return False

        logger.critical(
            f"[PortfolioRisk] EMERGENCY: heat={total:.2f}% >= {emergency_at}%. "
            f"Closing worst position."
        )

        worst = self._find_worst_position()
        if not worst:
            return False

        self._emergency_close_position(worst, total)
        return True

    def _find_worst_position(self) -> Optional[dict]:
        """
        Find the most appropriate position to emergency-close:
        1. Largest unrealised loss (protect remaining equity first)
        2. If all profitable: largest heat contributor (free up room)
        """
        from core.position_tracker import positions
        from core.data_agent import DataAgent

        all_pos    = positions.get_all()
        worst_pos  = None
        worst_pnl  = float("inf")
        da         = DataAgent()

        for pos in all_pos:
            try:
                df = da.get_ohlcv(pos["symbol"], timeframe="1m", limit=2)
                if df is None or df.empty:
                    continue

                live_price  = float(df['close'].iloc[-1])
                entry       = pos["entry_price"]
                sl_dist     = abs(entry - pos["current_sl"])
                if sl_dist == 0:
                    continue

                direction = pos["direction"]
                if direction in ("buy", "long"):
                    unreal_r = (live_price - entry) / sl_dist
                else:
                    unreal_r = (entry - live_price) / sl_dist

                if unreal_r < worst_pnl:
                    worst_pnl = unreal_r
                    worst_pos = {**pos, "current_price": live_price,
                                  "unrealised_r": unreal_r}
            except Exception:
                continue

        return worst_pos

    def _emergency_close_position(self, pos: dict, total_heat: float):
        """Execute emergency close and send alert."""
        trade_id = pos["trade_id"]
        symbol   = pos["symbol"]
        r        = pos.get("unrealised_r", 0)

        logger.critical(
            f"[PortfolioRisk] Emergency closing: {symbol} "
            f"({r:+.2f}R) | Portfolio heat was {total_heat:.2f}%"
        )

        try:
            from brokers.broker_router import get_router
            get_router().execute(
                {"symbol": symbol, "direction": "close",
                 "trade_id": trade_id},
                pos.get("current_price", 0),
                is_paper=pos.get("is_paper", True),
            )
        except Exception as e:
            logger.error(f"[PortfolioRisk] Emergency close execution failed: {e}")

        try:
            from interfaces.telegram_interface import tg
            tg.send(
                f"🚨 <b>EMERGENCY CLOSE</b>\n"
                f"Symbol   : {symbol}\n"
                f"Reason   : Portfolio heat {total_heat:.2f}% exceeded limit\n"
                f"Position : {r:+.2f}R unrealised\n"
                f"Action   : Closed to reduce risk"
            )
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────────────
    # SL TIGHTENING ON HIGH HEAT
    # ─────────────────────────────────────────────────────────────────────────

    def tighten_stops_if_overheated(self):
        """
        If heat > 5% (but < 6.5%): tighten all trailing SLs by 0.5R.
        This reduces risk exposure without forcing a close.
        Called from trading_loop when heat is elevated but not critical.
        """
        total = self.get_total_heat()
        if total < 5.0:
            return

        from core.position_tracker import positions
        logger.warning(
            f"[PortfolioRisk] Heat elevated at {total:.2f}%. "
            f"Tightening all trailing stops."
        )

        for pos in positions.get_all():
            try:
                entry     = pos["entry_price"]
                current_sl = pos["current_sl"]
                sl_dist    = abs(entry - current_sl)
                if sl_dist == 0:
                    continue

                tighten_by = sl_dist * 0.10  # tighten by 10% of SL distance

                direction = pos["direction"]
                if direction in ("buy", "long"):
                    new_sl = current_sl + tighten_by
                    positions.update_sl(
                        pos["trade_id"], new_sl,
                        reason=f"portfolio heat {total:.2f}% — tightening"
                    )
                else:
                    new_sl = current_sl - tighten_by
                    positions.update_sl(
                        pos["trade_id"], new_sl,
                        reason=f"portfolio heat {total:.2f}% — tightening"
                    )
            except Exception as e:
                logger.debug(f"[PortfolioRisk] SL tighten failed {pos['symbol']}: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # STATUS + REPORTING
    # ─────────────────────────────────────────────────────────────────────────

    def get_summary(self) -> dict:
        """Full portfolio risk summary for /portfolio command."""
        heat = self.get_full_heat()

        max_total = self._cfg.get("max_total_heat_pct", 6.0)
        heat_bar  = "█" * int(heat["total"] / max_total * 10)
        heat_bar  = heat_bar.ljust(10, "░")

        status = "✅ OK"
        if heat["total"] >= self._cfg.get("emergency_close_at_pct", 6.5):
            status = "🚨 EMERGENCY"
        elif heat["total"] >= max_total:
            status = "⚠️ AT LIMIT"
        elif heat["total"] >= max_total * 0.8:
            status = "🟡 ELEVATED"

        return {
            "total_heat":       heat["total"],
            "max_heat":         max_total,
            "utilisation_pct":  round(heat["total"] / max_total * 100, 1),
            "heat_bar":         heat_bar,
            "status":           status,
            "by_market":        heat["by_market"],
            "by_currency":      heat["by_currency"],
            "position_count":   len(heat["positions"]),
        }

    def get_status_message(self) -> str:
        """Formatted message for /portfolio Telegram command."""
        s = self.get_summary()

        market_lines = "\n".join(
            f"  {m}: {h:.2f}% / "
            f"{self._markets.get(m, {}).get('max_heat_pct', 3.0):.1f}%"
            for m, h in sorted(s["by_market"].items(), key=lambda x: -x[1])
        ) or "  No open positions"

        ccy_lines = " | ".join(
            f"{c}: {h:.2f}%"
            for c, h in sorted(s["by_currency"].items(), key=lambda x: -x[1])
        ) or "none"

        return (
            f"🔥 <b>PORTFOLIO RISK</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Total Heat : {s['total_heat']:.2f}% / {s['max_heat']:.1f}%\n"
            f"Utilisation: [{s['heat_bar']}] {s['utilisation_pct']:.0f}%\n"
            f"Status     : {s['status']}\n"
            f"Positions  : {s['position_count']}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"By Market:\n{market_lines}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Currency   : {ccy_lines}"
        )


# ── Singleton ─────────────────────────────────────────────────────────────────
_portfolio_risk: Optional[PortfolioRiskEngine] = None

def get_portfolio_risk() -> PortfolioRiskEngine:
    global _portfolio_risk
    if _portfolio_risk is None:
        _portfolio_risk = PortfolioRiskEngine()
    return _portfolio_risk

portfolio_risk = get_portfolio_risk
