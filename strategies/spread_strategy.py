"""
Multi-Outcome Spread Strategy — STATION-AWARE, OPTIONS-STYLE.

THE EDGE:
Polymarket resolves to a SPECIFIC airport weather station (e.g., Seoul=RKSI/Incheon).
Most forecast services predict for city CENTER. The airport can differ by 1-3°C.
We forecast the EXACT airport coordinates → better accuracy than the market.

THE STRATEGY (options-style multi-leg spread):
When our ensemble forecast shows a tight probability distribution, we buy
adjacent temperature buckets with Kelly-optimal sizing so that:
  - If ANY of our 2-3 buckets resolves YES → we profit
  - Combined cost < combined P(win) × $1.00 payout

Example: Forecast says 24°C (airport station), P(23°)=25%, P(24°)=40%, P(25°)=20%
  - Buy 23° @ $0.12 (market underpricing the cooler outcome)
  - Buy 24° @ $0.28 (our primary)
  - Buy 25° @ $0.18 (market underpricing warmer)
  Total cost: $0.58, P(any win): 85%, EV = $0.85 - $0.58 = +$0.27 per $1 staked

This replicates what Wallet1 ($58K realized PnL) does: buy cheap tails adjacent
to the most-likely outcome, wait for the market to realize the forecast was right.
"""

from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

from config import Config
from data.probability_engine import BucketProbability
from data.weather_stations import get_station
from logger import log


@dataclass
class SpreadLeg:
    bucket_label: str
    token_id: str
    market_price: float       # what the market is asking (best_ask)
    market_bid: float         # what the market is bidding
    our_probability: float    # our forecast probability for this bucket
    allocation_pct: float
    size_usd: float
    shares: float


@dataclass
class SpreadSignal:
    market_title: str
    primary_bucket: str
    station_name: str          # which airport this resolves to
    legs: List[SpreadLeg]
    total_cost: float
    expected_payout: float
    expected_profit: float
    expected_roi_pct: float    # EV as % of cost
    combined_probability: float  # P(any leg wins)
    edge_pct: float            # market mispricing %
    confidence: float
    reason: str


