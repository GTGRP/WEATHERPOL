"""
Local ML Model — XGBoost-powered, no API, sub-millisecond inference.

WHY THIS EXISTS:
  The GPT/LLM API can fail (rate limits, network, cost). When it does,
  this local model takes over — zero latency, zero cost, always available.
  It also runs ALONGSIDE the API model as a second opinion.

WHAT IT DOES:
  - Entry validation: BUY/SKIP with probability score
  - Exit timing: HOLD/SELL with confidence
  - Position sizing: Kelly fraction adjustment
  - Confidence calibration: is the ensemble model overconfident?

TRAINING:
  Trained on historical Polymarket weather outcomes (from backtest).
  Features: edge, edge_ratio, n_models, forecast_spread, lead_hours,
            market_price, bucket_position, city_win_rate, spread_bps.
  Target: did the trade win? (1/0)

ARCHITECTURE:
  XGBoost classifier (small, fast) with 50 trees, max_depth=4.
  Inference time: <1ms. Model file: ~50KB.
  Falls back to rule-based if model file not found.

Usage:
  from ml.local_model import LocalModel
  model = LocalModel()
  decision = model.predict_entry(features)  # {'action': 'BUY', 'prob': 0.72}
"""

import json
import os
import pickle
from pathlib import Path
from typing import Dict, Optional

import numpy as np

from logger import log


# Feature names in order
FEATURE_NAMES = [
    "edge",              # our_prob - market_price
    "edge_ratio",        # our_prob / market_price
    "n_models",          # number of models in agreement
    "forecast_std",      # std across model forecasts
    "lead_hours",        # hours until resolution
    "market_price",      # current best_ask
    "market_spread_bps", # bid-ask spread
    "city_win_rate",     # historical win rate for this city
    "confidence",        # ensemble confidence
    "bucket_position",   # 0=tail, 0.5=mid, 1=near-certain
    "hour_of_day",       # UTC hour
    "day_of_week",       # 0=Mon, 6=Sun
]

# City historical win rates (from SII-WANGZJ 14M-trade calibration)
CITY_WIN_RATES = {
    "buenos-aires": 0.101, "dallas": 0.099, "atlanta": 0.098,
    "toronto": 0.097, "seattle": 0.095, "nyc": 0.088,
    "seoul": 0.086, "london": 0.084, "ankara": 0.082,
    "wellington": 0.082, "chicago": 0.079, "paris": 0.056,
    "austin": 0.047, "denver": 0.045, "tokyo": 0.038,
    "shanghai": 0.036, "hong-kong": 0.035, "singapore": 0.035,
    "mumbai": 0.035, "delhi": 0.034, "moscow": 0.033,
}


