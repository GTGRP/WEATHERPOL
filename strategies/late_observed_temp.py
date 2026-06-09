"""
Late Observed-Temperature strategy — the overhauled PRIMARY edge.

Thesis
------
For a daily-high (or daily-low) market, once the local day is far enough along
that the peak heating (or overnight cooling) is essentially done, the day's
extreme is *physically locked*: the final settled high can only be ≥ the max
already observed. The order book, however, often still prices stale forecast
uncertainty. We exploit the gap two ways:

* **YES** the bucket that the observed data says will win, when its price still
  leaves a positive, fee-adjusted edge.
* **NO** the buckets the observed data has made *impossible* (e.g. a low bucket
  after the high is already locked above it) when the book still prices them
  rich enough to clear fees — the audit's NO-side edge.

Gating is fee-aware (Polymarket weather taker fee = 5% × p × (1−p)) and
timing-aware (only trades once the day is sufficiently "locked").

The pure decision core lives in :func:`decide_legs` (only depends on
``data.fees`` + stdlib) so it is fully unit-testable offline. The
:class:`LateObservedTempStrategy` wraps it with project ``Config`` and the
observed-temperature probability model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from data import fees

try:  # keep importable offline (Config imports dotenv)
    from config import Config  # type: ignore
except Exception:  # pragma: no cover
    Config = None  # type: ignore

try:
    from logger import log  # type: ignore
except Exception:  # pragma: no cover
    import logging
    log = logging.getLogger("late_observed_temp")


@dataclass
class LateObservedLeg:
    bucket_label: str
    side: str               # 'YES' or 'NO'
    token_id: str
    price: float            # price of the token we BUY (yes price for YES, no price for NO)
    our_probability: float  # our P(this token wins)
    edge: float             # our_probability - fee-adjusted breakeven
    ev_per_contract: float
    size_usd: float
    reason: str = ""


@dataclass
class LateObservedSignal:
    market_title: str
    city: str
    market_type: str
    observed_extreme_c: float
    remaining_extreme_c: Optional[float]
    hours_remaining: int
    lock_confidence: float
    legs: List[LateObservedLeg] = field(default_factory=list)
    reason: str = ""


@dataclass
class DecideParams:
    """Plain thresholds for the pure decision core (no Config dependency)."""
    min_edge: float = 0.10           # post-fee probability cushion required
    min_entry_price: float = 0.05    # ignore dust-priced YES legs
    max_entry_price: float = 0.95    # don't pay through the roof for YES
    no_min_price: float = 0.04       # only NO a dead bucket if book still prices it richly
    no_max_price: float = 0.97       # NO token price ceiling (avoid ~$1 no-edge fills)
    taker: bool = True
    base_fraction: float = 0.06      # base stake as fraction of balance
    max_fraction: float = 0.25       # cap per-leg stake fraction
    kelly_cap: float = 0.25
    max_legs: int = 4
    min_order_usd: float = 1.0


def _stake_usd(prob_win: float, price: float, balance: float, grade: float,
               params: DecideParams) -> float:
    """Edge/grade/Kelly-blended stake, clamped to fraction caps and balance."""
    kelly = fees.kelly_fraction(prob_win, price, cap=params.kelly_cap)
    frac = params.base_fraction + kelly
    frac = min(frac, params.max_fraction)
    frac *= max(0.0, min(1.5, grade / 0.6)) if grade else 1.0
    size = frac * balance
    size = min(size, balance * params.max_fraction)
    if size < params.min_order_usd:
        size = params.min_order_usd
    return round(min(size, balance), 2)


def decide_legs(
    observed_probs: Dict[str, float],
    yes_prices: Dict[str, float],
    yes_token_ids: Dict[str, str],
    balance: float,
    grade: float = 0.6,
    no_prices: Optional[Dict[str, float]] = None,
    no_token_ids: Optional[Dict[str, str]] = None,
    params: Optional[DecideParams] = None,
) -> List[LateObservedLeg]:
    """Pure decision core: turn observed bucket probabilities + book prices into
    fee-cleared YES/NO legs. Depends only on ``data.fees`` + stdlib.
    """
    params = params or DecideParams()
    no_prices = no_prices or {}
    no_token_ids = no_token_ids or {}
    legs: List[LateObservedLeg] = []

    for label, p_win in observed_probs.items():
        # --- YES side: bucket observed data expects to win ----------------
        yp = yes_prices.get(label)
        ytid = yes_token_ids.get(label)
        if yp is not None and ytid and params.min_entry_price <= yp <= params.max_entry_price:
            if fees.passes_fee_gate(p_win, yp, params.min_edge, params.taker):
                edge = p_win - fees.breakeven_prob(yp, params.taker)
                legs.append(LateObservedLeg(
                    bucket_label=label, side="YES", token_id=ytid, price=yp,
                    our_probability=p_win, edge=edge,
                    ev_per_contract=fees.ev_per_contract(p_win, yp, params.taker),
                    size_usd=_stake_usd(p_win, yp, balance, grade, params),
                    reason=f"observed P(win)={p_win:.0%} vs YES px {yp:.0%}, edge {edge:+.0%}",
                ))
                continue  # don't also NO a bucket we're going YES on

        # --- NO side: bucket observed data says is (near) impossible -------
        prob_no = 1.0 - p_win
        np_ = no_prices.get(label)
        ntid = no_token_ids.get(label)
        if np_ is None or not ntid:
            continue
        if not (params.no_min_price <= np_ <= params.no_max_price):
            continue
        if fees.passes_fee_gate(prob_no, np_, params.min_edge, params.taker):
            edge = prob_no - fees.breakeven_prob(np_, params.taker)
            legs.append(LateObservedLeg(
                bucket_label=label, side="NO", token_id=ntid, price=np_,
                our_probability=prob_no, edge=edge,
                ev_per_contract=fees.ev_per_contract(prob_no, np_, params.taker),
                size_usd=_stake_usd(prob_no, np_, balance, grade, params),
                reason=f"observed P(no)={prob_no:.0%} vs NO px {np_:.0%}, edge {edge:+.0%}",
            ))

    # Keep the strongest few legs by edge to respect bankroll / position caps.
    legs.sort(key=lambda l: l.edge, reverse=True)
    return legs[: params.max_legs]


class LateObservedTempStrategy:
    """Observation-driven primary strategy (wraps :func:`decide_legs`)."""

    name = "late_observed_temp"

    def __init__(self):
        c = Config
        def g(attr, default):
            return getattr(c, attr, default) if c is not None else default
        self.enabled = bool(g("LATE_OBSERVED_ENABLED", 1))
        self.no_side_enabled = bool(g("LATE_OBSERVED_NO_SIDE", 1))
        self.min_lock_conf = float(g("LATE_OBSERVED_MIN_LOCK", 0.70))
        self.params = DecideParams(
            min_edge=float(g("LATE_OBSERVED_MIN_EDGE", 0.10)),
            min_entry_price=float(g("MIN_ENTRY_PRICE", 0.05)),
            max_entry_price=float(g("LATE_OBSERVED_MAX_YES_PRICE", 0.95)),
            no_min_price=float(g("LATE_OBSERVED_NO_MIN_PRICE", 0.04)),
            no_max_price=float(g("LATE_OBSERVED_NO_MAX_PRICE", 0.97)),
            taker=bool(g("ASSUME_TAKER_FILLS", 1)),
            base_fraction=float(g("LATE_OBSERVED_BASE_FRACTION", 0.06)),
            max_fraction=float(g("LATE_OBSERVED_MAX_FRACTION", 0.25)),
            kelly_cap=float(g("KELLY_FRACTION", 0.15)) + 0.10,
            max_legs=int(g("LATE_OBSERVED_MAX_LEGS", 4)),
            min_order_usd=float(g("MIN_ORDER_SIZE", 1.0)),
        )

    def evaluate(
        self,
        market_title: str,
        buckets: Sequence[Tuple[str, float, float]],
        yes_prices: Dict[str, float],
        yes_token_ids: Dict[str, str],
        balance: float,
        city: str,
        observed_state,
        *,
        no_prices: Optional[Dict[str, float]] = None,
        no_token_ids: Optional[Dict[str, str]] = None,
        grade: float = 0.6,
        market_type: str = "highest_temperature",
    ) -> List[LateObservedSignal]:
        """Build at most one signal (a set of legs) for this market."""
        from data import observed_math as om  # local import: pure, always available

        if not self.enabled or observed_state is None:
            return []

        mode = "low" if "low" in (market_type or "").lower() else "high"
        lock = om.lock_confidence(
            observed_state.observed_extreme_c,
            observed_state.remaining_extreme_c,
            observed_state.remaining_spread_c,
            mode=mode,
        )
        if lock < self.min_lock_conf:
            return []

        probs = om.observed_bucket_probabilities(
            observed_state.observed_extreme_c,
            observed_state.remaining_extreme_c,
            observed_state.remaining_spread_c,
            list(buckets),
            mode=mode,
        )

        params = self.params
        if not self.no_side_enabled:
            no_prices = None
            no_token_ids = None

        legs = decide_legs(
            observed_probs=probs,
            yes_prices=yes_prices,
            yes_token_ids=yes_token_ids,
            balance=balance,
            grade=grade,
            no_prices=no_prices,
            no_token_ids=no_token_ids,
            params=params,
        )
        if not legs:
            return []

        return [LateObservedSignal(
            market_title=market_title,
            city=city,
            market_type=market_type,
            observed_extreme_c=observed_state.observed_extreme_c,
            remaining_extreme_c=observed_state.remaining_extreme_c,
            hours_remaining=observed_state.hours_remaining,
            lock_confidence=lock,
            legs=legs,
            reason=(f"LATE-OBSERVED {mode.upper()} | observed={observed_state.observed_extreme_c:.1f}°C "
                    f"| lock={lock:.0%} | {len(legs)} leg(s)"),
        )]
