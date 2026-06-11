"""
CRAVE v10.4 — Content Factory (Zone 4)
========================================
Automated content generation from trade data.

─────────────────────────────────────────────
FEATURE 1: TRADE RECAP VIDEO SCRIPT GENERATOR
─────────────────────────────────────────────
After every profitable A+ trade, Jarvis writes a complete 60-second
video script ready for YouTube Shorts / Instagram Reels.

The script includes:
  - Hook (first 3 seconds) — the outcome and R result
  - Setup explanation — which SMC confluence triggered entry
  - Entry/SL/TP walkthrough — the levels and why
  - How it played out — what price actually did
  - Key lesson — one takeaway for the viewer

Also generates:
  - Title options (5 YouTube-optimised titles)
  - Description with relevant hashtags
  - Thumbnail text suggestions
  - Pinned comment draft

─────────────────────────────────────────────
FEATURE 2: PUBLIC VERIFICATION DASHBOARD EXPORT
─────────────────────────────────────────────
Generates a static HTML proof-of-work page you can host on GitHub Pages.
Shows: win rate, profit factor, equity curve (normalised, no $ amounts),
trade log (no SL/TP details — just symbol, direction, result, date).

This builds credibility for social media without revealing strategy.

USAGE:
  from content.trade_recap import get_content_factory

  cf = get_content_factory()
  cf.generate_trade_recap(trade_id)        # async
  cf.export_public_dashboard()             # creates State/public_dashboard.html
"""

import os
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("crave.content")

RECAP_DIR    = Path(__file__).parent.parent.parent.parent / "State" / "recaps"
RECAP_DIR.mkdir(parents=True, exist_ok=True)

PUBLIC_DASH  = Path(__file__).parent.parent.parent.parent / "State" / "public_dashboard.html"


