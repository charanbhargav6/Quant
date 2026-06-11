"""
CRAVE — Full Portfolio Backtest + HTML Dashboard Generator
Run:  python Sub_Projects/Trading/run_full_backtest.py
Generates: Sub_Projects/Trading/backtest_dashboard.html
"""
import sys, os, json, logging, io
from pathlib import Path

# Fix Windows cp1252 encoding crash
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

_root = str(Path(__file__).resolve().parents[2])
sys.path.insert(0, _root)
sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("crave.fullbacktest")

import numpy as np
import pandas as pd

STARTING_EQUITY = 10_000.0
RISK_PER_TRADE  = 0.02

INSTRUMENTS = [
    # ── Crypto (optimized: B+/A grade, tight SL) ──
    ("BTCUSD",  30),
    ("ETHUSD",  30),
    ("SOLUSD",  30),
    # ── Commodities (Gold: dedicated pullback strategy, Silver: SMC) ──
    ("XAUUSD",  90),
    ("XAGUSD",  60),
    # ── Forex (optimized: B+ grade, tight 1.0x ATR SL) ──
    ("EURUSD",  90),

]


def run_all_backtests():
    from backtest_agent import BacktestAgent
    bt = BacktestAgent()
    results = {}
    for sym, days in INSTRUMENTS:
        logger.info(f"━━━ Backtesting {sym} ({days}d) ━━━")
        try:
            report = bt.run_backtest(sym, days=days, min_confidence=40, risk_per_trade=RISK_PER_TRADE)
            if "error" not in report:
                mc = bt.monte_carlo(report)
                report["_monte_carlo"] = mc
                results[sym] = report
                logger.info(f"  ✅ {sym}: {report['Signals']} trades | WR {report['Win_Rate']} | Return {report['Total_Return']}")
            else:
                logger.warning(f"  ❌ {sym}: {report['error']}")
                results[sym] = report
        except Exception as e:
            logger.error(f"  ❌ {sym} failed: {e}")
            results[sym] = {"error": str(e), "Symbol": sym}
    return results


def build_portfolio_equity(results: dict) -> tuple:
    """Build a combined portfolio equity curve from all instrument backtests."""
    all_trades = []
    for sym, r in results.items():
        if "error" in r:
            continue
        for t in r.get("_trades", []):
            all_trades.append({**t, "symbol": r.get("Symbol", sym)})

    if not all_trades:
        return [], [], {}

    # Simulate portfolio equity
    equity = STARTING_EQUITY
    curve = [{"trade": 0, "equity": equity, "symbol": "-", "r": 0}]
    for i, t in enumerate(all_trades, 1):
        r = t["r_multiple"]
        pnl = equity * RISK_PER_TRADE * r
        equity += pnl
        curve.append({"trade": i, "equity": round(equity, 2), "symbol": t["symbol"],
                       "r": r, "direction": t.get("direction", "?"),
                       "grade": t.get("grade", "?"), "outcome": t.get("outcome", "?"),
                       "pnl": round(pnl, 2)})

    # Stats
    r_arr = np.array([t["r_multiple"] for t in all_trades])
    wins = int((r_arr > 0).sum())
    losses = int((r_arr <= 0).sum())
    total = len(all_trades)
    wr = wins / total * 100 if total else 0
    exp = float(r_arr.mean()) if total else 0
    total_ret = (equity - STARTING_EQUITY) / STARTING_EQUITY * 100
    peak = STARTING_EQUITY
    max_dd = 0
    for c in curve:
        if c["equity"] > peak:
            peak = c["equity"]
        dd = (peak - c["equity"]) / peak * 100
        if dd > max_dd:
            max_dd = dd
    gross_p = float(r_arr[r_arr > 0].sum()) if (r_arr > 0).any() else 0
    gross_l = float(abs(r_arr[r_arr < 0].sum())) if (r_arr < 0).any() else 0.001
    pf = gross_p / gross_l if gross_l > 0 else 999

    stats = {
        "starting_equity": STARTING_EQUITY, "final_equity": round(equity, 2),
        "total_return_pct": round(total_ret, 2), "total_trades": total,
        "wins": wins, "losses": losses, "win_rate": round(wr, 1),
        "expectancy_r": round(exp, 3), "max_drawdown_pct": round(max_dd, 2),
        "profit_factor": round(pf, 2), "best_r": round(float(r_arr.max()), 2) if total else 0,
        "worst_r": round(float(r_arr.min()), 2) if total else 0,
    }
    return curve, all_trades, stats


