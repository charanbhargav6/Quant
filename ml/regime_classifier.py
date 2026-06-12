"""
CRAVE v10.0 — Regime Classifier
=================================
Detects the current market regime using machine learning.
Trained on YOUR OWN paper trading data — not generic data.

REGIMES:
  TRENDING_UP   — strong directional move, SMC long setups work well
  TRENDING_DOWN — strong directional move, SMC short setups work well
  RANGING       — sideways chop, SMC signals fail frequently → skip
  VOLATILE      — high ATR expansion, news-driven → smaller sizes only

WHY THIS MATTERS:
  The same SMC setup in a trending market has ~65% win rate.
  The same setup in a ranging market has ~40% win rate.
  Knowing the regime before entry is worth +20-25% win rate.

TRAINING:
  Requires 500+ completed paper trades in database.
  Each trade has: features at entry + outcome (win/loss + R).
  The model learns: "which feature combinations lead to wins in which regime?"

  Train manually:
    from ml.regime_classifier import regime_model
    regime_model.train()

  Auto-retrains monthly when 100+ new trades added.

USAGE (after training):
  from ml.regime_classifier import regime_model

  regime = regime_model.predict("XAUUSD=X", df_1h)
  # → "TRENDING_UP" / "TRENDING_DOWN" / "RANGING" / "VOLATILE"

  ok = regime_model.is_favourable(regime)
  # → True if SMC signals work in this regime
"""

import logging
import json
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("crave.ml.regime")

# Model storage path
# FIX M7: Extension changed from .json to .pkl — file uses pickle, not JSON.
MODEL_PATH = Path(__file__).parent / "models" / "regime_classifier.pkl"
MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)


