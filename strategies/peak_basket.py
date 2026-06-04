"""
Peak Basket Strategy — Unified directional-peak trading.

THE EDGE:
Weather markets are neg_risk multi-outcome: exactly ONE bucket resolves to $1,
all others to $0. The cheap-tail "spread" loses (Becker 14.7M-trade proof: every
cheap tier has negative unconditional edge). The REAL edge is superior forecasting
— we know the ensemble peak temp more accurately than the market prices it.

WHAT THIS STRATEGY DOES:
1. Determines the ensemble forecast peak (weighted blend of ECMWF, GFS, ICON,
   JMA, GEM, OWM, NWS, UKMO — 7+ models, airport-station coordinates).
2. Adds ONE directional neighbor as insurance:
   - warming trend → upper neighbor (+1°C)
   - cooling trend → lower neighbor (-1°C)
   - stable/flat → peak only (no neighbor needed)
3. Scales capital dynamically: more when stability/edge/models all align,
   less when uncertain. Never bets > 25% of balance on one basket.
4. Every leg must pass the price floor (5¢ — unsellable below this).
5. Total basket cost must be < 95¢ → any single winning leg nets profit.

DESIGN RULES (from 0-for-61 live lesson):
- Never buy a leg below 5¢ — no bid exists, instant 100% loss.
- Never buy blind neighbors on both sides — only the trend direction.
- Never buy when the peak is already priced > 85¢ — market already knows.
- Always hold to resolution — thin books make early exits losing.
- The neighbour is INSURANCE, not a separate bet. Skip it if mispriced.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from config import Config
from logger import log
from data.probability_engine import BucketProbability
from data.stability import StabilityReport


# ── dataclasses ──

@dataclass
class PeakBasketLeg:
    """A single leg (bucket) in a peak basket."""
    bucket_label: str
    token_id: str
    market_price: float        # best_ask (what we pay before maker re-price)
    our_probability: float     # ensemble probability for this bucket
    size_usd: float            # how much to allocate
    role: str                  # 'peak' | 'neighbor_warm' | 'neighbor_cool'
    offset: int                # 0 = peak, +1/-1 = neighbor


@dataclass
class PeakBasketSignal:
    """A complete peak-basket trading signal."""
    market_title: str
    city: str
    forecast_max_c: float      # ensemble forecast daily max
    trend: str                 # 'warming' | 'cooling' | 'stable' | 'sideways'
    stability_score: float     # 0-1 grade
    legs: List[PeakBasketLeg] = field(default_factory=list)
    total_cost: float = 0.0
    combined_prob: float = 0.0    # P(any leg wins)
    n_models: int = 0
    sizing_multiplier: float = 1.0
    direction: str = 'peak_only'  # 'peak_only' | 'warming' | 'cooling'
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

class PeakBasketStrategy:
    """
    Unified directional-peak basket — the ONLY strategy the bot needs.

    Decision chain:
      stability gate → find peak → directional neighbor → price/edge checks
      → dynamic sizing → HOLD to resolution.
    """

    name = "peak_basket"
    description = (
        "Buy the ensemble forecast peak + one trend-directional neighbor. "
        "Scale capital with stability, model agreement, and edge. "
        "Hold to resolution. Never buy unsellable cheap legs."
    )

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
    ) -> List[PeakBasketSignal]:
        """
        Evaluate a single weather market and produce zero or one PeakBasketSignal.

        Returns a list so it slots cleanly into the existing dashboard loop;
        at most one signal per market.
        """
        # ── gate 1: stability ──
        if stability is None:
            log.debug(f"PeakBasket {city}: no stability report -- skip")
            return []
        if not stability.predictable and stability.score < Config.PEAK_MIN_STABILITY:
            log.info(f"   SKIP PEAK BASKET {city}: grade {stability.score:.2f} < {Config.PEAK_MIN_STABILITY}")
            return []
        if grade < Config.PEAK_MIN_STABILITY:
            log.info(f"   SKIP PEAK BASKET {city}: passed grade {grade:.2f} < {Config.PEAK_MIN_STABILITY}")
            return []

        # ── gate 2: need enough models ──
        n_models = max(bp.n_models for bp in bucket_probs) if bucket_probs else 0
        if n_models < Config.PEAK_MIN_MODELS:
            log.debug(f"PeakBasket {city}: only {n_models} models (need {Config.PEAK_MIN_MODELS})")
            return []

        # ── gate 3: need usable buckets ──
        if not bucket_probs:
            return []

        # ── index buckets by numeric center for adjacency math ──
        indexed = []
        for bp in bucket_probs:
            c = _bucket_numeric_center(bp.bucket_label, bp.bucket_low, bp.bucket_high)
            if c is not None:
                indexed.append((c, bp))
        if not indexed:
            return []
        indexed.sort(key=lambda x: x[0])

        # ── find the peak bucket (closest to ensemble forecast max) ──
        target = stability.forecast_max_c
        center_i = min(range(len(indexed)), key=lambda i: abs(indexed[i][0] - target))
        center_val, center_bp = indexed[center_i]

        # ── determine direction from trend ──
        trend = (stability.trend or 'unknown').lower()
        if trend == 'warming':
            direction = +1
            direction_label = 'warming'
        elif trend == 'cooling':
            direction = -1
            direction_label = 'cooling'
        else:
            # stable/sideways/unknown → no neighbor needed; models agree on the peak
            direction = 0
            direction_label = 'peak_only'

        # ── build candidate legs: peak always, neighbor only if directional ──
        candidates: List[Tuple[int, BucketProbability, str]] = []
        # peak
        candidates.append((center_i, center_bp, 'peak'))

        # directional neighbor
        if direction != 0:
            ni = center_i + direction
            if 0 <= ni < len(indexed):
                neighbor_val, neighbor_bp = indexed[ni]
                role = 'neighbor_warm' if direction > 0 else 'neighbor_cool'
                candidates.append((ni, neighbor_bp, role))

        # ── price & floor checks for every candidate ──
        kept: List[Tuple[int, BucketProbability, str, float, str]] = []
        for idx, bp, role in candidates:
            price = market_prices.get(bp.bucket_label)
            tid = token_ids.get(bp.bucket_label)
            if price is None or tid is None:
                log.debug(f"PeakBasket {city}: {bp.bucket_label} -- no price/token")
                continue
            if price <= 0:
                continue

            # ── HARD price floor ──
            if price < Config.MIN_ENTRY_PRICE:
                log.info(f"   SKIP PEAK FLOOR {city}:{bp.bucket_label} ${price:.4f} < ${Config.MIN_ENTRY_PRICE:.2f}")
                continue

            # ── role-specific price caps ──
            if role == 'peak' and price > Config.PEAK_MAX_PEAK_PRICE:
                log.info(f"   SKIP PEAK EXPENSIVE {city}:{bp.bucket_label} ${price:.2f} > ${Config.PEAK_MAX_PEAK_PRICE:.2f} (market already knows)")
                continue
            if role != 'peak' and price > Config.PEAK_MAX_NEIGHBOR_PRICE:
                log.debug(f"   SKIP NEIGHBOR EXPENSIVE {city}:{bp.bucket_label} ${price:.2f} > ${Config.PEAK_MAX_NEIGHBOR_PRICE:.2f}")
                continue

            kept.append((idx, bp, role, price, tid))

        # Must have at least the peak
        if not any(role == 'peak' for _, _, role, _, _ in kept):
            log.info(f"   SKIP PEAK SKIP {city}: peak bucket failed checks")
            return []

        # ── basket cost & profit-guarantee check ──
        total_market_cost = sum(price for _, _, _, price, _ in kept)
        if total_market_cost >= Config.PEAK_MAX_BASKET_COST:
            log.info(f"   SKIP PEAK COST {city}: basket ${total_market_cost:.2f} >= ${Config.PEAK_MAX_BASKET_COST:.2f}")
            return []
        if total_market_cost <= 0:
            return []

        # ── edge check: P(any leg wins) must exceed basket cost by min edge ──
        combined_prob = sum(bp.probability for _, bp, _, _, _ in kept)
        combined_prob = min(0.99, combined_prob)
        basket_edge = combined_prob - total_market_cost
        if basket_edge < Config.PEAK_MIN_EDGE:
            log.info(f"   SKIP PEAK EDGE {city}: edge {basket_edge:.1%} < {Config.PEAK_MIN_EDGE:.1%} "
                     f"(Pwin={combined_prob:.0%} cost=${total_market_cost:.2f})")
            return []

        # ── dynamic sizing (the capital-scaling engine) ──
        # base = BASE_FRACTION of balance
        # mult = stability × model_agreement × edge × efficiency
        base = balance * Config.PEAK_BASE_FRACTION
        base = max(Config.MIN_ORDER_SIZE * len(kept), base)

        # stability multiplier: higher score → bigger bet
        stability_mult = 0.40 + (stability.score * 0.75)  # 0.70→0.92, 0.90→1.08
        stability_mult = max(0.25, min(1.50, stability_mult))

        # model-agreement multiplier: more models + lower spread → higher conviction
        peak_bp = next(bp for _, bp, role, _, _ in kept if role == 'peak')
        model_spread = getattr(peak_bp, 'std_forecast', 1.5) or 1.5
        model_mult = min(1.0, n_models * 0.15) * (1.0 / max(0.5, model_spread))
        model_mult = max(0.30, min(1.80, model_mult))

        # edge multiplier: bigger mispricing → bet bigger
        edge_mult = 0.70 + (basket_edge * 4.0)  # edge 5%→0.90, 15%→1.30, 25%→1.70
        edge_mult = max(0.50, min(2.00, edge_mult))

        # basket-efficiency multiplier: cheaper basket → higher ROI per dollar → bet more
        cost_ratio = total_market_cost / Config.PEAK_MAX_BASKET_COST  # 0 = free, 1 = at cap
        efficiency_mult = 1.50 - cost_ratio  # cheap=1.50, expensive=0.55
        efficiency_mult = max(0.50, min(1.50, efficiency_mult))

        sizing_mult = stability_mult * model_mult * edge_mult * efficiency_mult
        # Clamp: never risk more than MAX_FRACTION ÷ BASE_FRACTION effective multiplier
        max_effective_mult = Config.PEAK_MAX_FRACTION / max(Config.PEAK_BASE_FRACTION, 0.01)
        sizing_mult = max(0.20, min(max_effective_mult, sizing_mult))

        basket_usd = base * sizing_mult
        basket_usd = max(Config.MIN_ORDER_SIZE * len(kept), basket_usd)
        basket_usd = min(basket_usd, balance * Config.PEAK_MAX_FRACTION)

        # ── allocate across legs: peak gets 65% of basket, neighbors split the rest ──
        peak_count = sum(1 for _, _, role, _, _ in kept if role == 'peak')
        neighbor_count = len(kept) - peak_count
        peak_alloc_pct = 0.65 if neighbor_count > 0 else 1.0
        neighbor_alloc_pct = (1.0 - peak_alloc_pct) / max(neighbor_count, 1)

        legs = []
        for idx, bp, role, price, tid in kept:
            if role == 'peak':
                leg_usd = basket_usd * peak_alloc_pct
            else:
                leg_usd = basket_usd * neighbor_alloc_pct
            leg_usd = max(Config.MIN_ORDER_SIZE, round(leg_usd, 2))

            offset = 0 if role == 'peak' else (+1 if role == 'neighbor_warm' else -1)
            legs.append(PeakBasketLeg(
                bucket_label=bp.bucket_label,
                token_id=tid,
                market_price=price,
                our_probability=bp.probability,
                size_usd=leg_usd,
                role=role,
                offset=offset,
            ))

        total_cost = round(sum(l.size_usd for l in legs), 2)

        # ── exit logic ──
        if stability.rain_block:
            exit_hint = "HOLD — rain caps the high (peak likely to hit)"
            hold_hint = True
        elif trend in ('warming', 'cooling') and stability.score >= 0.70:
            exit_hint = "HOLD — directional trend strong, let it resolve"
            hold_hint = True
        elif stability.score >= 0.80:
            exit_hint = "HOLD — high stability, resolution is near-certain"
            hold_hint = True
        else:
            exit_hint = "HOLD — thin books, better to resolve"
            hold_hint = True

        # ── reason string (one-line log summary) ──
        peak_label = next(l.bucket_label for l in legs if l.role == 'peak')
        neighbor_str = ''
        for l in legs:
            if l.role != 'peak':
                neighbor_str = f" +{l.bucket_label}" if l.offset > 0 else f" {l.bucket_label}"
                break
        if not neighbor_str:
            neighbor_str = ' (peak-only)'

        reason = (
            f"PEAK {city} trend={trend} grade={stability.score:.2f} | "
            f"peak={peak_label} (fcst {target:.1f}°C){neighbor_str} | "
            f"{len(legs)}legs ${total_cost:.2f} Pwin={combined_prob:.0%} "
            f"edge={basket_edge:.0%} | "
            f"×{sizing_mult:.2f} (stab{stability_mult:.1f} mod{model_mult:.1f} "
            f"edge{edge_mult:.1f} eff{efficiency_mult:.1f}) | {exit_hint}"
        )

        log.info(f"   > PEAK {reason}")
        return [PeakBasketSignal(
            market_title=market_title,
            city=city,
            forecast_max_c=target,
            trend=trend,
            stability_score=stability.score,
            legs=legs,
            total_cost=total_cost,
            combined_prob=combined_prob,
            n_models=n_models,
            sizing_multiplier=sizing_mult,
            direction=direction_label,
            hold_hint=hold_hint,
            exit_hint=exit_hint,
            reason=reason,
        )]
