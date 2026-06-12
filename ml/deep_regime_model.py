import os
import logging
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset
import pandas as pd
import numpy as np
from pathlib import Path

logger = logging.getLogger("crave.ml.deep_regime")

# Model storage path
MODEL_PATH = Path(__file__).parent / "models" / "deep_regime_lstm.pth"
MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)

class LSTMRegimeNet(nn.Module):
    def __init__(self, input_size, hidden_size=64, num_layers=2, num_classes=4):
        super(LSTMRegimeNet, self).__init__()
        self.hidden_size = hidden_size
        self.num_layers = num_layers
        
        # Batch First: (batch_size, seq_length, input_size)
        self.lstm = nn.LSTM(input_size, hidden_size, num_layers, 
                            batch_first=True, dropout=0.2 if num_layers > 1 else 0)
        self.fc1 = nn.Linear(hidden_size, 32)
        self.relu = nn.ReLU()
        self.dropout = nn.Dropout(0.3)
        self.fc2 = nn.Linear(32, num_classes)
        self.softmax = nn.Softmax(dim=1)
        
    def forward(self, x):
        # x shape: (batch_size, seq_length, input_size)
        h0 = torch.zeros(self.num_layers, x.size(0), self.hidden_size).to(x.device)
        c0 = torch.zeros(self.num_layers, x.size(0), self.hidden_size).to(x.device)
        
        # out shape: (batch_size, seq_length, hidden_size)
        out, _ = self.lstm(x, (h0, c0))
        
        # Take the output of the last time step
        out = out[:, -1, :]
        out = self.fc1(out)
        out = self.relu(out)
        out = self.dropout(out)
        out = self.fc2(out)
        return self.softmax(out)

class DeepRegimeClassifier:
    REGIMES = ["TRENDING_UP", "TRENDING_DOWN", "RANGING", "VOLATILE"]
    SEQ_LENGTH = 24  # Look back 24 hours
    
    def __init__(self):
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.model = None
        self._trained = False
        self._load_model()
        
    def _extract_sequence_features(self, df: pd.DataFrame) -> np.ndarray:
        """
        Converts OHLCV dataframe into a sequence tensor.
        Requires at least SEQ_LENGTH rows.
        """
        if len(df) < self.SEQ_LENGTH:
            return None
            
        # Get the last SEQ_LENGTH rows
        seq_df = df.tail(self.SEQ_LENGTH).copy()
        
        features = []
        for i in range(len(seq_df)):
            row = seq_df.iloc[i]
            # Simple normalized features
            close = row['close']
            c_open = (row['open'] - close) / close
            c_high = (row['high'] - close) / close
            c_low = (row['low'] - close) / close
            
            # Extract basic indicators if they exist, or calculate them
            # For this MVP, we use normalized price action
            features.append([c_open, c_high, c_low, row.get('volume', 0)])
            
        # Normalize volume
        arr = np.array(features, dtype=np.float32)
        vol_col = arr[:, 3]
        vol_max = np.max(vol_col) if np.max(vol_col) > 0 else 1
        arr[:, 3] = vol_col / vol_max
        
        return arr

    def predict(self, symbol: str, df: pd.DataFrame) -> str:
        from ml.regime_classifier import RegimeClassifier
        # Fallback to rule-based if model not loaded
        if not self._trained or self.model is None:
            return RegimeClassifier().detect_regime_rules(df, symbol)
            
        seq = self._extract_sequence_features(df)
        if seq is None:
            return RegimeClassifier().detect_regime_rules(df, symbol)
            
        self.model.eval()
        with torch.no_grad():
            x = torch.tensor(seq, dtype=torch.float32).unsqueeze(0).to(self.device)
            probs = self.model(x).cpu().numpy()[0]
            
        idx = np.argmax(probs)
        regime = self.REGIMES[idx]
        
        if probs[idx] < 0.5:
            return RegimeClassifier().detect_regime_rules(df, symbol)
            
        return regime

    def is_favourable(self, regime: str) -> bool:
        return regime in ("TRENDING_UP", "TRENDING_DOWN", "VOLATILE")

    def should_reduce_size(self, regime: str) -> bool:
        return regime == "VOLATILE"

    def _load_model(self):
        if MODEL_PATH.exists():
            try:
                # 4 features: open_pct, high_pct, low_pct, vol_norm
                self.model = LSTMRegimeNet(input_size=4, num_classes=len(self.REGIMES)).to(self.device)
                self.model.load_state_dict(torch.load(MODEL_PATH, map_location=self.device, weights_only=True))
                self.model.eval()
                self._trained = True
                logger.info(f"[DeepRegime] Loaded LSTM model from {MODEL_PATH} (Device: {self.device})")
            except Exception as e:
                logger.error(f"[DeepRegime] Failed to load model: {e}")
                self._trained = False

    def train(self) -> bool:
        """
        Train the LSTM on historical data sequences.
        Note: This is a placeholder for the actual training loop.
        In a full implementation, you would construct (N, SEQ_LENGTH, F) tensors
        from historical data and backpropagate.
        """
        logger.info("[DeepRegime] LSTM training initiated...")
        # For MVP, we instantiate the architecture and save it to verify PyTorch integration
        self.model = LSTMRegimeNet(input_size=4, num_classes=len(self.REGIMES)).to(self.device)
        torch.save(self.model.state_dict(), MODEL_PATH)
        self._trained = True
        logger.info(f"[DeepRegime] Mock training complete. Model saved to {MODEL_PATH}")
        return True

deep_regime_model = DeepRegimeClassifier()
