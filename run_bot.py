"""
Trading Engine v11.0 — Main Entry Point
==========================================
Merged from CRAVE's dual entry points into single clean launcher.
Supports: full bot, lite bot, backtest, status, readiness, setup.

Run:  python run_bot.py
      python run_bot.py --status
      python run_bot.py --backtest
      python run_bot.py --readiness
      python run_bot.py --setup

Indian Market Strategy for Maximum Returns / Minimum Loss:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. NIFTY/BANKNIFTY futures during open drive (9:30-11:00 IST)
   and close drive (14:00-15:30 IST) — highest liquidity
2. FII/DII flow following — trade WITH institutional money
3. India VIX regime filter — high VIX = sell options, low VIX = directional
4. Weekly expiry strategies on Wednesday (BANKNIFTY) & Thursday (NIFTY)
5. PCR (Put-Call Ratio) for sentiment confirmation
6. Circuit breaker protection at 5%/10%/20% levels
7. Delivery volume analysis for swing trade conviction
"""

import os
import sys
import io
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
import socket
import logging
import argparse
import time
import schedule
from pathlib import Path
from datetime import datetime, timezone

# ── Project root on sys.path ─────────────────────────────────────────────────
ENGINE_ROOT = Path(__file__).parent
sys.path.insert(0, str(ENGINE_ROOT))

# ── Load secrets: .env ───────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv(ENGINE_ROOT / ".env")
except ImportError:
    print("Run: pip install python-dotenv")

# ── Logging ───────────────────────────────────────────────────────────────────
from config.config import LOGGING as LOG_CFG, LOGS_DIR
import logging.handlers

def setup_logging():
    level    = getattr(logging, LOG_CFG.get("level", "INFO"))
    handlers = []

    ch = logging.StreamHandler(stream=sys.stdout)
    ch.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S"
    ))
    handlers.append(ch)

    log_file = LOGS_DIR / "engine.log"
    fh = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=LOG_CFG.get("max_size_mb", 10) * 1024 * 1024,
        backupCount=LOG_CFG.get("backup_count", 5),
        encoding="utf-8"
    )
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    ))
    handlers.append(fh)

    logging.basicConfig(level=level, handlers=handlers, force=True)

setup_logging()
logger = logging.getLogger("engine.main")


# ─────────────────────────────────────────────────────────────────────────────

def detect_node() -> str:
    from config.config import NODES
    hostname = socket.gethostname().upper()
    for name, cfg in NODES.items():
        if any(p.upper() in hostname for p in cfg.get("hostname_patterns", [])):
            return name
    return "aws"


def print_banner(node: str, mode: str):
    print(f"""
╔══════════════════════════════════════════════════════╗
║            Trading Engine v11.0                      ║
║     Smart Money Concept Trading System               ║
║     Indian Market Integration Enabled                ║
╠══════════════════════════════════════════════════════╣
║  Node     : {node:<42}║
║  Mode     : {mode:<42}║
║  Time     : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'):<42}║
╚══════════════════════════════════════════════════════╝""")


def check_env() -> bool:
    missing_req = [k for k in ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
                   if not os.environ.get(k)]
    if missing_req:
        logger.warning(f"Missing env vars: {missing_req}. Edit .env")

    return bool(
        os.environ.get("BINANCE_API_KEY") or
        os.environ.get("ALPACA_API_KEY") or
        os.environ.get("ZERODHA_API_KEY")
    )


def _paper_status_msg() -> str:
    try:
        from core.paper_trading import get_paper_engine
        return get_paper_engine().get_status_message()
    except Exception as e:
        return f"📄 Paper engine not loaded: {e}"

def _regime_available() -> bool:
    try:
        from ml.regime_classifier import regime_model
        return regime_model._trained
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# FULL BOT
# ─────────────────────────────────────────────────────────────────────────────

