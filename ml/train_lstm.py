import os
import sys
import numpy as np
import pandas as pd
import yfinance as yf
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import TensorDataset, DataLoader
from pathlib import Path
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("crave.ml.train")

# Fix path for imports
_root = str(Path(__file__).resolve().parents[1])
if _root not in sys.path:
    sys.path.insert(0, _root)

from ml.deep_regime_model import LSTMRegimeNet, DeepRegimeClassifier, MODEL_PATH

# 0=TRENDING_UP, 1=TRENDING_DOWN, 2=RANGING, 3=VOLATILE
REGIMES = DeepRegimeClassifier.REGIMES
SEQ_LENGTH = DeepRegimeClassifier.SEQ_LENGTH
FUTURE_LOOKAHEAD = 24  # candles to look ahead for outcome labeling

def fetch_data(symbol, period="2y"):
    logger.info(f"Fetching {period} 1h data for {symbol}...")
    df = yf.download(symbol, period=period, interval="1h", progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [c.lower() for c in df.columns]
    df.dropna(inplace=True)
    if 'adj close' in df.columns:
        df.drop(columns=['adj close'], inplace=True)
    return df

def calculate_atr(df, period=14):
    tr = pd.concat([
        df['high'] - df['low'],
        (df['high'] - df['close'].shift()).abs(),
        (df['low'] - df['close'].shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0/period, adjust=False).mean()

def generate_future_labels(df):
    """
    Looks ahead 24 candles to determine the true regime.
    """
    logger.info("Generating future predictive labels...")
    df['atr'] = calculate_atr(df, 14)
    labels = np.zeros(len(df), dtype=int)
    
    closes = df['close'].values
    highs = df['high'].values
    lows = df['low'].values
    atrs = df['atr'].values
    
    for i in range(len(df) - FUTURE_LOOKAHEAD):
        current_close = closes[i]
        current_atr = atrs[i]
        
        if current_atr == 0:
            labels[i] = 2 # Ranging
            continue
            
        future_highs = highs[i+1 : i+1+FUTURE_LOOKAHEAD]
        future_lows = lows[i+1 : i+1+FUTURE_LOOKAHEAD]
        
        max_f = np.max(future_highs)
        min_f = np.min(future_lows)
        
        up_move = (max_f - current_close) / current_atr
        down_move = (current_close - min_f) / current_atr
        
        # Volatile: sweeps both sides > 1.5 ATR
        if up_move > 1.5 and down_move > 1.5:
            labels[i] = 3
        # Trending Up: goes up > 2 ATR, doesn't drop > 1 ATR
        elif up_move > 2.0 and down_move < 1.0:
            labels[i] = 0
        # Trending Down: goes down > 2 ATR, doesn't bounce > 1 ATR
        elif down_move > 2.0 and up_move < 1.0:
            labels[i] = 1
        # Everything else is Ranging
        else:
            labels[i] = 2
            
    df['label'] = labels
    return df

def extract_sequences(df):
    """
    Create (N, SEQ_LENGTH, 4) tensor and (N,) labels.
    Features: normalized open, high, low, volume
    """
    logger.info("Extracting sequences...")
    closes = df['close'].values
    opens = df['open'].values
    highs = df['high'].values
    lows = df['low'].values
    vols = df.get('volume', pd.Series(np.zeros(len(df)))).values
    labels = df['label'].values
    
    X, y = [], []
    
    for i in range(SEQ_LENGTH, len(df) - FUTURE_LOOKAHEAD):
        seq_c = closes[i-SEQ_LENGTH : i]
        seq_o = opens[i-SEQ_LENGTH : i]
        seq_h = highs[i-SEQ_LENGTH : i]
        seq_l = lows[i-SEQ_LENGTH : i]
        seq_v = vols[i-SEQ_LENGTH : i]
        
        # Normalize relative to close
        f_open = (seq_o - seq_c) / seq_c
        f_high = (seq_h - seq_c) / seq_c
        f_low  = (seq_l - seq_c) / seq_c
        
        v_max = np.max(seq_v) if np.max(seq_v) > 0 else 1
        f_vol = seq_v / v_max
        
        seq_features = np.column_stack((f_open, f_high, f_low, f_vol))
        X.append(seq_features)
        y.append(labels[i]) # Label of the current candle based on its future
        
    return np.array(X, dtype=np.float32), np.array(y, dtype=np.int64)

def main():
    symbols = ["GC=F", "EURUSD=X", "BTC-USD", "ETH-USD"]
    all_X, all_y = [], []
    
    for sym in symbols:
        try:
            df = fetch_data(sym, period="730d")
            if len(df) < SEQ_LENGTH + FUTURE_LOOKAHEAD + 1:
                logger.warning(f"Not enough data for {sym}")
                continue
                
            df = generate_future_labels(df)
            X, y = extract_sequences(df)
            
            if X.shape[0] > 0:
                all_X.append(X)
                all_y.append(y)
        except Exception as e:
            logger.error(f"Failed to process {sym}: {e}")
            
    if not all_X:
        logger.error("No data fetched.")
        return
        
    X_full = np.concatenate(all_X, axis=0)
    y_full = np.concatenate(all_y, axis=0)
    
    logger.info(f"Total dataset: {X_full.shape[0]} sequences of shape {X_full.shape[1:]}")
    
    # Class balancing weights
    from collections import Counter
    counts = Counter(y_full)
    logger.info(f"Class distribution: {counts}")
    
    weights = np.zeros(len(REGIMES), dtype=np.float32)
    for k, v in counts.items():
        weights[k] = len(y_full) / (len(REGIMES) * v)
        
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    class_weights = torch.tensor(weights).to(device)
    
    # DataLoader
    dataset = TensorDataset(torch.tensor(X_full), torch.tensor(y_full))
    loader = DataLoader(dataset, batch_size=256, shuffle=True)
    
    model = LSTMRegimeNet(input_size=4, num_classes=len(REGIMES)).to(device)
    criterion = nn.CrossEntropyLoss(weight=class_weights)
    optimizer = optim.Adam(model.parameters(), lr=0.001)
    
    EPOCHS = 15
    logger.info(f"Starting PyTorch training for {EPOCHS} epochs on {device}...")
    
    for epoch in range(EPOCHS):
        model.train()
        total_loss = 0
        correct = 0
        
        for batch_X, batch_y in loader:
            batch_X, batch_y = batch_X.to(device), batch_y.to(device)
            
            optimizer.zero_grad()
            outputs = model(batch_X)
            loss = criterion(outputs, batch_y)
            loss.backward()
            optimizer.step()
            
            total_loss += loss.item()
            _, preds = torch.max(outputs, 1)
            correct += torch.sum(preds == batch_y).item()
            
        acc = correct / len(dataset)
        logger.info(f"Epoch {epoch+1}/{EPOCHS} | Loss: {total_loss/len(loader):.4f} | Accuracy: {acc*100:.2f}%")
        
    # Save the real model
    torch.save(model.state_dict(), MODEL_PATH)
    logger.info(f"✅ LSTM Training complete. Saved massive backtest model to {MODEL_PATH}")

if __name__ == "__main__":
    main()