class RegimeClassifier:

    REGIMES = ["TRENDING_UP", "TRENDING_DOWN", "RANGING", "VOLATILE"]

    # FIX M8: Single source of truth from config.
    # 100 = minimum for initial training, 500+ = production quality.
    @property
    def MIN_TRAINING_ROWS(self) -> int:
        try:
            from config.config import ML
            return ML.get("min_training_rows", 100)
        except Exception:
            return 100

    @property
    def RETRAIN_INTERVAL_TRADES(self) -> int:
        try:
            from config.config import ML
            return ML.get("retrain_interval_trades", 100)
        except Exception:
            return 100

    def __init__(self):
        self._model        = None
        self._scaler       = None
        self._trained      = False
        self._train_count  = 0
        self._last_regime: Optional[str] = None
        self._load_model()

    # ─────────────────────────────────────────────────────────────────────────
    # RULE-BASED REGIME (works before ML is trained)
    # ─────────────────────────────────────────────────────────────────────────

    def detect_regime_rules(self, df: pd.DataFrame,
                             symbol: str = "") -> str:
        """
        Rule-based regime detection.
        Used before ML model is trained (first 500 trades).
        Also used as fallback if ML model fails.

        Logic:
          VOLATILE:      ATR expanding >40% above 20d average
          TRENDING_UP:   EMA21 > EMA50 > EMA200, price above all, ADX-like score
          TRENDING_DOWN: EMA21 < EMA50 < EMA200, price below all
          RANGING:       everything else
        """
        if len(df) < 50:
            return "RANGING"   # Not enough data = assume worst case

        try:
            close  = df['close']
            ema21  = close.ewm(span=21, adjust=False).mean()
            ema50  = close.rolling(50).mean()
            ema200 = close.rolling(200).mean()

            last        = close.iloc[-1]
            e21         = ema21.iloc[-1]
            e50         = ema50.iloc[-1]
            e200        = ema200.iloc[-1] if not pd.isna(ema200.iloc[-1]) else e50

            # ATR expansion check — independent baseline avoids desensitization
            tr = pd.concat([
                df['high'] - df['low'],
                (df['high'] - df['close'].shift()).abs(),
                (df['low']  - df['close'].shift()).abs(),
            ], axis=1).max(axis=1)

            atr_now = tr.ewm(alpha=1/14, adjust=False).mean().iloc[-1]
            # Use an older, non-overlapping slice so sustained volatility
            # doesn't inflate the baseline and suppress VOLATILE detection.
            if len(tr) >= 100:
                hist_tr = tr.iloc[:-50] if len(tr) > 100 else tr
                atr_avg = hist_tr.mean()
            else:
                atr_avg = tr.mean()

            if atr_avg > 0 and atr_now / atr_avg > 1.4:
                return "VOLATILE"

            # Trend detection — relaxed: only require EMA21 vs EMA50
            bullish_ema = last > e21 > e50
            bearish_ema = last < e21 < e50

            # Directional consistency: last N candles moving same direction
            n = 10
            if len(close) >= n:
                recent_change = (close.iloc[-1] - close.iloc[-n]) / close.iloc[-n]
                strong_up   = recent_change > 0.002  and bullish_ema
                strong_down = recent_change < -0.002 and bearish_ema
            else:
                strong_up = strong_down = False

            if strong_up:
                return "TRENDING_UP"
            if strong_down:
                return "TRENDING_DOWN"

            # ADX-based trending detection as fallback
            try:
                high = df['high']
                low = df['low']
                plus_dm = high.diff().clip(lower=0)
                minus_dm = (-low.diff()).clip(lower=0)
                tr = pd.concat([
                    high - low,
                    (high - close.shift()).abs(),
                    (low - close.shift()).abs(),
                ], axis=1).max(axis=1)
                atr14 = tr.ewm(alpha=1/14, adjust=False).mean()
                plus_di = 100 * (plus_dm.ewm(alpha=1/14, adjust=False).mean() / atr14)
                minus_di = 100 * (minus_dm.ewm(alpha=1/14, adjust=False).mean() / atr14)
                dx = (abs(plus_di - minus_di) / (plus_di + minus_di)) * 100
                adx = dx.ewm(alpha=1/14, adjust=False).mean().iloc[-1]
                if adx > 20:
                    if plus_di.iloc[-1] > minus_di.iloc[-1]:
                        return "TRENDING_UP"
                    else:
                        return "TRENDING_DOWN"
            except Exception:
                pass

            return "RANGING"

        except Exception as e:
            logger.debug(f"[Regime] Rule detection error: {e}")
            return "RANGING"

    # ─────────────────────────────────────────────────────────────────────────
    # ML PREDICTION (after training)
    # ─────────────────────────────────────────────────────────────────────────

    def predict(self, symbol: str,
                df: pd.DataFrame) -> str:
        """
        Predict market regime. Uses ML if trained, rules otherwise.
        Always returns one of: TRENDING_UP / TRENDING_DOWN / RANGING / VOLATILE
        """
        # Always run rule-based first (fast, interpretable)
        rule_regime = self.detect_regime_rules(df, symbol)

        # If ML model not trained yet, use rules
        if not self._trained or self._model is None:
            self._last_regime = rule_regime
            return rule_regime

        # Use ML model
        try:
            from ml.feature_engineering import extract_features
            features = extract_features(symbol, df, {})
            X = self._features_to_array(features)

            if X is None:
                return rule_regime

            probs  = self._model.predict_proba(X)[0]
            idx    = np.argmax(probs)
            regime = self.REGIMES[idx]

            # Confidence threshold: if ML is unsure, fall back to rules
            if probs[idx] < 0.55:
                logger.debug(
                    f"[Regime] Low ML confidence {probs[idx]:.2f} — "
                    f"using rules: {rule_regime}"
                )
                return rule_regime

            self._last_regime = regime
            logger.debug(f"[Regime] ML: {regime} ({probs[idx]:.2f} confidence)")
            return regime

        except Exception as e:
            logger.debug(f"[Regime] ML prediction failed: {e}")
            return rule_regime

    def is_favourable(self, regime: str) -> bool:
        """
        Is the SMC trend pipeline active for this regime?
        RANGING routes to mean-reversion instead.
        VOLATILE returns True but requires call to should_reduce_size() for
        half-sizing — is_favourable() alone does NOT imply full position size.

        NOTE: trading_loop._regime_checked_analyse() uses this method for routing.
        Any change to the routing logic must be reflected here and vice versa.
        """
        return regime in ("TRENDING_UP", "TRENDING_DOWN", "VOLATILE")

    def should_reduce_size(self, regime: str) -> bool:
        """Should we use reduced position size in this regime?"""
        return regime == "VOLATILE"

    # ─────────────────────────────────────────────────────────────────────────
    # TRAINING
    # ─────────────────────────────────────────────────────────────────────────

    def train(self) -> bool:
        """
        Train the regime classifier on paper trading data.
        Call after 500+ paper trades.

        Uses Random Forest — interpretable, doesn't overfit easily,
        works well with the feature set we have.
        """
        logger.info("[Regime] Starting training...")

        try:
            from xgboost import XGBClassifier
            from sklearn.preprocessing import StandardScaler
            from sklearn.model_selection import cross_val_score
            from ml.feature_engineering import get_training_dataframe

            df = get_training_dataframe(min_rows=self.MIN_TRAINING_ROWS)
            if df is None:
                logger.warning(
                    f"[Regime] Not enough training data "
                    f"(need {self.MIN_TRAINING_ROWS}+ completed trades)."
                )
                return False

            # Outlier removal on entry-time features only.
            # r_multiple/outcome_class are forward-looking labels — including them
            # removes high-R winners/deep losers as anomalies, creating survivorship
            # bias before labeling even runs.
            _OUTCOME_COLS = {"regime_label", "r_multiple", "outcome",
                             "outcome_class", "close_price"}
            n_before = len(df)
            try:
                from sklearn.ensemble import IsolationForest
                numeric_cols = [c for c in df.columns
                                if df[c].dtype in (float, int)
                                and c not in _OUTCOME_COLS]
                X_check = df[numeric_cols].fillna(0).values
                iso     = IsolationForest(
                    contamination=min(0.05, max(0.01, 10 / len(df))),
                    random_state=42,
                    n_jobs=-1,
                )
                mask = iso.fit_predict(X_check) == 1  # 1=inlier, -1=outlier
                df   = df[mask].reset_index(drop=True)
                n_removed = n_before - len(df)
                logger.info(
                    f"[Regime] Outlier removal: {n_removed} rows removed "
                    f"({n_removed/n_before*100:.1f}%), "
                    f"{len(df)} rows remaining."
                )
            except Exception as e:
                logger.warning(
                    f"[Regime] Outlier removal failed (non-fatal): {e}. "
                    f"Training on all {n_before} rows."
                )

            if len(df) < 20:
                logger.warning("[Regime] Too few rows after outlier removal.")
                return False

            # Label regimes based on features
            df["regime_label"] = df.apply(self._label_regime, axis=1)

            # Feature columns
            feature_cols = [c for c in df.columns
                            if c not in ["r_multiple", "outcome",
                                         "outcome_class", "regime_label",
                                         "close_price"]]

            X = df[feature_cols].fillna(0).values
            y = df["regime_label"].values

            if len(np.unique(y)) < 2:
                logger.warning("[Regime] Not enough regime diversity to train.")
                return False

            # Scale features
            scaler = StandardScaler()
            X_scaled = scaler.fit_transform(X)

            # Class weights — VOLATILE (label 3) is rare; without weighting
            # XGBoost ignores it, silently disabling the half-sizing gate.
            from collections import Counter
            counts = Counter(y.tolist())
            n_total = len(y)
            n_classes = len(counts)
            sample_weight = np.array([
                n_total / (n_classes * counts[label]) for label in y
            ])

            # Train XGBoost
            model = XGBClassifier(
                n_estimators=100,
                max_depth=5,
                learning_rate=0.1,
                n_jobs=-1,
                random_state=42
            )
            model.fit(X_scaled, y, sample_weight=sample_weight)

            # Cross-validation with TimeSeriesSplit — prevents future data leaking
            # into training folds (standard k-fold inflates accuracy on time series).
            from sklearn.model_selection import TimeSeriesSplit
            tscv      = TimeSeriesSplit(n_splits=5)
            cv_scores = cross_val_score(model, X_scaled, y, cv=tscv)
            logger.info(
                f"[Regime] Training complete. "
                f"CV accuracy: {cv_scores.mean():.3f} ± {cv_scores.std():.3f}"
            )

            # Minimum quality gate — block deployment of useless models
            min_cv = 0.45
            try:
                from config.config import ML as _ML_CFG
                min_cv = _ML_CFG.get("min_cv_accuracy", 0.45)
            except Exception:
                pass

            if cv_scores.mean() < min_cv:
                logger.warning(
                    f"[Regime] CV accuracy {cv_scores.mean():.1%} below minimum "
                    f"{min_cv:.0%}. Model NOT deployed — using rules only."
                )
                return False

            self._model   = model
            self._scaler  = scaler
            self._trained = True
            self._feature_cols = feature_cols

            # Save model
            self._save_model(feature_cols, cv_scores.mean())

            # Notify
            try:
                from interfaces.telegram_interface import tg
                tg.send(
                    f"🤖 <b>Regime Classifier Trained</b>\n"
                    f"Rows: {len(df)}\n"
                    f"CV Accuracy: {cv_scores.mean():.1%}\n"
                    f"Regimes: {list(np.unique(y))}"
                )
            except Exception:
                pass

            # Log feature importances to neural_memory
            try:
                importances = dict(zip(feature_cols, model.feature_importances_))
                importances = dict(sorted(importances.items(), key=lambda x: x[1], reverse=True))
                from src.core.neural_memory import NeuralMemory
                nm = NeuralMemory()
                nm.store("regime_feature_importances", {
                    "importances": importances,
                    "cv_accuracy": float(cv_scores.mean()),
                    "n_rows": len(df),
                    "trained_at": datetime.now(timezone.utc).isoformat(),
                })
                top3 = list(importances.items())[:3]
                logger.info(f"[Regime] Top 3 features: {top3}")
            except Exception as e:
                logger.debug(f"[Regime] Feature importance logging failed: {e}")

            return True

        except ImportError:
            logger.warning(
                "[Regime] scikit-learn not installed. "
                "Run: pip install scikit-learn"
            )
            return False
        except Exception as e:
            logger.error(f"[Regime] Training failed: {e}")
            return False

    def _label_regime(self, row) -> int:
        """
        Derive regime label from trade features using session-tagged ground truth.
        Uses actual EMA alignment + ATR state at TRADE CLOSE (not entry).

        Session-tagged labeling (ground truth):
          TRENDING_UP:   EMA21>EMA50>EMA200 at close AND positive R
          TRENDING_DOWN: EMA21<EMA50<EMA200 at close AND positive R
          VOLATILE:      ATR expansion >1.4 at close
          RANGING:       everything else
        """
        atr_exp = row.get("atr_expansion", 1.0)

        if atr_exp > 1.4:
            return 3   # VOLATILE

        # Use EMA alignment at close as ground truth signal.
        # Regime is a market structure property — NOT a trade outcome.
        # The r > 0 condition was removed: a losing trade in a trending market
        # must still be labeled as trending, otherwise the classifier learns
        # "RANGING = bad outcome" instead of "RANGING = sideways structure."
        ema21  = row.get("ema21_close", row.get("ema21", 0))
        ema50  = row.get("ema50_close", row.get("ema50", 0))
        ema200 = row.get("ema200_close", row.get("ema200", 0))

        if ema21 > ema50 > ema200:
            return 0   # TRENDING_UP
        if ema21 < ema50 < ema200:
            return 1   # TRENDING_DOWN

        return 2   # RANGING (ambiguous EMA stack)

    def _features_to_array(self, features: dict):
        """Convert features dict to scaled array for prediction."""
        if not hasattr(self, "_feature_cols"):
            return None
        try:
            row  = [features.get(col, 0) for col in self._feature_cols]
            arr  = np.array(row).reshape(1, -1)
            return self._scaler.transform(arr)
        except Exception:
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # MODEL PERSISTENCE
    # ─────────────────────────────────────────────────────────────────────────

    def _save_model(self, feature_cols: list, accuracy: float):
        """Save trained model to disk."""
        try:
            import pickle
            model_data = {
                "model":        self._model,
                "scaler":       self._scaler,
                "feature_cols": feature_cols,
                "accuracy":     accuracy,
                "trained_at":   datetime.now(timezone.utc).isoformat(),
                "regimes":      self.REGIMES,
            }
            with open(MODEL_PATH, "wb") as f:
                pickle.dump(model_data, f)
            logger.info(f"[Regime] Model saved to {MODEL_PATH}")
        except Exception as e:
            logger.error(f"[Regime] Model save failed: {e}")

    def _load_model(self):
        """Load trained model from disk if available."""
        if not MODEL_PATH.exists():
            return

        try:
            import pickle
            with open(MODEL_PATH, "rb") as f:
                data = pickle.load(f)

            self._model        = data["model"]
            self._scaler       = data["scaler"]
            self._feature_cols = data["feature_cols"]
            self._trained      = True

            logger.info(
                f"[Regime] Model loaded. "
                f"Accuracy={data.get('accuracy', 0):.1%} | "
                f"Trained={data.get('trained_at', '?')[:10]}"
            )
        except Exception as e:
            logger.info(f"[Regime] No trained model ({e}) — using rules only.")

    def get_status(self) -> dict:
        return {
            "trained":       self._trained,
            "model_path":    str(MODEL_PATH),
            "last_regime":   self._last_regime,
            "using":         "ML + rules" if self._trained else "rules only",
        }


# ── Singleton ─────────────────────────────────────────────────────────────────
try:
    from ml.deep_regime_model import deep_regime_model as regime_model
    logger.info("[Regime] Active Model: DeepRegimeClassifier (PyTorch LSTM)")
except ImportError as e:
    logger.info(f"[Regime] PyTorch not found or deep regime failed: {e}. Falling back to XGBoost/Rule-based.")
    regime_model = RegimeClassifier()