class ContentFactory:

    def __init__(self):
        # Only generate recaps for A+ and A grade trades with positive R
        self._min_grade  = {"A+", "A"}
        self._min_r      = 1.0   # Only recap trades that made at least 1R

    # ─────────────────────────────────────────────────────────────────────────
    # TRADE RECAP
    # ─────────────────────────────────────────────────────────────────────────

    def generate_trade_recap(self, trade_id: str, run_async: bool = True):
        """
        Generate a complete video script for a trade.
        Runs asynchronously so it doesn't block trading.
        """
        # Get trade data
        try:
            from core.database_manager import db
            trades = db.get_recent_trades(limit=200)
            trade  = next(
                (t for t in trades if t.get("trade_id") == trade_id), None
            )
            if not trade:
                logger.debug(f"[Content] Trade {trade_id} not found in DB")
                return
        except Exception as e:
            logger.debug(f"[Content] DB fetch failed: {e}")
            return

        # Quality filter — only recap good trades
        r     = float(trade.get("r_multiple", 0) or 0)
        grade = trade.get("grade", "C")

        if r < self._min_r or grade not in self._min_grade:
            logger.debug(
                f"[Content] Skipping recap: {trade_id} "
                f"(grade={grade}, R={r:.2f})"
            )
            return

        if run_async:
            t = threading.Thread(
                target=self._generate_recap_sync,
                args=(trade,),
                daemon=True,
                name="ContentFactory",
            )
            t.start()
        else:
            self._generate_recap_sync(trade)

    def _generate_recap_sync(self, trade: dict):
        """Build the full recap document."""
        try:
            # Try LLM-enhanced script first
            script = self._generate_llm_script(trade)
            if not script:
                # Fall back to template-based script
                script = self._generate_template_script(trade)

            trade_id = trade.get("trade_id", "UNKNOWN")
            filepath = RECAP_DIR / f"{trade_id}_recap.md"
            filepath.write_text(script)

            logger.info(f"[Content] Recap saved: {filepath}")

            # Notify via Telegram
            try:
                from interfaces.telegram_interface import tg
                r      = trade.get("r_multiple", 0)
                symbol = trade.get("symbol", "?").replace("=X", "").replace("-USD", "")
                tg.send(
                    f"🎬 <b>TRADE RECAP READY: {symbol} {r:+.2f}R</b>\n"
                    f"Video script generated.\n"
                    f"📁 State/recaps/{trade_id}_recap.md"
                )
            except Exception:
                pass

        except Exception as e:
            logger.error(f"[Content] Recap generation failed: {e}")

    def _generate_llm_script(self, trade: dict) -> Optional[str]:
        """Use Jarvis LLM to generate script if available."""
        try:
            from intelligence.jarvis_llm import get_jarvis
            jarvis = get_jarvis()
            if not jarvis.is_ready():
                return None

            symbol    = trade.get("symbol", "?").replace("=X","").replace("-USD","")
            direction = trade.get("direction", "?").upper()
            entry     = trade.get("entry_price", 0)
            exit_p    = trade.get("exit_price", 0)
            sl        = trade.get("stop_loss", 0)
            r         = float(trade.get("r_multiple", 0))
            outcome   = trade.get("outcome", "?").replace("_"," ")
            grade     = trade.get("grade", "?")
            hold_h    = float(trade.get("hold_duration_h", 0))
            open_t    = trade.get("open_time", "?")

            prompt = f"""You are a professional trading content creator writing a 60-second 
video script about an algorithmic trading win for YouTube Shorts / Instagram Reels.

TRADE DATA:
  Symbol:    {symbol}
  Direction: {direction}
  Grade:     {grade}
  Entry:     {entry}
  Stop Loss: {sl}
  Exit:      {exit_p}
  Result:    {r:+.2f}R ({outcome})
  Hold Time: {hold_h:.1f} hours
  Opened:    {open_t}

Write a complete video script with these EXACT sections (use these headers):

## HOOK (0-3 seconds)
[2 punchy sentences. Lead with the result. Create curiosity.]

## SETUP EXPLANATION (3-20 seconds)
[Explain the SMC setup in plain English. What made this a high-probability trade?]

## ENTRY WALKTHROUGH (20-35 seconds)  
[Walk through the levels. Where was the OB/FVG? Where was the SL? Why those levels?]

## HOW IT PLAYED OUT (35-50 seconds)
[What did price actually do? Was it clean? Any sweeps before TP?]

## KEY LESSON (50-60 seconds)
[One actionable lesson. Call to action for comments/follow.]

## TITLES (5 YouTube-optimised options)
[5 titles with strong hooks]

## HASHTAGS
[15 relevant hashtags for trading content]

## DESCRIPTION
[YouTube description, 3 paragraphs]

## THUMBNAIL TEXT
[Bold 5-word text for thumbnail]

Keep language accessible — this is for beginner-to-intermediate traders.
Never mention exact account size. Say "made 2R" not "$200"."""

            response = jarvis._client.generate_content(prompt)
            return response.text.strip()

        except Exception as e:
            logger.debug(f"[Content] LLM script failed: {e}")
            return None

    def _generate_template_script(self, trade: dict) -> str:
        """Template-based script when LLM is not available."""
        symbol    = trade.get("symbol", "?").replace("=X","").replace("-USD","")
        direction = trade.get("direction", "buy").upper()
        entry     = trade.get("entry_price", 0)
        exit_p    = trade.get("exit_price", 0)
        sl        = trade.get("stop_loss", 0)
        r         = float(trade.get("r_multiple", 0))
        grade     = trade.get("grade", "?")
        hold_h    = float(trade.get("hold_duration_h", 0))
        outcome   = trade.get("outcome", "?").replace("_"," ")

        price_move = abs(float(exit_p) - float(entry))
        direction_word = "LONG" if direction in ("BUY","LONG") else "SHORT"

        return f"""# Trade Recap — {symbol} {r:+.2f}R
Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}

---

## HOOK (0-3 seconds)
My algo just printed a {r:+.2f}R trade on {symbol}.
Here's exactly what the setup looked like and why I took it.

## SETUP EXPLANATION (3-20 seconds)
This was a Grade {grade} Smart Money Concepts setup on the 1-Hour chart.
The algorithm identified a {direction_word} signal — price had swept liquidity
and confirmed a Change of Character at a key Order Block level.
Three confluences aligned: structure break, FVG, and volume confirmation.

## ENTRY WALKTHROUGH (20-35 seconds)
Entry: {entry:.4f}
Stop Loss: {sl:.4f} (below the Order Block)
Take Profit was 2× the SL distance — minimum 1:2 risk-to-reward.
Risk was fixed at 1% of capital. Non-negotiable.

## HOW IT PLAYED OUT (35-50 seconds)
The trade held for {hold_h:.1f} hours.
Price moved {price_move:.4f} from entry to exit at {exit_p:.4f}.
Outcome: {outcome}. Final result: {r:+.2f}R.

## KEY LESSON (50-60 seconds)
The key was waiting for ALL confluences — not just one signal.
Most traders enter too early. The algorithm waited for structure confirmation.
Follow for daily algorithmic SMC setups. Comment your questions below.

## TITLES
1. "I Let an Algorithm Trade For Me — Here's What Happened ({symbol} {r:+.0f}R)"
2. "This SMC Setup Made {r:+.1f}R in {hold_h:.0f} Hours (Algorithmic Trading)"
3. "{symbol} {direction_word} Signals — How My Bot Identifies Them"
4. "The {grade} Grade Setup That Printed {r:+.1f}R (Explained)"
5. "SMC Order Block Entry That Actually Worked ({symbol})"

## HASHTAGS
#SmartMoneyConcepts #AlgorithmicTrading #SMCTrading #ForexTrading 
#TradingBot #{symbol.replace("=X","").replace("-USD","")} 
#CryptoTrading #TechnicalAnalysis #OrderBlock #FairValueGap
#TradingJournal #PropFirm #DayTrading #SwingTrading #PassiveIncome

## DESCRIPTION
This is a breakdown of a real algorithmic trade using Smart Money Concepts (SMC).
My CRAVE algorithm identified a Grade {grade} setup on {symbol} and executed it with 
strict risk management — 1% risk, 2R minimum target.

The setup: price swept liquidity, confirmed a Change of Character (CHoCH), 
and retested a valid Order Block with volume confirmation. Three confluences = entry.

Result: {r:+.2f}R in {hold_h:.0f} hours. This is what systematic, rule-based 
trading looks like. No emotion. Just process.

## THUMBNAIL TEXT
{r:+.1f}R | {symbol} | {grade} GRADE
"""

    # ─────────────────────────────────────────────────────────────────────────
    # PUBLIC VERIFICATION DASHBOARD
    # ─────────────────────────────────────────────────────────────────────────

    def export_public_dashboard(self) -> str:
        """
        Generate a static HTML verification dashboard.
        Shows trading performance without revealing strategy details or $ amounts.
        Can be hosted on GitHub Pages (free) and linked in social media bio.

        Privacy: normalises equity to % returns (never shows $ amount),
        hides SL/TP levels, shows only direction/result/date per trade.
        """
        try:
            from core.database_manager import db
            from core.paper_trading import get_paper_engine

            trades = db.get_recent_trades(limit=200)
            stats  = get_paper_engine().get_stats()

            if not trades:
                return "No trades to export yet."

            total  = stats.get("total_trades", 0)
            wr     = stats.get("win_rate_float", 0)
            pf     = stats.get("profit_factor_float", 0)
            sharpe = stats.get("sharpe_float", 0)
            ret    = stats.get("total_return", "+0.00%")
            dd     = stats.get("max_drawdown", "0.00%")

            # Build normalised equity curve (% not $)
            start = get_paper_engine()._state.get("starting_equity", 10000)
            curve = get_paper_engine()._state.get("equity_curve", [])
            curve_pct = [
                round((e - start) / start * 100, 2) for e in curve
            ]

            # Build trade log (sanitised)
            trade_rows = ""
            for t in sorted(trades,
                             key=lambda x: x.get("close_time",""),
                             reverse=True)[:50]:
                r       = float(t.get("r_multiple", 0) or 0)
                sym     = (t.get("symbol","?")
                            .replace("=X","").replace("-USD",""))
                direction = t.get("direction","?")[:1].upper()
                date    = (t.get("close_time","?") or "")[:10]
                grade   = t.get("grade","?")
                outcome = t.get("outcome","?").replace("_"," ")
                color   = "#00c896" if r > 0 else "#ff3d57"
                trade_rows += f"""
                <tr>
                  <td>{date}</td>
                  <td>{sym}</td>
                  <td>{direction}</td>
                  <td>{grade}</td>
                  <td style="color:{color};font-weight:700">{r:+.2f}R</td>
                  <td style="color:#4a6070;font-size:11px">{outcome}</td>
                </tr>"""

            curve_js = json.dumps(curve_pct[-100:])  # Last 100 points

            html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CRAVE Trading — Live Verification</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#080c10;color:#c8d8e8;font-family:'Courier New',monospace}}
  .header{{background:#0d1117;border-bottom:1px solid #1c2836;
    padding:20px 24px;display:flex;justify-content:space-between;align-items:center}}
  .logo{{color:#00c896;font-size:14px;letter-spacing:4px}}
  .disclaimer{{color:#4a6070;font-size:10px;max-width:400px;text-align:right}}
  .grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;
    padding:20px 24px}}
  .card{{background:#0d1117;border:1px solid #1c2836;
    border-left:3px solid #00c896;padding:16px}}
  .label{{font-size:9px;letter-spacing:3px;color:#4a6070;
    text-transform:uppercase;margin-bottom:6px}}
  .value{{font-size:24px;font-weight:700}}
  .green{{color:#00c896}} .red{{color:#ff3d57}} .amber{{color:#f5a623}}
  .chart-wrap{{padding:0 24px 20px;height:200px}}
  table{{width:calc(100% - 48px);margin:0 24px 24px;
    border-collapse:collapse;font-size:12px}}
  th{{font-size:9px;letter-spacing:2px;color:#4a6070;
    padding:8px 12px;border-bottom:1px solid #1c2836;text-align:left}}
  td{{padding:10px 12px;border-bottom:1px solid rgba(28,40,54,0.4)}}
  .footer{{padding:16px 24px;border-top:1px solid #1c2836;
    font-size:9px;color:#4a6070;letter-spacing:2px;
    display:flex;justify-content:space-between}}
</style>
</head>
<body>
<div class="header">
  <div class="logo">CRAVE / TRADING VERIFICATION</div>
  <div class="disclaimer">
    All results are paper trading. Past performance does not guarantee future results.
    Returns shown as % of starting equity — no dollar amounts disclosed.
    Strategy and code are proprietary.
  </div>
</div>

<div class="grid">
  <div class="card">
    <div class="label">Total Return</div>
    <div class="value {'green' if '+' in ret else 'red'}">{ret}</div>
  </div>
  <div class="card">
    <div class="label">Win Rate</div>
    <div class="value {'green' if wr>=50 else 'amber'}">{wr:.1f}%</div>
  </div>
  <div class="card">
    <div class="label">Profit Factor</div>
    <div class="value {'green' if pf>=1.5 else 'amber'}">{pf:.2f}</div>
  </div>
  <div class="card">
    <div class="label">Sharpe Ratio</div>
    <div class="value {'green' if sharpe>=0.8 else 'amber'}">{sharpe:.2f}</div>
  </div>
  <div class="card">
    <div class="label">Total Trades</div>
    <div class="value">{total}</div>
  </div>
  <div class="card">
    <div class="label">Max Drawdown</div>
    <div class="value red">-{dd}</div>
  </div>
  <div class="card">
    <div class="label">Strategy</div>
    <div class="value" style="font-size:14px">SMC v10.4</div>
  </div>
  <div class="card">
    <div class="label">Mode</div>
    <div class="value" style="font-size:14px;color:#3d8eff">PAPER</div>
  </div>
</div>

<div class="chart-wrap">
  <canvas id="equityChart"></canvas>
</div>

<table>
  <thead>
    <tr><th>Date</th><th>Symbol</th><th>Dir</th>
        <th>Grade</th><th>Result</th><th>Outcome</th></tr>
  </thead>
  <tbody>{trade_rows}</tbody>
</table>

<div class="footer">
  <span>CRAVE v10.4 · SMC Algorithmic Trading · charanbhargav6</span>
  <span>Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</span>
</div>

<script>
const ctx = document.getElementById('equityChart').getContext('2d');
const data = {curve_js};
new Chart(ctx, {{
  type: 'line',
  data: {{
    labels: data.map((_,i) => i+1),
    datasets: [{{
      data: data,
      borderColor: '#00c896',
      borderWidth: 1.5,
      fill: true,
      backgroundColor: 'rgba(0,200,150,0.08)',
      pointRadius: 0,
      tension: 0.1
    }}]
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      x: {{ display: false }},
      y: {{
        ticks: {{ color: '#4a6070', font: {{ size: 10, family: 'Courier New' }},
                  callback: v => v + '%' }},
        grid: {{ color: 'rgba(28,40,54,0.5)' }},
        border: {{ display: false }}
      }}
    }}
  }}
}});
</script>
</body>
</html>"""

            PUBLIC_DASH.write_text(html)
            logger.info(f"[Content] Public dashboard exported: {PUBLIC_DASH}")
            return str(PUBLIC_DASH)

        except Exception as e:
            logger.error(f"[Content] Dashboard export failed: {e}")
            return f"Export failed: {e}"


# ── Singleton ─────────────────────────────────────────────────────────────────
_factory: Optional[ContentFactory] = None

def get_content_factory() -> ContentFactory:
    global _factory
    if _factory is None:
        _factory = ContentFactory()
    return _factory