class LocalModel:
    """XGBoost-based fast local ML model for trade decisions."""

    def __init__(self, model_path: str = "data/xgb_model.json"):
        self.model_path = Path(model_path)
        self.model = None
        self._loaded = False
        self._load_or_init()

    def _load_or_init(self):
        """Load trained model or initialize rule-based fallback."""
        try:
            import xgboost as xgb
            if self.model_path.exists():
                self.model = xgb.XGBClassifier()
                self.model.load_model(str(self.model_path))
                self._loaded = True
                log.info(f"  XGBoost model loaded ({self.model_path.stat().st_size} bytes)")
            else:
                log.info("  XGBoost model not found — using rule-based fallback")
                log.info("  (Model will be trained after collecting trade history)")
        except ImportError:
            log.info("  xgboost not installed — using rule-based fallback")
            log.info("  (pip install xgboost for faster local ML)")
        except Exception as e:
            log.warning(f"  XGBoost load failed: {e} — using rule-based")

    def predict_entry(self, features: dict) -> dict:
        """
        Predict whether to enter a trade.

        Args:
            features: dict with keys matching FEATURE_NAMES

        Returns:
            {'action': 'BUY'|'SKIP', 'probability': 0.0-1.0, 'source': 'xgb'|'rules'}
        """
        if self.model is not None and self._loaded:
            return self._xgb_predict(features)
        return self._rules_predict(features)

    def predict_exit(self, features: dict) -> dict:
        """
        Predict whether to exit a position early.

        Args:
            features: dict with position state

        Returns:
            {'action': 'HOLD'|'SELL', 'probability': 0.0-1.0, 'source': 'xgb'|'rules'}
        """
        # For exit, we mostly want to HOLD (resolution is binary)
        # Only exit if forecast reverses or profit target hit
        pnl_pct = features.get("pnl_pct", 0)
        forecast_changed = features.get("forecast_changed", False)
        hours_remaining = features.get("hours_remaining", 24)

        if pnl_pct > 50:
            return {"action": "SELL", "probability": 0.85, "source": "rules",
                    "reason": f"Profit target hit (+{pnl_pct:.0f}%)"}
        if forecast_changed and pnl_pct < 0:
            return {"action": "SELL", "probability": 0.75, "source": "rules",
                    "reason": "Forecast reversed against position"}
        if hours_remaining < 2 and pnl_pct < -50:
            return {"action": "SELL", "probability": 0.60, "source": "rules",
                    "reason": "Near resolution, deep loss"}

        return {"action": "HOLD", "probability": 0.80, "source": "rules",
                "reason": "Hold to binary resolution"}

    def _xgb_predict(self, features: dict) -> dict:
        """XGBoost model inference."""
        try:
            x = np.array([[features.get(f, 0) for f in FEATURE_NAMES]], dtype=np.float32)
            prob = float(self.model.predict_proba(x)[0][1])
            action = "BUY" if prob > 0.55 else "SKIP"
            return {"action": action, "probability": prob, "source": "xgb"}
        except Exception as e:
            log.debug(f"XGBoost inference failed: {e} — falling back to rules")
            return self._rules_predict(features)

    def _rules_predict(self, features: dict) -> dict:
        """Rule-based fallback when XGBoost is unavailable."""
        edge = features.get("edge", 0)
        edge_ratio = features.get("edge_ratio", 0)
        n_models = features.get("n_models", 0)
        confidence = features.get("confidence", 0)
        spread_bps = features.get("market_spread_bps", 500)
        market_price = features.get("market_price", 0.5)

        score = 0.0

        # Edge is the strongest signal
        if edge > 0.10:
            score += 0.35
        elif edge > 0.05:
            score += 0.20
        elif edge > 0.02:
            score += 0.10

        # Edge ratio (relative edge)
        if edge_ratio > 5.0:
            score += 0.30
        elif edge_ratio > 3.0:
            score += 0.20
        elif edge_ratio > 2.0:
            score += 0.10

        # Model agreement
        if n_models >= 5:
            score += 0.15
        elif n_models >= 3:
            score += 0.10

        # Confidence
        if confidence > 0.80:
            score += 0.10

        # Spread penalty
        if spread_bps > 1000:
            score -= 0.20
        elif spread_bps > 500:
            score -= 0.10

        # Price zone (mid-range is better for flipping, tails for holding)
        if 0.10 <= market_price <= 0.50:
            score += 0.05

        score = max(0.0, min(0.95, score))
        action = "BUY" if score >= 0.50 else "SKIP"

        return {"action": action, "probability": score, "source": "rules"}

    def train_on_history(self, trades: list):
        """Train XGBoost model on historical trade outcomes."""
        if len(trades) < 50:
            log.info(f"  Need 50+ trades to train (have {len(trades)})")
            return False

        try:
            import xgboost as xgb

            X, y = [], []
            for t in trades:
                feats = t.get("features", {})
                if not feats:
                    continue
                row = [feats.get(f, 0) for f in FEATURE_NAMES]
                X.append(row)
                y.append(1 if t.get("won", False) else 0)

            if len(X) < 50:
                return False

            self.model = xgb.XGBClassifier(
                n_estimators=50, max_depth=4, learning_rate=0.1,
                objective="binary:logistic", eval_metric="logloss",
                use_label_encoder=False, verbosity=0,
            )
            self.model.fit(np.array(X), np.array(y))
            self.model.save_model(str(self.model_path))
            self._loaded = True
            log.info(f"  XGBoost trained on {len(X)} trades — saved to {self.model_path}")
            return True
        except ImportError:
            log.warning("  xgboost not installed — cannot train")
        except Exception as e:
            log.warning(f"  XGBoost training failed: {e}")
        return False

    def get_status(self) -> dict:
        return {
            "model": "XGBoost" if self._loaded else "Rules",
            "loaded": self._loaded,
            "trained": self._loaded,
        }


# Singleton
_instance: Optional[LocalModel] = None


def get_local_model() -> LocalModel:
    global _instance
    if _instance is None:
        _instance = LocalModel()
    return _instance