class SpreadStrategy:
    """
    Multi-outcome spread: buy 2-4 adjacent buckets weighted by ensemble probability.
    Only enters when combined cost < expected payout (positive EV).
    Uses STATION-AWARE forecasting (airport coordinates, not city center).
    """

    def __init__(self):
        self.enabled = Config.SPREAD_ENABLED
        self.min_edge = Config.MIN_EDGE_TO_ENTER  # minimum EV edge
        self.max_legs = 4
        self.primary_weight = 0.50   # primary bucket gets 50% of budget
        self.neighbor_weight = 0.30  # each ±1 neighbor gets up to 30%
        self.far_weight = 0.15       # ±2 gets up to 15%

    def evaluate(
        self,
        market_title: str,
        bucket_probs: List[BucketProbability],
        market_prices: Dict[str, float],
        token_ids: Dict[str, str],
        balance: float,
        market_bids: Dict[str, float] = None,
        city: str = "",
    ) -> List[SpreadSignal]:
        if not self.enabled:
            return []

        # Default bids to asks if not provided (conservative — assumes we pay ask)
        if market_bids is None:
            market_bids = market_prices

        # Station info for logging
        station = get_station(city) if city else None
        station_label = f"{station.icao} ({station.station_name})" if station else city

        # Sort by temperature
        sorted_bk = sorted(bucket_probs, key=lambda b: b.bucket_low)
        if len(sorted_bk) < 3:
            return []

        # Find primary (highest forecast probability)
        primary = max(sorted_bk, key=lambda b: b.probability)
        if primary.probability < 0.20:
            return []  # no strong signal

        primary_idx = sorted_bk.index(primary)

        # Budget
        budget = min(Config.SPREAD_MAX_COST, balance * Config.MAX_BET_PCT)
        if budget < 0.50:
            return []

        # ── Build candidate legs (primary + neighbors) ──
        candidates: List[Tuple[int, float]] = []  # (index, alloc_weight)
        candidates.append((primary_idx, self.primary_weight))

        # ±1 neighbors
        if primary_idx - 1 >= 0:
            nb = sorted_bk[primary_idx - 1]
            if nb.probability > 0.05:  # at least 5% chance
                candidates.append((primary_idx - 1, self.neighbor_weight))
        if primary_idx + 1 < len(sorted_bk):
            nb = sorted_bk[primary_idx + 1]
            if nb.probability > 0.05:
                candidates.append((primary_idx + 1, self.neighbor_weight))

        # ±2 neighbors (only if strong secondary signal)
        if primary_idx - 2 >= 0:
            nb = sorted_bk[primary_idx - 2]
            if nb.probability > 0.08:
                candidates.append((primary_idx - 2, self.far_weight))
        if primary_idx + 2 < len(sorted_bk):
            nb = sorted_bk[primary_idx + 2]
            if nb.probability > 0.08:
                candidates.append((primary_idx + 2, self.far_weight))

        if len(candidates) < 2:
            return []

        # ── Build legs with market prices ──
        legs = []
        total_weight = sum(w for _, w in candidates)
        total_cost = 0.0
        combined_prob = 0.0

        for idx, weight in candidates:
            bk = sorted_bk[idx]
            label = bk.bucket_label
            ask = market_prices.get(label, 0.99)  # what we pay (best_ask)
            bid = market_bids.get(label, 0.0)
            tid = token_ids.get(label)

            if not tid:
                continue
            if ask > 0.85:  # too expensive — market is already certain
                continue
            if ask <= 0:
                continue

            alloc = weight / total_weight
            size = budget * alloc
            if size < 0.10:
                continue

            shares = size / ask if ask > 0 else 0

            legs.append(SpreadLeg(
                bucket_label=label,
                token_id=tid,
                market_price=ask,
                market_bid=bid,
                our_probability=bk.probability,
                allocation_pct=alloc,
                size_usd=size,
                shares=shares,
            ))
            total_cost += size
            combined_prob += bk.probability

        if len(legs) < 2:
            return []

        combined_prob = min(0.99, combined_prob)

        # ── EV calculation ──
        # Each leg: if it wins, we get $1.00 per share × shares = size_usd / price
        # Expected payout across all legs
        expected_payout = sum(
            leg.our_probability * (leg.size_usd / max(leg.market_price, 0.01))
            for leg in legs
        )
        # But only one bucket can win, so cap at max single-leg payout
        max_single_payout = max(
            leg.size_usd / max(leg.market_price, 0.01) for leg in legs
        )
        expected_payout = min(expected_payout, combined_prob * max_single_payout)

        expected_profit = expected_payout - total_cost
        expected_roi = (expected_profit / total_cost * 100) if total_cost > 0 else 0

        if expected_profit <= 0:
            return []

        # Edge = how much the market is mispricing
        # Market-implied probability = sum of bucket prices
        # Our probability = combined_prob
        market_implied = sum(
            leg.market_price for leg in legs
        ) / len(legs)  # avg market probability
        edge_pct = (combined_prob - market_implied) * 100

        if edge_pct < self.min_edge * 100:
            return []

        confidence = min(0.90, primary.confidence * (combined_prob / 0.80))

        reason = (
            f"Spread at {station_label} | "
            f"Forecast={primary.mean_forecast:.0f}C | "
            f"{len(legs)} legs | P(win)={combined_prob:.0%} | "
            f"Cost=${total_cost:.2f} | EV=${expected_payout:.2f} | "
            f"ROI={expected_roi:+.0f}% | Edge={edge_pct:+.0f}%"
        )

        log.info(f"  SPREAD: {reason}")
        return [SpreadSignal(
            market_title=market_title,
            primary_bucket=primary.bucket_label,
            station_name=station_label,
            legs=legs,
            total_cost=total_cost,
            expected_payout=expected_payout,
            expected_profit=expected_profit,
            expected_roi_pct=expected_roi,
            combined_probability=combined_prob,
            edge_pct=edge_pct,
            confidence=confidence,
            reason=reason,
        )]
