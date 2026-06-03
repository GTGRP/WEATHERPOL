"""
ML Decision Engine — GPT-5.5 via Freemodel API for fast trading decisions.

Design principles:
- MINIMAL TOKENS: Each query is <200 tokens, response <100 tokens
- MARKET-SCOPED CONTEXT: Only active markets included, freed on close
- FAST: Single API call per decision cycle (~200ms)
- DECISIVE: Returns BUY/SKIP/SELL with confidence score

The ML is used for:
1. Signal validation (confirm/reject sniper signals)
2. Entry timing (should we buy now or wait?)
3. Position review (hold/sell open positions)
4. Market selection (which cities to prioritize today)

Token budget per call: ~150-300 tokens total (prompt + response)
"""

import time
import json
import requests
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone

from config import Config
from logger import log



class MLDecisionEngine:
    """Fast ML-powered trading decisions using GPT-5.5."""

    def __init__(self):
        self.base_url = Config.ML_API_URL
        self.api_key = Config.ML_API_KEY
        self.model = Config.ML_MODEL
        self.enabled = bool(self.api_key)
        self._session = requests.Session()
        self._session.headers.update({
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
        })
        self._cache: Dict[str, Tuple[float, Dict]] = {}
        self._cache_ttl = 120  # 2 minutes
        self._total_tokens_used = 0

        # Local model fallback (no API needed)
        self._local_model = None
        self._api_failures = 0
        self._api_failure_window = 0
        self._max_api_failures = 5  # switch to local after 5 failures

        if self.enabled:
            log.info(f"  ML Engine: {self.model} via {self.base_url[:30]}...")
        else:
            log.info("  ML Engine: API disabled — using local model only")

    @property
    def local_model(self):
        """Lazy-load local model."""
        if self._local_model is None:
            try:
                from ml.local_model import get_local_model
                self._local_model = get_local_model()
            except Exception as e:
                log.warning(f"  Local model init failed: {e}")
        return self._local_model

    def validate_signal(self, city: str, bucket_label: str, entry_price: float,
                        our_prob: float, edge: float, forecast_temp=0.0,
                        n_models: int = 3, weekly_context: str = '') -> Dict:
        """
        Ask ML to validate a trading signal. Returns:
        {action: 'BUY'|'SKIP', confidence: 0-1, reason: str}
        
        Uses minimal tokens (~150 total).
        """
        # Ensure forecast_temp is float
        try:
            forecast_temp = float(forecast_temp)
        except (ValueError, TypeError):
            forecast_temp = 0.0

        if not self.enabled:
            # Use local model when API is disabled
            if self.local_model is not None:
                return self.local_model.predict_entry({
                    "edge": edge, "edge_ratio": our_prob / max(entry_price, 0.01),
                    "n_models": n_models, "confidence": 0.6,
                    "market_spread_bps": 500, "market_price": entry_price,
                    "forecast_std": 1.5, "lead_hours": 24,
                    "city_win_rate": 0.09, "bucket_position": entry_price,
                    "hour_of_day": datetime.now(timezone.utc).hour,
                    "day_of_week": datetime.now(timezone.utc).weekday(),
                })
            return {'action': 'BUY', 'confidence': 0.7, 'reason': 'ML disabled'}

        # Check cache
        cache_key = f"{city}_{bucket_label}_{entry_price:.3f}"
        now = time.time()
        if cache_key in self._cache:
            ts, result = self._cache[cache_key]
            if now - ts < self._cache_ttl:
                return result

        # Ultra-compact prompt (~100 tokens)
        prompt = (
            f"Weather trade signal. Reply JSON only: {{\"action\":\"BUY\"|\"SKIP\",\"conf\":0-1,\"why\":\"<5 words>\"}}\n"
            f"City:{city} Bucket:{bucket_label} Price:${entry_price:.3f} "
            f"OurProb:{our_prob:.0%} Edge:{edge:.0%} "
            f"Forecast:{forecast_temp:.1f}°C Models:{n_models}\n"
            f"History:{weekly_context[:80]}"
        )

        result = self._query(prompt, max_tokens=60)
        self._cache[cache_key] = (now, result)
        return result

    def review_position(self, city: str, bucket_label: str, entry_price: float,
                        current_price: float, hold_hours: float,
                        resolution_hours: float) -> Dict:
        """
        Ask ML whether to hold or sell an open position.
        Returns: {action: 'HOLD'|'SELL', confidence: 0-1, reason: str}
        """
        if not self.enabled:
            return {'action': 'HOLD', 'confidence': 0.5, 'reason': 'ML disabled'}

        roi_pct = ((current_price - entry_price) / entry_price) * 100

        prompt = (
            f"Position review. Reply JSON: {{\"action\":\"HOLD\"|\"SELL\",\"conf\":0-1,\"why\":\"<5 words>\"}}\n"
            f"City:{city} {bucket_label} Entry:${entry_price:.3f} "
            f"Now:${current_price:.3f} ROI:{roi_pct:+.0f}% "
            f"Held:{hold_hours:.0f}h Left:{resolution_hours:.0f}h"
        )

        return self._query(prompt, max_tokens=50)

    def select_markets(self, available_cities: List[str],
                       weekly_context: str = '') -> List[str]:
        """
        Ask ML which cities to prioritize today.
        Returns ranked list of cities.
        """
        if not self.enabled:
            return available_cities[:8]

        prompt = (
            f"Rank cities for weather trading today. Reply JSON array of top 5: [\"city1\",\"city2\",...]\n"
            f"Available: {','.join(available_cities[:15])}\n"
            f"Performance: {weekly_context[:100]}"
        )

        result = self._query(prompt, max_tokens=40)
        if isinstance(result.get('raw'), list):
            return result['raw']
        return available_cities[:8]


    def _query(self, prompt: str, max_tokens: int = 60) -> Dict:
        """
        Make a single API call to the ML model.
        Optimized for speed and minimal token usage.
        """
        try:
            resp = self._session.post(
                f"{self.base_url}/chat/completions",
                json={
                    'model': self.model,
                    'messages': [
                        {'role': 'system', 'content': 'You are a weather trading assistant. Reply with JSON only. Be extremely concise.'},
                        {'role': 'user', 'content': prompt},
                    ],
                    'max_tokens': max_tokens,
                    'temperature': 0.1,  # deterministic
                },
                timeout=8,  # 8s timeout (freemodel can be slow)
            )

            if resp.status_code != 200:
                self._api_failures += 1
                log.warning(f"  ML API FAIL [{self._api_failures}]: HTTP {resp.status_code} — {resp.text[:80]}")
                # Fall back to local model
                return self._local_fallback('BUY', f'API HTTP {resp.status_code}')

            data = resp.json()
            content = data.get('choices', [{}])[0].get('message', {}).get('content', '{}')

            # Track token usage
            usage = data.get('usage', {})
            self._total_tokens_used += usage.get('total_tokens', 0)

            # Parse JSON response
            return self._parse_response(content)

        except requests.Timeout:
            self._api_failures += 1
            log.warning(f"  ML API TIMEOUT [{self._api_failures}] — using local model")
            return self._local_fallback('BUY', 'API timeout')
        except Exception as e:
            self._api_failures += 1
            log.warning(f"  ML API ERROR [{self._api_failures}]: {str(e)[:80]}")
            return self._local_fallback('BUY', f'API: {str(e)[:30]}')

    def _local_fallback(self, default_action: str, reason: str) -> dict:
        """Use local XGBoost/rules model when API fails."""
        if self.local_model is not None:
            # Local model needs features — use minimal defaults for fallback
            result = self.local_model._rules_predict({
                "edge": 0.05, "edge_ratio": 2.0, "n_models": 3,
                "confidence": 0.6, "market_spread_bps": 500,
                "market_price": 0.1,
            })
            result["reason"] = f"local_fallback: {reason}"
            return result
        return {"action": default_action, "confidence": 0.5,
                "reason": f"fallback: {reason}", "source": "fallback"}

    def _parse_response(self, content: str) -> Dict:
        """Parse ML model response (handles various JSON formats)."""
        content = content.strip()
        # Remove markdown code blocks if present
        if content.startswith('```'):
            content = content.split('\n', 1)[-1].rsplit('```', 1)[0].strip()

        try:
            parsed = json.loads(content)

            # Handle array response (for select_markets)
            if isinstance(parsed, list):
                return {'raw': parsed, 'action': 'SELECT', 'confidence': 0.8, 'reason': ''}

            # Normalize keys
            action = parsed.get('action', parsed.get('act', 'BUY')).upper()
            confidence = float(parsed.get('conf', parsed.get('confidence', 0.5)))
            reason = parsed.get('why', parsed.get('reason', ''))

            return {
                'action': action,
                'confidence': min(1.0, max(0.0, confidence)),
                'reason': str(reason)[:50],
            }
        except (json.JSONDecodeError, ValueError, TypeError):
            # Fallback: try to extract action from text
            content_upper = content.upper()
            if 'SKIP' in content_upper or 'NO' in content_upper:
                return {'action': 'SKIP', 'confidence': 0.5, 'reason': 'parsed from text'}
            if 'SELL' in content_upper:
                return {'action': 'SELL', 'confidence': 0.5, 'reason': 'parsed from text'}
            return {'action': 'BUY', 'confidence': 0.5, 'reason': 'parse failed'}

    def get_token_usage(self) -> int:
        """Total tokens used this session."""
        return self._total_tokens_used

    def get_status(self) -> Dict:
        """ML engine status."""
        local_status = self.local_model.get_status() if self.local_model else {"model": "none"}
        return {
            'enabled': self.enabled,
            'model': self.model if self.enabled else 'local',
            'local_model': local_status.get("model", "none"),
            'tokens_used': self._total_tokens_used,
            'api_failures': self._api_failures,
            'cache_size': len(self._cache),
        }
