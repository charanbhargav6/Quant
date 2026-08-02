import pandas as pd
import numpy as np
import os
from datetime import datetime, timedelta

def generate_synthetic_gold_data(start_date: str, end_date: str, filename: str):
    start = pd.to_datetime(start_date, utc=True)
    end = pd.to_datetime(end_date, utc=True)
    
    # Generate 1-minute timestamps
    dates = pd.date_range(start=start, end=end, freq='1min')
    n = len(dates)
    
    # Base price for Gold in 2023
    base_price = 1900.0
    
    # Generate random walk with drift
    np.random.seed(42)
    returns = np.random.normal(loc=0.000001, scale=0.0002, size=n)
    
    # Volatility clustering (GARCH-like)
    vol_shocks = np.random.lognormal(mean=0, sigma=0.5, size=n)
    # Smooth the volatility
    smoothed_vol = pd.Series(vol_shocks).rolling(window=60, min_periods=1).mean().values
    
    returns = returns * smoothed_vol
    
    price_path = base_price * np.exp(np.cumsum(returns))
    
    # Generate OHLC
    # Open is the previous close (or base price for the first one)
    opens = np.roll(price_path, shift=1)
    opens[0] = base_price
    closes = price_path
    
    # Highs and lows around open and close
    highs = np.maximum(opens, closes) + np.abs(np.random.normal(0, 0.2, n)) * smoothed_vol * 10
    lows = np.minimum(opens, closes) - np.abs(np.random.normal(0, 0.2, n)) * smoothed_vol * 10
    
    # Volumes (higher when volatility is high)
    volumes = np.abs(np.random.normal(500, 200, n)) * (smoothed_vol + 0.5)
    
    df = pd.DataFrame({
        'open': opens,
        'high': highs,
        'low': lows,
        'close': closes,
        'volume': volumes
    }, index=dates)
    
    # Drop weekends to simulate forex market
    df = df[df.index.dayofweek < 5]
    
    os.makedirs('data/historical', exist_ok=True)
    filepath = os.path.join('data/historical', filename)
    df.to_parquet(filepath, engine='pyarrow')
    print(f"Generated {len(df)} rows of synthetic XAUUSD data to {filepath}")

if __name__ == "__main__":
    generate_synthetic_gold_data('2023-01-01', '2024-01-01', 'XAUUSD_1m_20230101_20240101.parquet')