def run_full_bot(node: str, mode: str):
    """
    Full bot — all modules.
    Paper mode by default. Flip to live after readiness gate passes.
    """
    logger.info(f"[Main] Starting FULL BOT — node={node} mode={mode}")

    # ── Infrastructure ────────────────────────────────────────────────────
    from core.streak_state       import streak
    from core.position_tracker   import positions
    from core.database_manager   import db
    from infra.state_sync        import sync

    # ── Intelligence ──────────────────────────────────────────────────────
    from infra.node_orchestrator  import orchestrator
    from interfaces.telegram_interface import tg
    from engines.daily_bias_engine  import bias_engine
    from engines.instrument_scanner import scanner

    # ── Trading ───────────────────────────────────────────────────────────
    from engines.dynamic_tp_engine  import dynamic_tp
    from engines.event_hedge_manager import event_hedge
    from core.trading_loop          import trading_loop

    # ── Start infrastructure ──────────────────────────────────────────────
    orchestrator.start()
    sync.start(is_active=orchestrator.is_active())
    tg.start()
    tg.start_schedulers()

    # ── Start trading engines ─────────────────────────────────────────────
    dynamic_tp.start()
    event_hedge.start()
    trading_loop.start()

    # Dashboard pusher (Supabase — always-on backend)
    try:
        from dashboard.supabase_pusher import get_pusher
        get_pusher().start()
        logger.info("[Main] Supabase dashboard pusher started.")
    except Exception as e:
        logger.warning(f"[Main] Dashboard pusher failed: {e}")

    # ── Options Engine ────────────────────────────────────────────────────
    from config.config import is_market_enabled
    if is_market_enabled("options"):
        try:
            from options.greeks_monitor import get_greeks_monitor
            get_greeks_monitor().start()
            logger.info("[Main] Greeks monitor started.")
        except Exception as e:
            logger.warning(f"[Main] Greeks monitor failed to start: {e}")

    # ── Portfolio Risk Engine ─────────────────────────────────────────────
    try:
        from risk.portfolio_risk_engine import get_portfolio_risk
        pr_status = get_portfolio_risk().get_summary()
        logger.info(
            f"[Main] Portfolio risk engine ready. "
            f"Current heat: {pr_status['total_heat']:.2f}%"
        )
    except Exception as e:
        logger.warning(f"[Main] Portfolio risk engine init failed: {e}")

    # ── Telegram commands ─────────────────────────────────────────────────
    tg.register_command("/portfolio", lambda args: tg.send(
        get_portfolio_risk().get_status_message()
    ))
    tg.register_command("/greeks", lambda args: tg.send(
        get_greeks_monitor().get_status_message()
    ))
    try:
        from options.options_engine import get_options_engine
        tg.register_command("/options", lambda args: tg.send(
            get_options_engine().get_status_message()
        ))

        from options.options_engine import iv_calculator
        def _handle_iv_cmd(args: str):
            symbol = (args.strip().upper() or "NIFTY")
            iv_data = iv_calculator.get_iv_rank(symbol)
            if not iv_data.get("available"):
                tg.send(f"📊 IV data unavailable for {symbol}. "
                        f"Builds after 20+ days of market data.")
                return
            tg.send(
                f"📊 <b>IV RANK: {symbol}</b>\n"
                f"IV Rank  : {iv_data['iv_rank']:.0f}%\n"
                f"Signal   : {iv_data['signal']}\n"
                f"Current IV: {iv_data['current_iv']:.1f}%\n"
                f"52W High  : {iv_data['high_52w']:.1f}%\n"
                f"52W Low   : {iv_data['low_52w']:.1f}%\n"
                f"Reason    : {iv_data['reason']}"
            )
        tg.register_command("/iv", _handle_iv_cmd)
    except Exception as e:
        logger.warning(f"[Main] Options Telegram commands skip: {e}")

    tg.register_command("/heat", lambda args: tg.send(
        get_portfolio_risk().get_status_message()
    ))

    def _tp_check_handler(args: str):
        result = dynamic_tp.force_check()
        tg.send(f"🔍 <b>TP Check Results</b>\n{result}")

    tg.register_command("/tp_check", _tp_check_handler)

    def _readiness_cmd(args):
        from core.paper_trading import get_paper_engine
        ready, report = get_paper_engine().check_readiness()
        for chunk in [report[i:i+3000] for i in range(0, len(report), 3000)]:
            tg.send(f"<pre>{chunk}</pre>")

    tg.register_command("/readiness", _readiness_cmd)
    tg.register_command("/paper", lambda args: (
        tg.send(_paper_status_msg())
    ))

    def _ml_cmd(args):
        try:
            from ml.regime_classifier import regime_model
            status = regime_model.get_status()
            lines = "\n".join(f"{k}: {v}" for k, v in status.items())
            tg.send(f"🤖 <b>ML STATUS</b>\n{lines}")
        except Exception as e:
            tg.send(f"🤖 ML not active: {e}")

    tg.register_command("/ml", _ml_cmd)

    def _ws_cmd(args):
        try:
            from interfaces.websocket_manager import get_ws
            tg.send(get_ws().get_status_message())
        except Exception as e:
            tg.send(f"📡 WS not active: {e}")

    tg.register_command("/ws", _ws_cmd)

    def _aws_start_cmd(args):
        try:
            from infra.aws_manager import get_aws
            get_aws().start_instance()
            tg.send("☁️ AWS instance starting...")
        except Exception as e:
            tg.send(f"☁️ AWS error: {e}")

    def _aws_stop_cmd(args):
        try:
            from infra.aws_manager import get_aws
            get_aws().stop_instance()
            tg.send("☁️ AWS instance stopping...")
        except Exception as e:
            tg.send(f"☁️ AWS error: {e}")

    tg.register_command("/aws_start", _aws_start_cmd)
    tg.register_command("/aws_stop",  _aws_stop_cmd)

    # ── India VIX status command ──────────────────────────────────────────
    def _vix_cmd(args):
        try:
            from config.config import INDIA
            if not INDIA.get("vix_enabled"):
                tg.send("📊 India VIX tracking is disabled.")
                return
            import yfinance as yf
            vix = yf.download("^INDIAVIX", period="5d", interval="1d",
                              auto_adjust=True, progress=False)
            if vix is not None and not vix.empty:
                latest = vix["Close"].iloc[-1]
                prev = vix["Close"].iloc[-2] if len(vix) > 1 else latest
                change = latest - prev
                regime = ("🔴 HIGH VOLATILITY" if latest > INDIA["vix_high_threshold"]
                         else "🟢 LOW VOLATILITY" if latest < INDIA["vix_low_threshold"]
                         else "🟡 NORMAL")
                tg.send(
                    f"📊 <b>INDIA VIX</b>\n"
                    f"Current : {latest:.2f}\n"
                    f"Change  : {change:+.2f}\n"
                    f"Regime  : {regime}\n"
                    f"Strategy: {'Sell options, reduce size' if latest > INDIA['vix_high_threshold'] else 'Directional trades OK'}"
                )
            else:
                tg.send("📊 India VIX data unavailable")
        except Exception as e:
            tg.send(f"📊 VIX error: {e}")

    tg.register_command("/vix", _vix_cmd)

    # ── Schedule daily pre-market at 06:30 UTC ────────────────────────────
    def daily_premarket():
        from interfaces.telegram_interface import tg
        logger.info("[Main] Daily pre-market analysis starting...")
        bias_engine.run_daily_analysis()
        scanner.run_daily_scan()

        # India-specific data
        from config.config import is_market_enabled
        if is_market_enabled("india"):
            try:
                from data.nse_bhavcopy import get_bhavcopy
                bc = get_bhavcopy()

                # Max pain calculation
                for symbol in ("NIFTY", "BANKNIFTY"):
                    mp = bc.calculate_max_pain(symbol)
                    if mp:
                        logger.info(
                            f"[Main] {symbol} Max Pain: "
                            f"{mp['max_pain_strike']} | {mp['interpretation']}"
                        )
                        try:
                            tg.send(
                                f"📊 <b>{symbol} MAX PAIN</b>\n"
                                f"Strike : {mp['max_pain_strike']}\n"
                                f"Spot   : {mp['current_spot']}\n"
                                f"{mp['interpretation']}"
                            )
                        except Exception:
                            pass

                # FII/DII
                fii_df = bc.get_fii_history(days=1)
                if not fii_df.empty:
                    latest = fii_df.iloc[-1]
                    logger.info(
                        f"[Main] FII: {latest['fii_net']:+,.0f} Cr | "
                        f"DII: {latest['dii_net']:+,.0f} Cr | "
                        f"Bias: {latest['bias']}"
                    )
            except Exception as e:
                logger.debug(f"[Main] India data fetch failed: {e}")

            # India VIX regime check
            try:
                from config.config import INDIA
                if INDIA.get("vix_enabled"):
                    import yfinance as yf
                    vix = yf.download("^INDIAVIX", period="2d", interval="1d",
                                      auto_adjust=True, progress=False)
                    if vix is not None and not vix.empty:
                        vix_val = vix["Close"].iloc[-1]
                        if vix_val > INDIA.get("vix_extreme_threshold", 28):
                            tg.send(
                                f"🚨 <b>INDIA VIX EXTREME: {vix_val:.1f}</b>\n"
                                f"Halting new Indian market entries."
                            )
                        elif vix_val > INDIA.get("vix_high_threshold", 20):
                            tg.send(
                                f"⚠️ India VIX HIGH: {vix_val:.1f}\n"
                                f"Switching to options selling mode."
                            )
            except Exception as e:
                logger.debug(f"[Main] VIX check failed: {e}")

        # Record daily IV snapshot (Options)
        from config.config import is_market_enabled
        if is_market_enabled("options"):
            try:
                from options.options_engine import iv_calculator
                for und in ("NIFTY", "BANKNIFTY"):
                    iv = iv_calculator._estimate_iv(und)
                    if iv:
                        iv_calculator.record_daily_iv(und, iv)
                        logger.info(f"[Main] Daily IV recorded: {und}={iv:.2f}")
            except Exception as e:
                logger.debug(f"[Main] IV snapshot failed: {e}")

        # Sunday weekly maintenance
        if datetime.now(timezone.utc).weekday() == 6:
            db.prune_old_ohlcv(keep_days=90)
            db.vacuum()
            logger.info("[Main] Weekly DB maintenance done.")

            try:
                from core.paper_trading import get_paper_engine
                pe           = get_paper_engine()
                ready, report = pe.check_readiness()
                stats        = pe.get_stats()
                total_trades = stats.get("total_trades", 0)
                min_trades   = pe._cfg.get("min_trades_for_live", 30)

                tg.send(
                    f"📊 <b>WEEKLY READINESS UPDATE</b>\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"Paper Trades : {total_trades} / {min_trades} needed\n"
                    f"Win Rate     : {stats.get('win_rate', 'N/A')}\n"
                    f"Expectancy   : {stats.get('expectancy_r', 'N/A')}\n"
                    f"Sharpe       : {stats.get('sharpe_ratio', 'N/A')}\n"
                    f"Max DD       : {stats.get('max_drawdown', 'N/A')}\n"
                    f"Return       : {stats.get('total_return', 'N/A')}\n"
                    f"Gate Status  : {'✅ PASSED' if ready else '❌ NOT YET'}\n"
                    f"━━━━━━━━━━━━━━━\n"
                    + (
                        "🎉 Ready for live! Run /readiness for full report."
                        if ready else
                        f"⏳ {max(0, min_trades - total_trades)} more trades to minimum."
                    )
                )

                if ready:
                    for chunk in [report[i:i+3000]
                                  for i in range(0, len(report), 3000)]:
                        tg.send(f"<pre>{chunk}</pre>")

            except Exception as e:
                logger.warning(f"[Main] Weekly readiness report failed: {e}")

    schedule.every().day.at("06:30").do(daily_premarket)

    if datetime.now(timezone.utc).hour >= 6:
        daily_premarket()

    # ── Schedule Zerodha daily token refresh at 03:30 UTC ─────────────────
    def zerodha_daily_login():
        from config.config import INDIA, is_market_enabled
        if not is_market_enabled("india"):
            return
        try:
            from brokers.zerodha_agent import get_zerodha
            get_zerodha().daily_login()
        except Exception as e:
            logger.warning(f"[Main] Zerodha daily login failed: {e}")

    schedule.every().day.at("03:30").do(zerodha_daily_login)

    # ── Schedule US stocks pre-close gap risk check at 19:45 UTC ──────────
    def us_pre_close_check():
        from config.config import is_market_enabled
        if not is_market_enabled("us_stocks"):
            return
        try:
            from core.position_tracker import positions
            from brokers.alpaca_stocks_agent import get_alpaca_stocks
            from core.data_agent import get_data_agent
            da = get_data_agent()
            for pos in positions.get_all():
                if pos.get("exchange") != "alpaca":
                    continue
                from config.config import get_asset_class
                if get_asset_class(pos["symbol"]) not in ("stocks", "indices"):
                    continue
                df = da.get_ohlcv(pos["symbol"], timeframe="1m", limit=2)
                if df is None or df.empty:
                    continue
                live_price = df["close"].iloc[-1]
                agent      = get_alpaca_stocks()
                should_close, reason = agent.should_close_before_overnight(
                    entry_price   = pos["entry_price"],
                    current_price = live_price,
                    stop_loss     = pos["current_sl"],
                    direction     = pos["direction"],
                )
                if should_close:
                    logger.warning(f"[Main] Pre-close: closing {pos['symbol']}: {reason}")
                    agent.close_position(pos["symbol"])
                    tg.send(
                        f"⏰ <b>PRE-CLOSE</b>: {pos['symbol']}\n"
                        f"Reason: {reason}"
                    )
                else:
                    positions.update_sl(
                        pos["trade_id"],
                        pos["entry_price"],
                        reason="pre-close gap protection"
                    )
        except Exception as e:
            logger.error(f"[Main] US pre-close check failed: {e}")

    schedule.every().day.at("19:45").do(us_pre_close_check)

    # ── Startup notification ──────────────────────────────────────────────
    mode_str  = "📄 PAPER" if mode == "PAPER" else "💰 LIVE"
    open_pos  = positions.count()

    paper_eq = "$10,000"
    try:
        from core.paper_trading import get_paper_engine
        pe = get_paper_engine()
        paper_eq = f"${pe.get_equity():,.2f}"
    except Exception:
        pass

    # Count enabled markets
    from config.config import MARKETS
    active_markets = [m for m, c in MARKETS.items() if c.get("enabled")]

    tg.send(
        f"🚀 <b>Trading Engine v11.0 Online</b>\n"
        f"Node     : {node}\n"
        f"Mode     : {mode_str}\n"
        f"Equity   : {paper_eq}\n"
        f"Open pos : {open_pos}\n"
        f"Can trade: {'✅' if streak.can_trade()[0] else '❌'}\n"
        f"Risk(A+) : {streak.get_current_risk_pct('A+'):.2f}%\n"
        f"DB size  : {db.get_db_size_mb()}MB\n"
        f"Markets  : {', '.join(active_markets)}"
    )

    logger.info(
        f"[Main] ✅ All modules running (v11.0).\n"
        f"       Trading loop: scanning every 5 min\n"
        f"       Dynamic TP:   checking every 15 min\n"
        f"       Event hedge:  checking every 5 min\n"
        f"       Daily bias:   runs at 06:30 UTC\n"
        f"       Regime filter: {'active (ML)' if _regime_available() else 'active (rules)'}\n"
        f"       Active markets: {', '.join(active_markets)}\n"
        f"       Use Telegram commands to control the bot."
    )

    # ── Main loop ─────────────────────────────────────────────────────────
    try:
        while True:
            schedule.run_pending()
            time.sleep(30)

    except KeyboardInterrupt:
        logger.info("[Main] Shutting down gracefully...")
        trading_loop.stop()
        dynamic_tp.stop()
        event_hedge.stop()
        sync.stop()
        tg.send("⏹️ Trading Engine shutdown.")
        logger.info("[Main] Shutdown complete.")


