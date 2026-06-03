"""
QUICK-FLIP STRATEGY — Forecast Change Detection + Fast Exit.

HOW THE 70-80% WIN RATE WALLETS ACTUALLY WORK:

They don't buy lottery tickets and pray. They detect when new weather model
data becomes available (ECMWF at 00/12 UTC, GFS every 6h, HRRR hourly), and
they trade BEFORE the Polymarket crowd digests the update.

The pattern:
  1. ECMWF 12Z run publishes at ~12:15 UTC
  2. Our bot fetches the new forecast at 12:16 UTC
  3. Detects: Seoul max temp forecast changed from 23C to 25C
  4. Buys the 25C bucket at the OLD price (market hasn't updated yet)
  5. 15-60 minutes later, market digests the new info -> price moves from 30c to 50c
  6. Bot sells at 50c for quick 67% profit (or holds if edge is massive)

The win rate is high (70-80%) because:
  - You're trading on NEW INFORMATION that the market hasn't priced in yet
  - The direction is correct most of the time (forecast updates are REAL data)
  - You exit quickly, capturing the information arbitrage

MULTI-OUTCOME SPREAD ARBITRAGE:
  Weather markets have 11 buckets that should sum to ~$1.00. When the sum
  deviates significantly (e.g., $0.78 or $1.29), there's mispricing.
  Buy the underpriced cluster, sell/hedge the overpriced ones.

FORECAST UPDATE SCHEDULE (when to poll):
  ECMWF:     00:15, 12:15 UTC (main), 06:15, 18:15 (ensemble)
  GFS:       00:30, 06:30, 12:30, 18:30 UTC
  HRRR:      Every hour at :15
  ICON:      00:45, 03:45, 06:45, 09:45, 12:45, 15:45, 18:45, 21:45 UTC
  JMA:       00:30, 06:30, 12:30, 18:30 UTC (Asia-focused)
"""

import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

from config import Config
from data.weather_stations import get_station
from logger import log


# ── Forecast update times (UTC) ──
FORECAST_UPDATE_SCHEDULE = {
    "ECMWF": [0, 12],           # hours after which updates publish (~15 min delay)
    "GFS": [0, 6, 12, 18],
    "HRRR": list(range(24)),     # every hour
    "ICON": [0, 3, 6, 9, 12, 15, 18, 21],
    "JMA": [0, 6, 12, 18],
    "GEM": [0, 12],
}


def minutes_since_last_update() -> Tuple[str, int]:
    """
    Returns (model_name, minutes_since_update) for the most recent forecast update.
    If we're within 15 minutes of an update, that's an ACTIONABLE window.
    """
    now = datetime.now(timezone.utc)
    best_model = "none"
    best_minutes = 999

    for model, hours in FORECAST_UPDATE_SCHEDULE.items():
        for h in hours:
            update_time = now.replace(hour=h, minute=15, second=0, microsecond=0)
            if update_time > now:
                update_time -= timedelta(hours=24)
            diff = (now - update_time).total_seconds() / 60
            if 0 <= diff < best_minutes:
                best_minutes = diff
                best_model = model

    return best_model, int(best_minutes)


@dataclass
class ForecastChange:
    """Detected change in forecast between two polls."""
    city: str
    station_icao: str
    market_type: str          # 'highest' or 'lowest'
    old_forecast_c: float     # previous ensemble mean temperature
    new_forecast_c: float     # new ensemble mean temperature
    delta_c: float            # change in forecast
    old_primary_bucket: str   # what the old forecast said
    new_primary_bucket: str   # what the new forecast says
    affected_buckets: List[str]  # buckets that changed probability significantly
    timestamp: datetime


@dataclass
class QuickFlipSignal:
    """A rapid-entry, rapid-exit trade signal."""
    market_title: str
    bucket_label: str
    token_id: str
    direction: str            # 'BUY' (forecast now favors this bucket more)
    entry_price: float        # best_ask (what we pay)
    target_price: float       # where we expect to sell
    entry_reason: str
    forecast_change: Optional[ForecastChange]
    confidence: float
    expected_hold_minutes: int
    expected_roi_pct: float
    size_usd: float
    shares: float


