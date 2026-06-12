"""
PEAK-CLUSTER STRATEGY — parallel adjacent-bucket basket around the estimated peak.

Runs ALONGSIDE the other strategies (does not disturb them). It estimates the
peak bucket (the highest model-probability bucket), then buys a basket of the
ADJACENT buckets around it (e.g. 28,29,30,31,32) whose COMBINED per-share cost
is < PEAK_CLUSTER_MAX_COST (< $1 after fees).

Why it works: the buckets are mutually exclusive, so exactly ONE can win and pay
$1. If we buy EQUAL SHARES across the basket and the combined per-share cost is
below $1, then ANY single winning leg returns more than the whole basket cost —
i.e. any-one-wins => net profit after fees. This captures early / mispriced
markets where the model is confident about the neighbourhood of the peak but not
the exact bucket.

Hold-to-resolution by nature: the payoff is realised at settlement, so legs are
placed with hold_hint=True (they bypass the early-exit sellability floor).
"""

from dataclasses import dataclass
from typing import List

from config import Config
from logger import log


@dataclass
class ClusterLeg:
    bucket_label: str
    token_id: str
    price: float
    prob: float
    size_usd: float


@dataclass
class PeakClusterSignal:
    market_title: str
    legs: List[ClusterLeg]
    total_cost: float
    combined_prob: float
    expected_roi_pct: float
    n_legs: int
    reason: str


class PeakClusterStrategy:
    name = "peak_cluster"
    description = (
        "Parallel adjacent-bucket basket around the estimated peak; combined "
        "per-share cost < $1 so any single winning leg profits after fees."
    )

    def __init__(self):
        self._load_cfg()

    def _load_cfg(self):
        g = lambda n, d: getattr(Config, n, d)
        self.span = int(g('PEAK_CLUSTER_SPAN', 2))
        self.max_cost = float(g('PEAK_CLUSTER_MAX_COST', g('BASKET_MAX_COST', 0.85)))
        self.min_legs = int(g('PEAK_CLUSTER_MIN_LEGS', 2))
        self.max_legs = int(g('PEAK_CLUSTER_MAX_LEGS', 5))
        self.min_edge = float(g('PEAK_CLUSTER_MIN_EDGE', 0.03))
        self.min_conf = float(g('PEAK_CLUSTER_MIN_CONF', 0.55))
        self.max_center_price = float(g('PEAK_CLUSTER_MAX_CENTER_PRICE', 0.85))
        self.base_fraction = float(g('PEAK_CLUSTER_BASE_FRACTION', 0.05))
        self.max_fraction = float(g('PEAK_CLUSTER_MAX_FRACTION', 0.20))
        self.max_usd = float(g('PEAK_CLUSTER_MAX_USD', 15.0))
        self.abs_floor = float(g('ABS_PRICE_FLOOR', 0.01))

    def evaluate(self, market_title, bucket_probs, market_prices, token_ids,
                 balance, city="", grade=0.6) -> List[PeakClusterSignal]:
        self._load_cfg()  # pick up live /settings overrides
        if not bucket_probs or balance <= 0:
            return []

        # Build an ordered list of buckets (assume market/temperature order).
        items = []
        for bp in bucket_probs:
            label = bp.bucket_label
            price = market_prices.get(label)
            if price is None:
                continue
            items.append({
                'label': label,
                'prob': float(bp.probability),
                'price': float(price or 0.0),
                'token': token_ids.get(label, ''),
                'conf': float(getattr(bp, 'confidence', 0.0) or 0.0),
            })
        if len(items) < self.min_legs:
            return []

        # Center = estimated peak (highest-probability bucket).
        ci = max(range(len(items)), key=lambda i: items[i]['prob'])
        center = items[ci]
        if center['conf'] < self.min_conf:
            return []
        if center['price'] <= 0 or center['price'] > self.max_center_price:
            return []

        lo = max(0, ci - self.span)
        hi = min(len(items) - 1, ci + self.span)
        window = [it for it in items[lo:hi + 1]
                  if it['token'] and it['price'] >= self.abs_floor]
        if len(window) < self.min_legs:
            return []

        # Greedily add the highest-probability neighbours while the combined
        # per-share cost stays under the max-cost budget.
        window_sorted = sorted(window, key=lambda x: -x['prob'])
        chosen = []
        cost = 0.0
        pr = 0.0
        for it in window_sorted:
            if len(chosen) >= self.max_legs:
                break
            if cost + it['price'] <= self.max_cost:
                chosen.append(it)
                cost += it['price']
                pr += it['prob']
        if len(chosen) < self.min_legs or cost <= 0:
            return []

        combined_prob = min(1.0, pr)
        edge = combined_prob - cost
        if edge < self.min_edge:
            return []

        # Equal SHARES across legs => any single win pays $1 > per-share cost.
        budget = min(balance * self.base_fraction * (0.5 + max(0.0, grade)),
                     balance * self.max_fraction,
                     self.max_usd)
        if budget < 1.0:
            return []
        shares = budget / cost

        legs = []
        for it in sorted(chosen, key=lambda x: x['price']):
            leg_size = round(shares * it['price'], 4)
            if leg_size <= 0:
                continue
            legs.append(ClusterLeg(it['label'], it['token'], it['price'],
                                   it['prob'], leg_size))
        if len(legs) < self.min_legs:
            return []

        roi = (1.0 - cost) / cost * 100.0  # ROI when exactly one leg wins
        reason = (
            f"peak-cluster @ {center['label']}: {len(legs)} legs cost ${cost:.2f} "
            f"P(any)={combined_prob:.0%} edge={edge:+.0%} "
            f"(any win -> +{roi:.0f}% after fees)"
        )
        return [PeakClusterSignal(
            market_title=market_title,
            legs=legs,
            total_cost=round(cost, 4),
            combined_prob=combined_prob,
            expected_roi_pct=roi,
            n_legs=len(legs),
            reason=reason,
        )]