def generate_html(results: dict, curve: list, all_trades: list, stats: dict) -> str:
    """Generate a premium dark-mode HTML dashboard."""

    # Per-instrument summary rows
    inst_rows = ""
    for sym, r in results.items():
        if "error" in r:
            inst_rows += f'<tr><td>{sym}</td><td colspan="7" style="color:#f87171">Error: {r["error"][:80]}</td></tr>'
            continue
        wr_val = float(r["Win_Rate"].replace("%",""))
        wr_color = "#4ade80" if wr_val >= 55 else "#fbbf24" if wr_val >= 50 else "#f87171"
        ret_val = float(r["Total_Return"].replace("%",""))
        ret_color = "#4ade80" if ret_val > 0 else "#f87171"
        inst_rows += f"""<tr>
            <td><strong>{r.get('Symbol', sym)}</strong></td>
            <td>{r.get('Asset_Class','?')}</td>
            <td>{r['Signals']}</td>
            <td>{r['Wins']}/{r['Losses']}</td>
            <td style="color:{wr_color};font-weight:700">{r['Win_Rate']}</td>
            <td>{r['Expectancy_R']}</td>
            <td style="color:{ret_color};font-weight:700">{r['Total_Return']}</td>
            <td>{r['Profit_Factor']}</td>
        </tr>"""

    # Trade log rows (last 200)
    trade_rows = ""
    for i, c in enumerate(curve[1:][:200], 1):
        pnl_color = "#4ade80" if c.get("pnl", 0) >= 0 else "#f87171"
        r_color = "#4ade80" if c["r"] > 0 else "#f87171"
        dir_icon = "🟢" if c.get("direction") == "buy" else "🔴"
        trade_rows += f"""<tr>
            <td>{i}</td><td>{c['symbol']}</td><td>{dir_icon} {c.get('direction','?')}</td>
            <td>{c.get('grade','?')}</td><td>{c.get('outcome','?')}</td>
            <td style="color:{r_color};font-weight:700">{c['r']:+.2f}R</td>
            <td style="color:{pnl_color};font-weight:700">${c.get('pnl',0):+,.2f}</td>
            <td>${c['equity']:,.2f}</td>
        </tr>"""

    # Equity curve data for chart
    eq_labels = json.dumps([c["trade"] for c in curve])
    eq_data   = json.dumps([c["equity"] for c in curve])

    # Per-instrument return data for bar chart
    bar_labels = json.dumps([r.get("Symbol", s) for s, r in results.items() if "error" not in r])
    bar_data   = json.dumps([float(r["Total_Return"].replace("%","")) for s, r in results.items() if "error" not in r])

    # Win/Loss distribution
    win_loss_data = json.dumps([stats["wins"], stats["losses"]])

    final_eq = stats["final_equity"]
    ret_pct  = stats["total_return_pct"]
    ret_color = "#4ade80" if ret_pct > 0 else "#f87171"
    wr_color  = "#4ade80" if stats["win_rate"] >= 55 else "#fbbf24" if stats["win_rate"] >= 50 else "#f87171"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>CRAVE Backtest Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;900&display=swap" rel="stylesheet">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'Inter',sans-serif;background:#0a0a0f;color:#e2e8f0;min-height:100vh}}