class QuickFlipStrategy:
    """
    Detect forecast changes and enter BEFORE the market adjusts.
    Target: 5-30% profit per flip, 70-80% win rate, liquid markets only.
    """

    name = "quick_flip"
    description = (
        "Forecast-change arbitrage: enter when new weather model data arrives, "
        "exit when the market prices it in. High win rate (70-80%), quick exit "
        "(15-60 min), multi-outcome spread aware."
    )

    def __init__(self):
        self._last_forecasts: Dict[str, dict] = {}  # city -> last forecast snapshot
        self._last_poll_time: Optional[datetime] = None
        self.min_delta_c = 1.0        # minimum 1C forecast change to trigger
        self.min_confidence = 0.65    # need strong ensemble agreement
        self.max_entry_price = 0.85   # don't buy near-certain outcomes
        self.max_hold_minutes = 120   # exit within 2 hours
        self.target_roi_pct = 15.0    # target 15% profit per flip

    def should_poll_forecasts(self) -> bool:
        """Check if we're near a forecast update time (within 15 min)."""
        model, minutes = minutes_since_last_update()
        if minutes < 15:
            log.info(f"  FORECAST UPDATE: {model} updated {minutes}m ago — ACTIONABLE WINDOW")
            return True
        return False

    def detect_changes(
        self,
        city: str,
        market_type: str,
        bucket_probs: list,
        current_time: datetime,
    ) -> Optional[ForecastChange]:
        """
        Compare current ensemble forecast with the last polled forecast.
        Returns a ForecastChange if the forecast shifted significantly.
        """
        key = f"{city}_{market_type}"
        primary = max(bucket_probs, key=lambda b: b.probability) if bucket_probs else None
        if not primary:
            return None

        station = get_station(city)
        station_icao = station.icao if station else "???"
        current_mean = primary.mean_forecast
        current_primary_label = primary.bucket_label

        if key in self._last_forecasts:
            prev = self._last_forecasts[key]
            old_mean = prev["mean_temp"]
            old_label = prev["primary_label"]
            delta = abs(current_mean - old_mean)

            if delta >= self.min_delta_c:
                # Find affected buckets (those whose probability changed)
                affected = []
                for bp in bucket_probs:
                    old_prob = prev.get("probs", {}).get(bp.bucket_label, 0)
                    if abs(bp.probability - old_prob) > 0.05:  # 5pp change
                        affected.append(bp.bucket_label)

                change = ForecastChange(
                    city=city,
                    station_icao=station_icao,
                    market_type=market_type,
                    old_forecast_c=old_mean,
                    new_forecast_c=current_mean,
                    delta_c=delta,
                    old_primary_bucket=old_label,
                    new_primary_bucket=current_primary_label,
                    affected_buckets=affected if affected else [current_primary_label],
                    timestamp=current_time,
                )
                return change

        # Store current snapshot
        probs = {bp.bucket_label: bp.probability for bp in bucket_probs}
        self._last_forecasts[key] = {
            "mean_temp": current_mean,
            "primary_label": current_primary_label,
            "probs": probs,
            "timestamp": current_time,
        }
        return None

    def evaluate(
        self,
        market_title: str,
        bucket_probs: list,
        market_prices: dict,
        market_bids: dict,
        token_ids: dict,
        balance: float,
        city: str = "",
        market_type: str = "highest",
    ) -> List[QuickFlipSignal]:
        """
        Find quick-flip opportunities after forecast changes.
        Only enters when a forecast change is detected AND prices haven't adjusted.
        """
        signals = []
        now = datetime.now(timezone.utc)

        # Detect forecast changes
        change = self.detect_changes(city, market_type, bucket_probs, now)
        if not change:
            return []

        log.info(f"  CHANGE DETECTED: {city} {market_type} forecast shifted "
                 f"{change.delta_c:.1f}C: {change.old_primary_bucket} -> "
                 f"{change.new_primary_bucket} ({change.station_icao})")

        # For each affected bucket, check if the price is still at the OLD level
        for label in change.affected_buckets:
            bp = next((b for b in bucket_probs if b.bucket_label == label), None)
            if not bp:
                continue

            market_price = market_prices.get(label, 0.99)
            market_bid = market_bids.get(label, market_price)
            token_id = token_ids.get(label)

            if not token_id:
                continue
            if market_price > self.max_entry_price:
                continue
            if market_price < 0.03:  # too thin for quick flip
                continue

            our_prob = bp.probability
            edge = our_prob - market_price

            # Quick-flip edge: we think it should be at target_price based on new forecast
            # If market hasn't moved yet, we have an arbitrage window
            target_price = min(0.95, our_prob * 0.9)  # conservative target

            if target_price <= market_price * 1.05:
                continue  # not enough room for profit after spread

            expected_roi = (target_price - market_price) / market_price * 100
            if expected_roi < self.target_roi_pct:
                continue

            # Size: 5-15% of balance per flip
            size_usd = min(balance * 0.10, 20.0)
            if size_usd < 1.0:
                continue

            shares = size_usd / market_price if market_price > 0 else 0
            hold_minutes = min(self.max_hold_minutes, 30 + int(abs(change.delta_c) * 10))

            signal = QuickFlipSignal(
                market_title=market_title,
                bucket_label=label,
                token_id=token_id,
                direction="BUY",
                entry_price=market_price,
                target_price=target_price,
                entry_reason=(
                    f"FLIP: {change.station_icao} forecast shifted {change.delta_c:.1f}C "
                    f"({change.old_primary_bucket}->{change.new_primary_bucket}). "
                    f"Buy {label} at {market_price:.3f} before market adjusts. "
                    f"Exit at {target_price:.3f} ({expected_roi:.0f}% ROI)"
                ),
                forecast_change=change,
                confidence=bp.confidence,
                expected_hold_minutes=hold_minutes,
                expected_roi_pct=expected_roi,
                size_usd=size_usd,
                shares=shares,
            )
            signals.append(signal)

        return signals


