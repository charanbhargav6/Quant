import time
import threading
import logging
from datetime import datetime
from rich.live import Live
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.console import Console

console = Console()

class TerminalDashboard:
    def __init__(self):
        self.layout = self.make_layout()
        self.market_data = {}
        self.recent_signals = []
        self.council_debates = []
        self.account_status = {"equity": 0, "drawdown": 0, "open_positions": 0}
        self.render()
        self.live = Live(self.layout, refresh_per_second=4, screen=True)
        
    def make_layout(self) -> Layout:
        """Create the dashboard layout"""
        layout = Layout(name="root")
        layout.split(
            Layout(name="header", size=3),
            Layout(name="main", ratio=1),
            Layout(name="footer", size=10)
        )
        layout["main"].split_row(
            Layout(name="market_matrix", ratio=2),
            Layout(name="council_stream", ratio=3)
        )
        return layout
        
    def update_account(self, equity: float, open_positions: int, drawdown: float):
        self.account_status = {
            "equity": equity,
            "open_positions": open_positions,
            "drawdown": drawdown
        }
        
    def update_market(self, symbol: str, price: float, bias: str, regime: str, signal: str):
        self.market_data[symbol] = {
            "price": price,
            "bias": bias,
            "regime": regime,
            "signal": signal,
            "updated": datetime.now().strftime("%H:%M:%S")
        }
        
    def add_council_msg(self, symbol: str, msg: str):
        self.council_debates.append(f"[{datetime.now().strftime('%H:%M:%S')}] {symbol}: {msg}")
        if len(self.council_debates) > 15:
            self.council_debates.pop(0)

    def add_signal(self, symbol: str, direction: str, grade: str, status: str):
        self.recent_signals.append({
            "time": datetime.now().strftime('%H:%M:%S'),
            "symbol": symbol,
            "dir": direction.upper(),
            "grade": grade,
            "status": status
        })
        if len(self.recent_signals) > 5:
            self.recent_signals.pop(0)

    def generate_header(self) -> Panel:
        eq = self.account_status["equity"]
        pos = self.account_status["open_positions"]
        dd = self.account_status["drawdown"]
        
        status_text = Text.from_markup(
            f"[bold cyan]CRAVE Quant Terminal[/bold cyan] | "
            f"Equity: [bold green]${eq:.2f}[/bold green] | "
            f"Open Positions: [bold yellow]{pos}[/bold yellow] | "
            f"Drawdown: [bold red]{dd:.2f}%[/bold red]"
        )
        return Panel(status_text, style="blue")

    def generate_market_matrix(self) -> Panel:
        table = Table(expand=True, show_edge=False)
        table.add_column("Symbol", style="cyan")
        table.add_column("Price", style="white")
        table.add_column("Bias", justify="center")
        table.add_column("Regime", justify="center")
        table.add_column("Last Signal")
        table.add_column("Updated", style="dim")

        for sym, data in self.market_data.items():
            b_style = "green" if data["bias"] == "BUY" else "red" if data["bias"] == "SELL" else "dim"
            s_style = "bold green" if data["signal"] == "buy" else "bold red" if data["signal"] == "sell" else "white"
            
            table.add_row(
                sym,
                f"{data['price']:.4f}",
                f"[{b_style}]{data['bias']}[/{b_style}]",
                data['regime'],
                f"[{s_style}]{data['signal']}[/{s_style}]",
                data['updated']
            )
            
        return Panel(table, title="[bold]Live Market Matrix[/bold]", border_style="blue")

    def generate_council_stream(self) -> Panel:
        content = "\n".join(self.council_debates)
        if not content:
            content = "Waiting for LLM Council Debate to begin..."
            
        return Panel(content, title="[bold]🧠 LLM Council Stream[/bold]", border_style="magenta")

    def generate_footer(self) -> Panel:
        table = Table(expand=True, show_edge=False)
        table.add_column("Time", style="dim")
        table.add_column("Symbol", style="cyan")
        table.add_column("Direction")
        table.add_column("Grade")
        table.add_column("Status")
        
        for sig in self.recent_signals:
            d_style = "green" if sig['dir'] == "BUY" else "red"
            st_style = "bold green" if "EXECUTED" in sig['status'] else "dim"
            
            table.add_row(
                sig['time'],
                sig['symbol'],
                f"[{d_style}]{sig['dir']}[/{d_style}]",
                sig['grade'],
                f"[{st_style}]{sig['status']}[/{st_style}]"
            )
            
        return Panel(table, title="[bold]Recent Signals & Executions[/bold]", border_style="green")

    def render(self):
        self.layout["header"].update(self.generate_header())
        self.layout["main"]["market_matrix"].update(self.generate_market_matrix())
        self.layout["main"]["council_stream"].update(self.generate_council_stream())
        self.layout["footer"].update(self.generate_footer())
        
    def start(self):
        self.live.start()
        
    def stop(self):
        self.live.stop()

# Global singleton
dashboard = TerminalDashboard()
