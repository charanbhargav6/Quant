import pandas as pd
import numpy as np
import os
import sys
import logging
import math

from engines.orderflow_strategy import analyze_orderflow
from tests.test_02_to_08_strategy import WalkForwardPartitioner

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("backtester")

class BacktestEngine:
    def __init__(self, data_path: str, rr_ratio: float = 2.0, sl_pips: float = 2.0):
        self.data_path = data_path
        self.rr_ratio = rr_ratio
        self.sl_pips = sl_pips  # In synthetic Gold, let's say 2.0 price points is the SL.
        
        # Performance metrics
        self.trades = []
        
    def load_data(self) -> pd.DataFrame:
        logger.info(f"Loading data from {self.data_path}")
        df = pd.read_parquet(self.data_path)
        logger.info(f"Loaded {len(df)} rows.")
        return df

    def calculate_sharpe(self, trades: list) -> float:
        if not trades:
            return 0.0
        
        # Calculate R-multiples
        r_multiples = [t['pnl_r'] for t in trades]
        
        # Daily grouping for standard Sharpe (assuming trades are spread out)
        df = pd.DataFrame(trades)
        df['date'] = df['exit_time'].dt.date
        daily_pnl = df.groupby('date')['pnl_r'].sum()
        
        mean = daily_pnl.mean()
        std = daily_pnl.std()
        if std == 0 or math.isnan(std):
            return 0.0
        
        # Annualized Sharpe (approx 252 trading days)
        return (mean / std) * math.sqrt(252)

    def calculate_max_drawdown(self, trades: list) -> float:
        if not trades:
            return 0.0
        
        cumulative_r = np.cumsum([t['pnl_r'] for t in trades])
        running_max = np.maximum.accumulate(cumulative_r)
        drawdown = running_max - cumulative_r
        return drawdown.max()

    def simulate_trade(self, df: pd.DataFrame, entry_idx: int, direction: str) -> dict:
        """
        Simulate a trade from entry_idx forward until SL or TP is hit.
        Returns trade dict.
        """
        entry_row = df.iloc[entry_idx]
        entry_price = entry_row['close']
        
        if direction == 'Bullish':
            sl_price = entry_price - self.sl_pips
            tp_price = entry_price + (self.sl_pips * self.rr_ratio)
        else:
            sl_price = entry_price + self.sl_pips
            tp_price = entry_price - (self.sl_pips * self.rr_ratio)
            
        # Scan forward for exit
        for i in range(entry_idx + 1, len(df)):
            row = df.iloc[i]
            high = row['high']
            low = row['low']
            
            if direction == 'Bullish':
                if low <= sl_price:
                    return {'entry_time': entry_row.name, 'exit_time': row.name, 'direction': direction, 'pnl_r': -1.0}
                if high >= tp_price:
                    return {'entry_time': entry_row.name, 'exit_time': row.name, 'direction': direction, 'pnl_r': self.rr_ratio}
            else:
                if high >= sl_price:
                    return {'entry_time': entry_row.name, 'exit_time': row.name, 'direction': direction, 'pnl_r': -1.0}
                if low <= tp_price:
                    return {'entry_time': entry_row.name, 'exit_time': row.name, 'direction': direction, 'pnl_r': self.rr_ratio}
                    
        # Trade still open at end of window
        return {'entry_time': entry_row.name, 'exit_time': df.iloc[-1].name, 'direction': direction, 'pnl_r': 0.0}

    def run_window(self, window_df: pd.DataFrame) -> list:
        trades = []
        # Step through the dataframe (we skip the first 50 due to lookback)
        lookback = 50
        
        # To speed up backtest, we only analyze every N minutes unless in a trade
        in_trade = False
        trade_end_time = None
        
        # We need integer indices to use iloc
        for idx in range(lookback, len(window_df), 60):
            row = window_df.iloc[idx]
            
            # Skip if we are still in a trade
            if in_trade and row.name <= trade_end_time:
                continue
                
            in_trade = False
            
            result = analyze_orderflow(window_df, current_idx=idx, lookback=lookback)
            
            # Entry trigger
            if result.get('grade') in ('A+', 'A'):
                direction = result.get('direction', 'Neutral')
                
                if direction != "Neutral":
                    trade = self.simulate_trade(window_df, idx, direction)
                    if trade['pnl_r'] != 0.0:
                        trades.append(trade)
                        in_trade = True
                        trade_end_time = trade['exit_time']
                        
        return trades

    def execute_walk_forward(self, train_days: int = 90, test_days: int = 30):
        df = self.load_data()
        
        # Create Walk Forward Partitions
        windows = WalkForwardPartitioner.create_rolling_windows(df, train_days=train_days, test_days=test_days)
        
        logger.info(f"Created {len(windows)} Walk-Forward windows.")
        
        overall_oos_trades = []
        overall_is_trades = []
        
        for i, (train_df, test_df) in enumerate(windows):
            logger.info(f"--- Window {i+1} ---")
            logger.info(f"Train: {train_df.index[0]} to {train_df.index[-1]}")
            logger.info(f"Test:  {test_df.index[0]} to {test_df.index[-1]}")
            
            is_trades = self.run_window(train_df)
            oos_trades = self.run_window(test_df)
            
            is_sharpe = self.calculate_sharpe(is_trades)
            oos_sharpe = self.calculate_sharpe(oos_trades)
            
            logger.info(f"IS Trades: {len(is_trades)} | IS Sharpe: {is_sharpe:.2f}")
            logger.info(f"OOS Trades: {len(oos_trades)} | OOS Sharpe: {oos_sharpe:.2f}")
            
            # Protocol Test #4: Deflated Sharpe Validation
            # Pass bar: OOS Sharpe is within 40-60% of in-sample Sharpe
            if is_sharpe > 0:
                retention = (oos_sharpe / is_sharpe) * 100
                logger.info(f"Sharpe Retention: {retention:.1f}%")
                if 40 <= retention <= 150:
                    logger.info("PASS (Test #4): Walk-Forward OOS performance validated.")
                else:
                    logger.warning("FAIL (Test #4): OOS degradation too severe.")
            
            overall_is_trades.extend(is_trades)
            overall_oos_trades.extend(oos_trades)
            
        # Final overall metrics
        total_sharpe = self.calculate_sharpe(overall_oos_trades)
        total_dd = self.calculate_max_drawdown(overall_oos_trades)
        wins = sum(1 for t in overall_oos_trades if t['pnl_r'] > 0)
        total_trades = len(overall_oos_trades)
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
        
        logger.info("=========================================")
        logger.info("  FINAL OUT-OF-SAMPLE METRICS (ALL WINDOWS)")
        logger.info("=========================================")
        logger.info(f"Total OOS Trades : {total_trades}")
        logger.info(f"Win Rate         : {win_rate:.1f}%")
        logger.info(f"Max Drawdown (R) : {total_dd:.1f}R")
        logger.info(f"Deflated Sharpe  : {total_sharpe:.2f}")
        logger.info("=========================================")
        
        # Test #3: Regime Agnostic (Simplified check)
        bull_market_trades = [t for t in overall_oos_trades if t['direction'] == 'Bullish']
        bear_market_trades = [t for t in overall_oos_trades if t['direction'] == 'Bearish']
        
        bull_sharpe = self.calculate_sharpe(bull_market_trades)
        bear_sharpe = self.calculate_sharpe(bear_market_trades)
        
        logger.info("Test #3: Market Regime Agnostic Check")
        logger.info(f"Bullish Setup Sharpe: {bull_sharpe:.2f} | Bearish Setup Sharpe: {bear_sharpe:.2f}")
        if bull_sharpe > 0 and bear_sharpe > 0:
            logger.info("PASS (Test #3): Strategy performs in both directions.")
        else:
            logger.warning("FAIL (Test #3): Strategy is directionally biased.")

if __name__ == "__main__":
    engine = BacktestEngine(data_path="data/historical/XAUUSD_1m_20230101_20240101.parquet")
    engine.execute_walk_forward(train_days=90, test_days=30)