.header{{background:linear-gradient(135deg,#1a1a2e 0%,#16213e 50%,#0f3460 100%);
  padding:40px;text-align:center;border-bottom:2px solid #4ade8040}}
.header h1{{font-size:2.5rem;font-weight:900;
  background:linear-gradient(135deg,#4ade80,#22d3ee,#a78bfa);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent}}
.header p{{color:#94a3b8;margin-top:8px;font-size:1.1rem}}
.stats-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));
  gap:20px;padding:30px 40px;max-width:1400px;margin:0 auto}}
.stat-card{{background:linear-gradient(135deg,#1e1e30,#1a1a2e);border-radius:16px;
  padding:24px;border:1px solid #ffffff10;text-align:center;
  transition:transform 0.2s,box-shadow 0.2s}}
.stat-card:hover{{transform:translateY(-4px);box-shadow:0 8px 30px #4ade8015}}
.stat-card .label{{color:#94a3b8;font-size:0.75rem;text-transform:uppercase;letter-spacing:1px}}
.stat-card .value{{font-size:2rem;font-weight:900;margin-top:6px}}
.section{{max-width:1400px;margin:30px auto;padding:0 40px}}
.section h2{{font-size:1.5rem;font-weight:700;margin-bottom:20px;
  padding-bottom:10px;border-bottom:2px solid #ffffff10}}
.chart-container{{background:#1e1e30;border-radius:16px;padding:24px;
  border:1px solid #ffffff10;margin-bottom:30px}}
.charts-row{{display:grid;grid-template-columns:2fr 1fr;gap:20px}}
table{{width:100%;border-collapse:collapse;background:#1e1e30;border-radius:16px;overflow:hidden}}
th{{background:#16213e;padding:14px 16px;text-align:left;font-size:0.75rem;
  text-transform:uppercase;letter-spacing:1px;color:#94a3b8;position:sticky;top:0}}
td{{padding:12px 16px;border-bottom:1px solid #ffffff08;font-size:0.9rem}}
tr:hover td{{background:#ffffff05}}
.scroll-table{{max-height:500px;overflow-y:auto;border-radius:16px;border:1px solid #ffffff10}}
.verdict{{font-size:1.1rem;padding:20px;border-radius:12px;margin-top:20px;
  border:1px solid #ffffff10;text-align:center}}
.verdict.good{{background:#4ade8015;border-color:#4ade8040}}
.verdict.warn{{background:#fbbf2415;border-color:#fbbf2440}}
.verdict.bad{{background:#f8717115;border-color:#f8717140}}
@media(max-width:900px){{.charts-row{{grid-template-columns:1fr}}.stats-grid{{padding:20px}}}}
@media print {{
  body {{ background: #0a0a0f !important; -webkit-print-color-adjust: exact; }}
  .scroll-table {{ max-height: none !important; overflow: visible !important; }}
  .chart-container, .stat-card, table, th, td {{ page-break-inside: avoid; }}
}}
</style>
</head>
<body>
<div class="header">
  <h1>CRAVE BACKTEST DASHBOARD</h1>
  <p>SMC v9.3 Strategy &bull; $10,000 Starting Equity &bull; 2% Risk Per Trade &bull; Crypto \u2022 Gold \u2022 Forex \u2022 US Stocks \u2022 Indian F&amp;O</p>
</div>

<div class="stats-grid">
  <div class="stat-card"><div class="label">Starting Equity</div><div class="value" style="color:#94a3b8">${STARTING_EQUITY:,.0f}</div></div>
  <div class="stat-card"><div class="label">Final Equity</div><div class="value" style="color:{ret_color}">${final_eq:,.2f}</div></div>
  <div class="stat-card"><div class="label">Total Return</div><div class="value" style="color:{ret_color}">{ret_pct:+.2f}%</div></div>
  <div class="stat-card"><div class="label">Total Trades</div><div class="value" style="color:#22d3ee">{stats['total_trades']}</div></div>
  <div class="stat-card"><div class="label">Win Rate</div><div class="value" style="color:{wr_color}">{stats['win_rate']}%</div></div>
  <div class="stat-card"><div class="label">Expectancy</div><div class="value" style="color:#a78bfa">{stats['expectancy_r']:+.3f}R</div></div>
  <div class="stat-card"><div class="label">Max Drawdown</div><div class="value" style="color:#f87171">-{stats['max_drawdown_pct']}%</div></div>
  <div class="stat-card"><div class="label">Profit Factor</div><div class="value" style="color:#fbbf24">{stats['profit_factor']}</div></div>
</div>

<div class="section">
  <h2>📈 Portfolio Equity Curve</h2>
  <div class="charts-row">
    <div class="chart-container"><canvas id="equityChart"></canvas></div>
    <div class="chart-container"><canvas id="winLossChart"></canvas></div>
  </div>
</div>

<div class="section">
  <h2>🎯 Instrument Performance</h2>
  <div class="charts-row">
    <div class="chart-container"><canvas id="barChart"></canvas></div>
    <div style="display:flex;flex-direction:column;gap:12px">
      <div class="stat-card"><div class="label">Best R</div><div class="value" style="color:#4ade80">{stats['best_r']:+.1f}R</div></div>
      <div class="stat-card"><div class="label">Worst R</div><div class="value" style="color:#f87171">{stats['worst_r']:.1f}R</div></div>
      <div class="stat-card"><div class="label">W / L</div><div class="value" style="color:#22d3ee">{stats['wins']} / {stats['losses']}</div></div>
    </div>
  </div>
  <div style="overflow-x:auto;border-radius:16px;border:1px solid #ffffff10;margin-top:20px">
    <table><thead><tr>
      <th>Symbol</th><th>Asset</th><th>Trades</th><th>W/L</th><th>Win Rate</th><th>Expectancy</th><th>Return</th><th>PF</th>
    </tr></thead><tbody>{inst_rows}</tbody></table>
  </div>
</div>

<div class="section">
  <h2>📋 Trade Journal ({stats['total_trades']} Trades)</h2>
  <div class="scroll-table">
    <table><thead><tr>
      <th>#</th><th>Symbol</th><th>Direction</th><th>Grade</th><th>Outcome</th><th>R-Multiple</th><th>P&L</th><th>Equity</th>
    </tr></thead><tbody>{trade_rows}</tbody></table>
  </div>
</div>

<script>
const eq_ctx=document.getElementById('equityChart').getContext('2d');
new Chart(eq_ctx,{{type:'line',data:{{labels:{eq_labels},datasets:[{{
  label:'Portfolio Equity ($)',data:{eq_data},
  borderColor:'#4ade80',backgroundColor:'#4ade8015',fill:true,tension:0.3,
  pointRadius:0,borderWidth:2}}]}},
  options:{{responsive:true,plugins:{{legend:{{labels:{{color:'#94a3b8'}}}}}},
  scales:{{x:{{display:false}},y:{{ticks:{{color:'#94a3b8',callback:v=>'$'+v.toLocaleString()}},grid:{{color:'#ffffff08'}}}}}}}}}});

const wl_ctx=document.getElementById('winLossChart').getContext('2d');
new Chart(wl_ctx,{{type:'doughnut',data:{{labels:['Wins','Losses'],datasets:[{{
  data:{win_loss_data},backgroundColor:['#4ade80','#f87171'],borderWidth:0}}]}},
  options:{{responsive:true,plugins:{{legend:{{labels:{{color:'#94a3b8'}}}}}}}}}});

const bar_ctx=document.getElementById('barChart').getContext('2d');
const barData={bar_data};
new Chart(bar_ctx,{{type:'bar',data:{{labels:{bar_labels},datasets:[{{
  label:'Return %',data:barData,
  backgroundColor:barData.map(v=>v>=0?'#4ade8090':'#f8717190'),
  borderColor:barData.map(v=>v>=0?'#4ade80':'#f87171'),borderWidth:1,borderRadius:8}}]}},
  options:{{responsive:true,plugins:{{legend:{{display:false}}}},
  scales:{{x:{{ticks:{{color:'#94a3b8'}},grid:{{display:false}}}},
  y:{{ticks:{{color:'#94a3b8',callback:v=>v+'%'}},grid:{{color:'#ffffff08'}}}}}}}}}});
</script>
</body></html>"""
    return html


def main():
    print("""
╔══════════════════════════════════════════════════════╗
║        CRAVE FULL PORTFOLIO BACKTEST                 ║
║   $10,000 | 9 Instruments | SMC v9.3                ║
╚══════════════════════════════════════════════════════╝
    """)

    results = run_all_backtests()
    curve, all_trades, stats = build_portfolio_equity(results)

    if not all_trades:
        print("❌ No trades generated across any instrument.")
        return

    print(f"\n{'='*50}")
    print(f"  PORTFOLIO SUMMARY")
    print(f"{'='*50}")
    print(f"  Starting Equity : ${stats['starting_equity']:,.0f}")
    print(f"  Final Equity    : ${stats['final_equity']:,.2f}")
    print(f"  Total Return    : {stats['total_return_pct']:+.2f}%")
    print(f"  Total Trades    : {stats['total_trades']}")
    print(f"  Win Rate        : {stats['win_rate']}%")
    print(f"  Expectancy      : {stats['expectancy_r']:+.3f}R")
    print(f"  Max Drawdown    : -{stats['max_drawdown_pct']}%")
    print(f"  Profit Factor   : {stats['profit_factor']}")
    print(f"{'='*50}\n")

    html = generate_html(results, curve, all_trades, stats)
    out_path = Path(__file__).parent / "backtest_dashboard.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"✅ Dashboard saved: {out_path}")
    print(f"   Open in browser to view charts and trade journal.")

    # Also save raw JSON for programmatic use
    json_path = Path(__file__).parent / "backtest_results.json"
    safe = {}
    for sym, r in results.items():
        safe[sym] = {k: v for k, v in r.items() if not k.startswith("_")}
    safe["_portfolio"] = stats
    json_path.write_text(json.dumps(safe, indent=2, default=str), encoding="utf-8")
    print(f"✅ Raw data saved: {json_path}")


if __name__ == "__main__":
    main()