# ── MULTI-OUTCOME SPREAD DETECTOR ──

def find_spread_arbitrage(
    bucket_probs: list,
    market_prices: dict,
    token_ids: dict,
    balance: float,
) -> List[dict]:
    """
    Find multi-outcome spread mispricing.

    In an 11-bucket market, the sum of all bucket prices should be ~$1.00
    (since exactly one bucket will resolve to $1.00). When the sum deviates,
    there's an arbitrage or directional edge.

    If sum < $0.85: the market is UNDERPRICING outcomes -> buy the cluster
    If sum > $1.15: the market is OVERPRICING -> sell/avoid

    Strategy: buy the 2-3 buckets where our probability most exceeds
    the market price, provided the total cost is reasonable.
    """
    opportunities = []

    # Calculate market sum
    prices_list = [market_prices.get(bp.bucket_label, 0) for bp in bucket_probs]
    market_sum = sum(p for p in prices_list if p > 0)
    n_buckets = len([p for p in prices_list if p > 0])

    if market_sum <= 0:
        return []

    # Find the most underpriced buckets relative to our probability
    mispriced = []
    for bp in bucket_probs:
        mp = market_prices.get(bp.bucket_label, 0.99)
        if mp <= 0 or bp.probability <= 0:
            continue
        edge_ratio = bp.probability / max(mp, 0.01)
        edge = bp.probability - mp
        mispriced.append((bp, mp, edge, edge_ratio))

    # Sort by edge ratio (best first)
    mispriced.sort(key=lambda x: x[3], reverse=True)

    # If market_sum is significantly below 1.0, there's systematic underpricing
    if market_sum < 0.85 and len(mispriced) >= 2:
        # Buy the top 2-3 underpriced buckets
        best = mispriced[:3]
        total_cost = sum(m[1] for m in best)
        total_prob = sum(m[0].probability for m in best)

        if total_cost < total_prob:  # positive EV cluster
            total_roi = (total_prob - total_cost) / total_cost * 100
            opportunities.append({
                "type": "cluster_underpriced",
                "market_sum": market_sum,
                "buckets": [(m[0].bucket_label, m[1], m[0].probability) for m in best],
                "total_cost": total_cost,
                "total_probability": total_prob,
                "expected_roi_pct": total_roi,
                "edge_ratio": total_prob / max(total_cost, 0.01),
            })

    return opportunities