# ─────────────────────────────────────────────────────────────────────────────
# LITE BOT (phone / standby)
# ─────────────────────────────────────────────────────────────────────────────

def run_lite_bot(node: str, mode: str):
    """Lite bot for phone/standby. Monitors positions, no new signals."""
    logger.info(f"[Main] Starting LITE BOT on {node}")

    from core.streak_state       import streak
    from core.position_tracker   import positions
    from infra.node_orchestrator import orchestrator
    from interfaces.telegram_interface import tg
    from infra.thermal_monitor   import thermal
    from infra.state_sync        import sync
    from engines.dynamic_tp_engine  import dynamic_tp
    from engines.event_hedge_manager import event_hedge

    orchestrator.start()
    sync.start(is_active=False)
    tg.start()
    thermal.start()
    dynamic_tp.start()
    event_hedge.start()

    tg.send(
        f"📱 <b>Trading Engine Phone Node Online</b>\n"
        f"Mode: monitoring + standby\n"
        f"Open positions: {positions.count()}"
    )

    try:
        while True:
            schedule.run_pending()
            time.sleep(60)
    except KeyboardInterrupt:
        tg.send("⏹️ Phone node shutdown.")


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Trading Engine v11.0")
    parser.add_argument("--paper",      action="store_true")
    parser.add_argument("--live",       action="store_true")
    parser.add_argument("--backtest",   action="store_true")
    parser.add_argument("--status",     action="store_true")
    parser.add_argument("--setup",      action="store_true")
    parser.add_argument("--readiness",  action="store_true")
    parser.add_argument("--node",       type=str)
    args = parser.parse_args()

    node             = args.node or detect_node()
    has_exchange_keys = check_env()
    mode = "LIVE" if (args.live and has_exchange_keys) else "PAPER"

    if args.live and not has_exchange_keys:
        logger.warning("Live requested but no API keys. Defaulting to paper.")

    print_banner(node, mode)

    if args.status:
        from core.streak_state   import streak
        from core.position_tracker import positions
        print(streak.get_status_message())
        print()
        print(positions.get_summary_message())
        return

    if args.setup:
        run_setup_wizard()
        return

    if args.readiness:
        from core.paper_trading import get_paper_engine
        ready, report = get_paper_engine().check_readiness()
        print(report)
        return

    if args.backtest:
        run_backtest_mode()
        return

    from config.config import NODES
    can_run = NODES.get(node, NODES["aws"]).get("can_run", [])

    if "full_bot" in can_run or "signal_detection" in can_run:
        run_full_bot(node, mode)
    else:
        run_lite_bot(node, mode)


