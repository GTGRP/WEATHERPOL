"""
PEAKER STRATEGY — unified, high-confidence daily-peak play.

This MERGES the old `safety_peak` + `peak_basket` "peak" logic into ONE strategy
so the bot never buys the same peak bucket twice as a duplicate. It accurately
estimates the daily highest-temperature bucket and then takes one of three
FOCUSED shapes (the 4th — buying BOTH shoulders at once — is intentionally left
to `peak_cluster`, the wide any-one-wins basket):

  sub-strategy            shape                         when
  ----------------------  ----------------------------  ---------------------------
  peaker                  1 leg  (peak only)            STABLE + very high confidence
  peaker_basket_warmer    2 legs (peak + upper +1)      WARMING trend
  peaker_basket_cooler    2 legs (peak + lower -1)      COOLING trend / default lean

CALIBRATION (data-driven, Req-27):
Live results showed the COOL neighbour (-1 bucket) is the big winner while the
bare peak and the warm (+1) neighbour lose — i.e. real daily highs tend to land
ABOUT ONE BUCKET BELOW the model's estimated peak. So this strategy:
  * shifts the estimated peak DOWN by PEAKER_PEAK_BIAS_BUCKETS buckets,
  * DEFAULTS the stable/ambiguous case to the COOLER basket (not a mean-lean),
  * gives the cooler basket looser gates + a size multiplier (PEAKER_COOL_*),
  * only does the bare 1-leg `peaker` when confidence is VERY high AND the lone
    peak already clears the fee-aware profit floor by itself.

GUARANTEES:
  * equal SHARES across legs → whichever single bucket resolves to $1 covers the
    other leg + net profit after fees (buckets are mutually exclusive),
  * combined per-share basket cost < PEAKER_MAX_COST (default 0.95),
  * every leg clears the dust / sellability floor,
  * HOLD to resolution (thin books make early exits losing).

Patient by design: returning [] (no trade) is the correct output most of the time.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from config import Config
from logger import log
# These two are used ONLY as type hints below. Import defensively so a class
# rename in those modules can never crash the bot on startup.
try:
    from data.probability_engine import BucketProbability
except Exception:  # pragma: no cover
    BucketProbability = object  # type: ignore
try:
    from data.stability import StabilityReport
except Exception:  # pragma: no cover
    StabilityReport = object  # type: ignore


# ── dataclasses ──

@dataclass
class PeakerLeg:
    bucket_label: str
    token_id: str
    market_price: float        # best_ask (what we pay before maker re-price)
    our_probability: float     # ensemble probability for this bucket
    size_usd: float            # how much to allocate
    role: str                  # 'peak' | 'neighbor_warm' | 'neighbor_cool'
    offset: int                # 0 = peak, +1 = warmer, -1 = cooler


@dataclass
class PeakerSignal:
    market_title: str
    city: str
    forecast_max_c: float
    trend: str                 # 'warming' | 'cooling' | 'stable' | 'sideways'
    stability_score: float
    confidence: float
    legs: List[PeakerLeg] = field(default_factory=list)
    total_cost: float = 0.0    # per-share basket cost (sum of leg prices)
    basket_usd: float = 0.0    # total $ deployed across legs
    combined_prob: float = 0.0
    n_models: int = 0
    expected_roi_pct: float = 0.0
    sub_strategy: str = 'peaker'        # 'peaker' | 'peaker_basket_warmer' | 'peaker_basket_cooler'
    direction: str = 'stable'
    hold_hint: bool = True
    exit_hint: str = ''
    reason: str = ''


# ── helpers ──

_NUM_RE = re.compile(r'(-?\d+(?:\.\d+)?)')


def _bucket_numeric_center(label: str, lo: float, hi: float) -> Optional[float]:
    """Best-effort numeric center of a bucket label for adjacency math."""
    if lo != float('-inf') and hi != float('inf'):
        return (lo + hi) / 2.0
    m = _NUM_RE.search(label or '')
    if m:
        return float(m.group(1))
    if lo != float('-inf'):
        return lo
    if hi != float('inf'):
        return hi
    return None


# ── the strategy ──

class PeakerStrategy:
    """
    Unified high-confidence peak estimator with three focused shapes
    (peaker / warmer / cooler). The both-shoulders shape is delegated to
    peak_cluster. Calibrated to the proven cool-neighbour edge.
    """

    name = "peaker"
    description = (
        "Unified high-confidence daily-peak play: accurately estimate the peak "
        "bucket, then buy peak-only (stable) or peak + one directional neighbour "
        "(warmer/cooler). Equal shares, any winner covers the basket + profit "
        "after fees. Calibrated to the winning cool side. Hold to resolution."
    )

    def __init__(self):
        self._load_cfg()

    def _load_cfg(self):
        g = lambda n, d: getattr(Config, n, d)
        self.enabled = bool(g('PEAKER_ENABLED', 1))
        self.min_grade = float(g('PEAKER_MIN_GRADE', 0.60))
        self.min_models = int(g('PEAKER_MIN_MODELS', 3))
        self.max_std = float(g('PEAKER_MAX_STD', 1.4))
        self.min_conf = float(g('PEAKER_MIN_CONFIDENCE', 0.62))
        # confidence needed to take the bare 1-leg stable play
        self.solo_min_conf = float(g('PEAKER_SOLO_MIN_CONFIDENCE', 0.80))
        # how many buckets to shift the estimated peak DOWN (hot-bias correction)
        self.peak_bias = int(g('PEAKER_PEAK_BIAS_BUCKETS', 1))
        self.max_peak_price = float(g('PEAKER_MAX_PEAK_PRICE', 0.85))
        self.max_nb_price = float(g('PEAKER_MAX_NEIGHBOR_PRICE', 0.60))
        self.max_cost = float(g('PEAKER_MAX_COST', 0.95))
        self.fee_buffer = float(g('PEAKER_FEE_BUFFER', 0.02))
        self.min_net = float(g('PEAKER_MIN_NET_PROFIT', 0.03))
        self.min_edge = float(g('PEAKER_MIN_EDGE', 0.04))
        self.base_fraction = float(g('PEAKER_BASE_FRACTION', 0.05))
        self.max_fraction = float(g('PEAKER_MAX_FRACTION', 0.20))
        self.max_usd = float(g('PEAKER_MAX_USD', 15.0))
        # cool-side calibration: prefer cooler when ambiguous, size it up, and
        # relax its edge gate a touch because it is the proven winner.
        self.prefer_cool = bool(g('PEAKER_PREFER_COOL', 1))
        self.cool_size_mult = float(g('PEAKER_COOL_SIZE_MULT', 1.35))
        self.cool_edge_relax = float(g('PEAKER_COOL_EDGE_RELAX', 0.02))
        self.warm_size_mult = float(g('PEAKER_WARM_SIZE_MULT', 0.7))
        self.min_entry = float(g('MIN_ENTRY_PRICE', 0.02))
        self.min_order = float(g('MIN_ORDER_SIZE', 1.0))

    def evaluate(
        self,
        market_title: str,
        bucket_probs: List[BucketProbability],
        market_prices: Dict[str, float],
        token_ids: Dict[str, str],
        balance: float,
        city: str,
        stability: Optional[StabilityReport] = None,
        grade: float = 0.60,
    ) -> List[PeakerSignal]:
        self._load_cfg()  # pick up live /settings overrides
        if not self.enabled or not bucket_probs or balance <= 0:
            return []

        # ── gate 1: stability / grade ──
        if stability is None:
            log.debug(f"Peaker {city}: no stability report -- patient skip")
            return []
        eff_grade = min(grade, stability.score)
        if not stability.predictable and stability.score < self.min_grade:
            log.debug(f"Peaker {city}: grade {stability.score:.2f} < {self.min_grade} -- skip")
            return []
        if eff_grade < self.min_grade:
            log.debug(f"Peaker {city}: eff grade {eff_grade:.2f} < {self.min_grade} -- skip")
            return []

        # ── gate 2: enough agreeing models ──
        n_models = max(bp.n_models for bp in bucket_probs)
        if n_models < self.min_models:
            log.debug(f"Peaker {city}: {n_models} models < {self.min_models} -- skip")
            return []

        # ── gate 3: tight ensemble spread ──
        ens_std = min((bp.std_forecast for bp in bucket_probs), default=999)
        if ens_std > self.max_std:
            log.debug(f"Peaker {city}: std {ens_std:.2f}C > {self.max_std} -- skip")
            return []

        # ── index buckets by numeric center ──
        indexed: List[Tuple[float, BucketProbability]] = []
        for bp in bucket_probs:
            c = _bucket_numeric_center(bp.bucket_label, bp.bucket_low, bp.bucket_high)
            if c is not None:
                indexed.append((c, bp))
        if not indexed:
            return []
        indexed.sort(key=lambda x: x[0])

        # ── estimate peak (closest to forecast max), then apply the DOWNWARD
        #    hot-bias correction so the bare peak sits where highs actually land ──
        target = stability.forecast_max_c
        raw_i = min(range(len(indexed)), key=lambda i: abs(indexed[i][0] - target))
        center_i = max(0, raw_i - self.peak_bias)
        center_val, center_bp = indexed[center_i]

        # ── gate 4: peak-bucket confidence ──
        peak_conf = float(getattr(center_bp, 'confidence', 0.0) or 0.0)
        if peak_conf < self.min_conf:
            log.debug(f"Peaker {city}: peak conf {peak_conf:.2f} < {self.min_conf} -- skip")
            return []

        # ── decide the shape (sub-strategy) ──
        trend = (stability.trend or 'unknown').lower()
        has_warm = (center_i + 1) < len(indexed)
        has_cool = (center_i - 1) >= 0

        if trend == 'warming' and has_warm:
            direction, sub = +1, 'peaker_basket_warmer'
        elif trend == 'cooling' and has_cool:
            direction, sub = -1, 'peaker_basket_cooler'
        else:
            # stable / sideways / ambiguous: default to the proven cool side
            if self.prefer_cool and has_cool:
                direction, sub = -1, 'peaker_basket_cooler'
            elif peak_conf >= self.solo_min_conf:
                direction, sub = 0, 'peaker'              # bare 1-leg stable play
            elif has_cool:
                direction, sub = -1, 'peaker_basket_cooler'
            elif has_warm:
                direction, sub = +1, 'peaker_basket_warmer'
            else:
                direction, sub = 0, 'peaker'

        # bare 1-leg only when confidence is VERY high; otherwise add the cooler
        if direction == 0 and peak_conf < self.solo_min_conf:
            if has_cool:
                direction, sub = -1, 'peaker_basket_cooler'
            elif has_warm:
                direction, sub = +1, 'peaker_basket_warmer'

        # ── assemble candidate legs ──
        candidates: List[Tuple[int, BucketProbability, str]] = [(center_i, center_bp, 'peak')]
        if direction != 0:
            ni = center_i + direction
            if 0 <= ni < len(indexed):
                _, nb = indexed[ni]
                role = 'neighbor_warm' if direction > 0 else 'neighbor_cool'
                candidates.append((ni, nb, role))

        # ── price & floor checks ──
        kept: List[Tuple[int, BucketProbability, str, float, str]] = []
        for idx, bp, role in candidates:
            price = market_prices.get(bp.bucket_label)
            tid = token_ids.get(bp.bucket_label)
            if price is None or tid is None or price <= 0:
                continue
            if price < self.min_entry:
                continue
            if role == 'peak' and price > self.max_peak_price:
                continue
            if role != 'peak' and price > self.max_nb_price:
                continue
            kept.append((idx, bp, role, price, tid))

        if not any(role == 'peak' for _, _, role, _, _ in kept):
            log.debug(f"Peaker {city}: peak bucket failed price/floor checks -- skip")
            return []
        # if the neighbour fell out, collapse to the bare peak (only if it can
        # stand alone profitably; checked by the fee-aware floor below)
        if len(kept) == 1:
            sub = 'peaker'
            direction = 0

        # ── per-share basket cost & fee-aware profit floor (any-one-wins) ──
        total_cost = sum(price for _, _, _, price, _ in kept)
        if total_cost <= 0:
            return []
        max_basket_cost = min(self.max_cost, 1.0 - (self.fee_buffer + self.min_net))
        if total_cost >= max_basket_cost:
            log.debug(f"Peaker {city}: cost ${total_cost:.2f}/sh >= ${max_basket_cost:.2f} -- skip")
            return []

        # ── combined probability / edge (cool side gets a relaxed gate) ──
        combined_prob = min(0.99, sum(bp.probability for _, bp, _, _, _ in kept))
        edge = combined_prob - total_cost
        eff_min_edge = self.min_edge
        if sub == 'peaker_basket_cooler':
            eff_min_edge = max(0.0, self.min_edge - self.cool_edge_relax)
        if edge < eff_min_edge:
            log.debug(f"Peaker {city}: edge {edge:.1%} < {eff_min_edge:.1%} -- skip")
            return []

        # ── confidence-scaled sizing, with cool/warm multipliers ──
        conf_span = max(0.0, min(1.0, (peak_conf - self.min_conf) / max(0.01, 1.0 - self.min_conf)))
        frac = self.base_fraction + (self.max_fraction - self.base_fraction) * conf_span
        frac = max(self.base_fraction, min(self.max_fraction, frac))
        size_mult = 1.0
        if sub == 'peaker_basket_cooler':
            size_mult = self.cool_size_mult
        elif sub == 'peaker_basket_warmer':
            size_mult = self.warm_size_mult
        basket_usd = balance * frac * size_mult
        basket_usd = min(basket_usd, self.max_usd, balance * self.max_fraction)
        basket_usd = max(self.min_order * len(kept), basket_usd)

        # ── equal SHARES across legs ──
        cost_per_share = total_cost
        target_shares = basket_usd / cost_per_share if cost_per_share > 0 else 0.0
        max_price = max(price for _, _, _, price, _ in kept)
        min_shares_for_floor = self.min_order / max_price if max_price > 0 else 0.0
        shares = max(min_shares_for_floor, target_shares)

        legs: List[PeakerLeg] = []
        for idx, bp, role, price, tid in kept:
            leg_usd = max(self.min_order, round(shares * price, 2))
            offset = 0 if role == 'peak' else (+1 if role == 'neighbor_warm' else -1)
            legs.append(PeakerLeg(
                bucket_label=bp.bucket_label,
                token_id=tid,
                market_price=price,
                our_probability=bp.probability,
                size_usd=leg_usd,
                role=role,
                offset=offset,
            ))

        total_deployed = round(sum(l.size_usd for l in legs), 2)
        expected_roi_pct = ((1.0 - cost_per_share) / cost_per_share * 100.0) if cost_per_share > 0 else 0.0

        peak_label = next(l.bucket_label for l in legs if l.role == 'peak')
        nb = [l for l in legs if l.role != 'peak']
        if not nb:
            shape = 'peaker (peak-only)'
        elif nb[0].role == 'neighbor_cool':
            shape = f'peaker basket cooler (peak {peak_label} + cooler {nb[0].bucket_label})'
        else:
            shape = f'peaker basket warmer (peak {peak_label} + warmer {nb[0].bucket_label})'

        exit_hint = "HOLD -- high-confidence peaker, let it resolve"
        reason = (
            f"PEAKER {city} [{sub}] trend={trend} grade={eff_grade:.2f} conf={peak_conf:.0%} "
            f"std={ens_std:.2f}C | {shape} | {len(legs)}legs ${total_deployed:.2f} "
            f"Pwin={combined_prob:.0%} edge={edge:+.0%} roi~{expected_roi_pct:.0f}% after fees"
        )
        log.info(f"   > {reason}")

        return [PeakerSignal(
            market_title=market_title,
            city=city,
            forecast_max_c=target,
            trend=trend,
            stability_score=eff_grade,
            confidence=peak_conf,
            legs=legs,
            total_cost=round(total_cost, 4),
            basket_usd=total_deployed,
            combined_prob=combined_prob,
            n_models=n_models,
            expected_roi_pct=expected_roi_pct,
            sub_strategy=sub,
            direction=('warming' if direction > 0 else 'cooling' if direction < 0 else 'stable'),
            hold_hint=True,
            exit_hint=exit_hint,
            reason=reason,
        )]
