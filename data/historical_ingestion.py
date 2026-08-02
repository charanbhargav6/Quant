import os
import time
import logging
from datetime import datetime, timezone
import pandas as pd

logger = logging.getLogger("crave.data.historical")
logging.basicConfig(level=logging.INFO)

class HistoricalIngestionEngine:
    def __init__(self, data_dir: str = "data/historical"):
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)
        
    def _init_mt5(self):
        import MetaTrader5 as mt5
        if not mt5.initialize():
            logger.error("MT5 initialize failed.")
            return False
        return True

    def download_mt5_ticks(self, symbol: str, start_date: datetime, end_date: datetime) -> str:
        """
        Download historical tick data (Bid, Ask, Last) from MT5 for a date range.
        Saves the result to a Parquet file.
        """
        import MetaTrader5 as mt5
        if not self._init_mt5():
            raise RuntimeError("Could not connect to MT5")

        logger.info(f"Downloading ticks for {symbol} from {start_date.date()} to {end_date.date()}...")
        ticks = mt5.copy_ticks_range(symbol, start_date, end_date, mt5.COPY_TICKS_ALL)
        
        if ticks is None or len(ticks) == 0:
            logger.warning(f"No ticks found for {symbol} in the given range.")
            return ""

        df = pd.DataFrame(ticks)
        # Convert timestamp to UTC datetime
        df['time'] = pd.to_datetime(df['time'], unit='s', utc=True)
        df.set_index('time', inplace=True)
        
        # Save to parquet
        filename = f"{symbol}_ticks_{start_date.strftime('%Y%m%d')}_{end_date.strftime('%Y%m%d')}.parquet"
        filepath = os.path.join(self.data_dir, filename)
        
        # Ensure we have pyarrow or fastparquet
        try:
            df.to_parquet(filepath, engine="pyarrow")
            logger.info(f"Saved {len(df)} ticks to {filepath}")
        except ImportError:
            logger.error("Missing pyarrow. Please install pyarrow to save parquet files.")
            raise
            
        return filepath

    def download_mt5_ohlcv(self, symbol: str, timeframe_str: str, start_date: datetime, end_date: datetime) -> str:
        """
        Download historical OHLCV data from MT5 for a date range.
        Saves the result to a Parquet file.
        """
        import MetaTrader5 as mt5
        if not self._init_mt5():
            raise RuntimeError("Could not connect to MT5")
            
        tf_map = {
            "1m": mt5.TIMEFRAME_M1,
            "5m": mt5.TIMEFRAME_M5,
            "15m": mt5.TIMEFRAME_M15,
            "1h": mt5.TIMEFRAME_H1,
            "1d": mt5.TIMEFRAME_D1,
        }
        
        mt5_tf = tf_map.get(timeframe_str, mt5.TIMEFRAME_M1)

        logger.info(f"Downloading {timeframe_str} OHLCV for {symbol} from {start_date.date()} to {end_date.date()}...")
        rates = mt5.copy_rates_range(symbol, mt5_tf, start_date, end_date)
        
        if rates is None or len(rates) == 0:
            logger.warning(f"No OHLCV data found for {symbol} in the given range.")
            return ""

        df = pd.DataFrame(rates)
        df['time'] = pd.to_datetime(df['time'], unit='s', utc=True)
        df.set_index('time', inplace=True)
        
        filename = f"{symbol}_{timeframe_str}_{start_date.strftime('%Y%m%d')}_{end_date.strftime('%Y%m%d')}.parquet"
        filepath = os.path.join(self.data_dir, filename)
        
        df.to_parquet(filepath, engine="pyarrow")
        logger.info(f"Saved {len(df)} OHLCV bars to {filepath}")
            
        return filepath

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Download Historical Tick/OHLCV Data to Parquet")
    parser.add_argument("--symbol", type=str, required=True, help="Trading symbol (e.g., XAUUSD)")
    parser.add_argument("--type", type=str, choices=["tick", "ohlcv"], default="tick", help="Data type to download")
    parser.add_argument("--timeframe", type=str, default="1m", help="Timeframe if type is ohlcv (e.g., 1m, 1h)")
    parser.add_argument("--start", type=str, required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, required=True, help="End date (YYYY-MM-DD)")
    
    args = parser.parse_args()
    
    engine = HistoricalIngestionEngine()
    
    start_dt = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    
    if args.type == "tick":
        engine.download_mt5_ticks(args.symbol, start_dt, end_dt)
    else:
        engine.download_mt5_ohlcv(args.symbol, args.timeframe, start_dt, end_dt)