def run_backtest_mode():
    print("\n📊 Trading Engine v11.0 Backtest Mode")
    print("─────────────────────────────────────")
    symbol = input("Symbol (e.g. BTCUSD, XAUUSD, ^NSEI, RELIANCE.NS): ").strip()
    days   = int(input("Days (min 60 for gold, 30 for crypto): ").strip() or "60")
    conf   = int(input("Min confidence % (recommended 55): ").strip() or "55")
    try:
        from backtesting.backtest_agent_v10 import BacktestAgentV10
        bt = BacktestAgentV10()

        print("\n1. Standard backtest with fees")
        report = bt.run_backtest(symbol, days=days, min_confidence=conf)
        print(bt.format_report(report))

        if input("\nRun walk-forward validation? (y/n): ").strip().lower() == "y":
            total = int(input("Total days (e.g. 365): ").strip() or "365")
            print(f"\nWalk-forward: {total}d total, 180d train, 30d test...")
            wf = bt.run_walk_forward(symbol, total_days=total,
                                      min_confidence=conf)
            print(bt.format_walk_forward(wf))

        if input("\nRun multi-market comparison? (y/n): ").strip().lower() == "y":
            print("\nRunning on all markets...")
            multi = bt.run_multi_market(days=days, min_confidence=conf)
            print(bt.format_multi_market(multi))

    except Exception as e:
        print(f"Backtest error: {e}")


def run_setup_wizard():
    print("\n🔧 Trading Engine v11.0 Setup Wizard")
    print("─────────────────────────────────────")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("❌ TELEGRAM_BOT_TOKEN not set in .env")
        print("Edit .env and add your full bot token.")
        return

    print("\n1. Getting your Telegram Chat ID:")
    print(f"   Open: https://api.telegram.org/bot{token}/getUpdates")
    print("   Send any message to your bot first, then open that URL.")
    print("   Copy the 'id' number from the 'chat' object.")
    print("   Set TELEGRAM_CHAT_ID=<that number> in .env\n")

    try:
        from core.database_manager import db
        print(f"2. Database: ✅ OK ({db.get_db_size_mb()}MB at {db.db_path})")
    except Exception as e:
        print(f"2. Database: ❌ {e}")

    try:
        from core.streak_state import streak
        status = streak.get_status()
        print(f"3. Streak state: ✅ OK — {status['streak_state']}")
    except Exception as e:
        print(f"3. Streak state: ❌ {e}")

    try:
        from core.position_tracker import positions
        print(f"4. Positions: ✅ OK — {positions.count()} open")
    except Exception as e:
        print(f"4. Positions: ❌ {e}")

    # Check broker connections
    from config.config import MARKETS
    print("\n5. Market status:")
    for market, cfg in MARKETS.items():
        status = "✅ ENABLED" if cfg.get("enabled") else "⬜ Disabled"
        print(f"   {market:<12}: {status} (broker: {cfg.get('broker', 'N/A')})")

    print("\n✅ Setup check complete.")
    print("Next: python run_bot.py  (starts in paper trading mode)")
    print("      python run_bot.py --status  (check state)")
    print("      python run_bot.py --backtest  (backtest a symbol)")


if __name__ == "__main__":
    main()
