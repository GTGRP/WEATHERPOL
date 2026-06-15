"""
QUICK-FLIP STRATEGY v2 — Forecast-run change detection + book-or-cut exit.

WHAT CHANGED (and why the old one went 0% WR):
  The old version stored a per-CYCLE forecast snapshot, so "change" was just
  scan-to-scan jitter — it fired on noise, on a single model, and NEVER truly
  exited (target prices rarely converged), so flips decayed into bad lottery
  tickets held to resolution.

v2 fixes:
  1. RUN-BOUNDARY BASELINE — the baseline is keyed to the most recent model RUN
     (ECMWF 00/12Z, GFS 6-hourly, HRRR hourly, ...). We only compare forecasts
     ACROSS run boundaries, so a signal means "a new model run actually moved the
     forecast", not cycle jitter.
  2. KEEP BOTH ENTRY PATHS, STALE = BOOST (not a hard gate) — we still enter on a
     genuine forecast move even if the market already drifted; but when the
     market price is STALE (hasn't moved) we BOOST confidence, because that's the
     cleanest information-arbitrage window. We never *require* staleness, so we
     don't miss pure-forecast opportunities.
  3. PUBLISH-WINDOW BOOST — extra confidence when we're inside the minutes just
     after a model publishes (the actionable window).
  4. MULTI-MODEL AGREEMENT — confidence starts from the bucket's ensemble
     agreement and must clear a floor after boosts.
  5. DEDUP COOLDOWN + SMALLER SIZE + CONCURRENT CAP — stop re-signalling the same
     bucket, size each flip small, and (capped in the dashboard) limit concurrent
     flips. The flip also carries its expected hold window so the loop can
     BOOK-OR-CUT it at market instead of letting it rot (see trading/exit_policies).

FORECAST UPDATE SCHEDULE (UTC, ~15 min publish delay):
  ECMWF 00/12 (+06/18 ens) · GFS 00/06/12/18 · HRRR hourly
  ICON 3-hourly · JMA 00/06/12/18 · GEM 00/12
"""

from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

from config import Config
from data.weather_stations import get_station
from logger import log


# ── Forecast update times (UTC) ──
FORECAST_UPDATE_SCHEDULE = {
    "ECMWF": [0, 12],
    "GFS": [0, 6, 12, 18],
    "HRRR": list(range(24)),
    "ICON": [0, 3, 6, 9, 12, 15, 18, 21],
    "JMA": [0, 6, 12, 18],
    "GEM": [0, 12],
}


def _current_run() -> Tuple[str, str, int]:
    """Return (run_id, model, minutes_since_update) for the most recent model run.

    run_id encodes the model + its publish timestamp, so it only changes when a
    NEW model run publishes — that is the boundary we baseline against.
    """
    now = datetime.now(timezone.utc)
    best_model = "none"
    best_minutes = 10 ** 9
    best_dt: Optional[datetime] = None
    for model, hours in FORECAST_UPDATE_SCHEDULE.items():
        for h in hours:
            ut = now.replace(hour=h, minute=15, second=0, microsecond=0)
            if ut > now:
                ut -= timedelta(hours=24)
            diff = (now - ut).total_seconds() / 60.0
            if 0 <= diff < best_minutes:
                best_minutes = diff
                best_model = model
                best_dt = ut
    run_id = f"{best_model}:{best_dt.isoformat()}" if best_dt else "none"
    return run_id, best_model, int(best_minutes)


def minutes_since_last_update() -> Tuple[str, int]:
    """Back-compat helper: (model, minutes_since_update)."""
    _, model, minutes = _current_run()
    return model, minutes


@dataclass
class ForecastChange:
    """A real, run-over-run change in the ensemble forecast."""
    city: str
    station_icao: str
    market_type: str
    old_forecast_c: float
    new_forecast_c: float
    delta_c: float
    old_primary_bucket: str
    new_primary_bucket: str
    affected_buckets: List[str]
    timestamp: datetime


@dataclass
class QuickFlipSignal:
    """A rapid-entry, time-boxed (book-or-cut) trade signal."""
    market_title: str
    bucket_label: str
    token_id: str
    direction: str
    entry_price: float
    target_price: float
    entry_reason: str
    forecast_change: Optional[ForecastChange]
    confidence: float
    expected_hold_minutes: int
    expected_roi_pct: float
    size_usd: float
    shares: float
    our_prob: float = 0.0


class QuickFlipStrategy:
    """Enter when a NEW model run moves the forecast and the book hasn't caught
    up. Small size, dedup cooldown, time-boxed book-or-cut exit."""

    name = "quick_flip"
    description = (
        "Forecast-run change arbitrage: enter when a new model run shifts the "
        "forecast before the book adjusts; stale book / publish window boost "
        "confidence; time-boxed book-or-cut exit."
    )

    def __init__(self):
        # per (city,market_type) run-boundary baseline
        self._last_forecasts: Dict[str, dict] = {}
        # per (city,market_type,label) last seen market price (stale detection)
        self._last_prices: Dict[str, float] = {}
        # per token_id last signal time (dedup cooldown)
        self._recent_signals: Dict[str, datetime] = {}
        self._load_cfg()

    def _load_cfg(self):
        g = lambda n, d: getattr(Config, n, d)
        self.min_delta_c = float(g('QUICK_FLIP_MIN_DELTA_C', 1.0))
        self.min_confidence = float(g('QUICK_FLIP_MIN_CONFIDENCE', 0.60))
        self.max_entry_price = float(g('QUICK_FLIP_MAX_ENTRY', 0.85))
        self.max_hold_minutes = int(g('QUICK_FLIP_MAX_HOLD_MIN', 120))
        self.target_roi_pct = float(g('QUICK_FLIP_TARGET_ROI', 15.0))
        self.size_pct = float(g('QUICK_FLIP_SIZE_PCT', 0.05))
        self.max_size_usd = float(g('QUICK_FLIP_MAX_SIZE_USD', 10.0))
        self.cooldown_min = float(g('QUICK_FLIP_SIGNAL_COOLDOWN_MIN', 30.0))
        self.window_min = float(g('QUICK_FLIP_WINDOW_MIN', 20.0))
        self.window_boost = float(g('QUICK_FLIP_WINDOW_BOOST', 0.10))
        self.stale_boost = float(g('QUICK_FLIP_STALE_BOOST', 0.10))
        self.stale_eps = float(g('QUICK_FLIP_STALE_EPS', 0.01))
        # Req-23 REVIVAL: plain-mispricing entry threshold (run change no longer
        # required) + per-market cap so one market can't eat the whole scan.
        self.min_edge = float(g('QUICK_FLIP_MIN_EDGE', 0.08))
        self.max_per_market = int(g('QUICK_FLIP_MAX_PER_MARKET', 3))

    def should_poll_forecasts(self) -> bool:
        """True when we're inside the actionable window after a publish."""
        _, model, minutes = _current_run()
        if minutes < self.window_min:
            log.info(f"  FORECAST UPDATE: {model} updated {minutes}m ago — ACTIONABLE WINDOW")
            return True
        return False

    def detect_changes(
        self,
        city: str,
        market_type: str,
        bucket_probs: list,
        current_time: datetime,
        run_id: str,
    ) -> Optional[ForecastChange]:
        """Compare the current forecast to the RUN-BOUNDARY baseline. Only emits a
        change when a NEW run has moved the ensemble mean by >= min_delta_c."""
        key = f"{city}_{market_type}"
        primary = max(bucket_probs, key=lambda b: b.probability) if bucket_probs else None
        if not primary:
            return None
        station = get_station(city)
        station_icao = station.icao if station else "???"
        current_mean = primary.mean_forecast
        current_label = primary.bucket_label
        probs = {bp.bucket_label: bp.probability for bp in bucket_probs}
        snapshot = {
            "mean_temp": current_mean,
            "primary_label": current_label,
            "probs": probs,
            "run_id": run_id,
            "timestamp": current_time,
        }
        prev = self._last_forecasts.get(key)
        if prev is None:
            self._last_forecasts[key] = snapshot
            return None
        if prev.get("run_id") == run_id:
            # Same model run — no NEW data. Don't trade scan jitter.
            return None
        # A new run published: measure the run-over-run shift, then re-baseline.
        old_mean = prev["mean_temp"]
        old_label = prev["primary_label"]
        delta = abs(current_mean - old_mean)
        self._last_forecasts[key] = snapshot
        if delta < self.min_delta_c:
            return None
        affected = []
        for bp in bucket_probs:
            old_prob = prev.get("probs", {}).get(bp.bucket_label, 0)
            if abs(bp.probability - old_prob) > 0.05:
                affected.append(bp.bucket_label)
        return ForecastChange(
            city=city, station_icao=station_icao, market_type=market_type,
            old_forecast_c=old_mean, new_forecast_c=current_mean, delta_c=delta,
            old_primary_bucket=old_label, new_primary_bucket=current_label,
            affected_buckets=affected if affected else [current_label],
            timestamp=current_time,
        )

    def _update_last_prices(self, city: str, market_type: str, market_prices: dict):
        for label, price in market_prices.items():
            try:
                self._last_prices[f"{city}_{market_type}_{label}"] = float(price or 0.0)
            except (TypeError, ValueError):
                continue

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
        self._load_cfg()  # pick up live /settings overrides
        signals: List[QuickFlipSignal] = []
        if not bucket_probs or balance <= 0:
            return signals
        now = datetime.now(timezone.utc)
        run_id, model, minutes = _current_run()
        in_window = minutes < self.window_min

        # Req-23 REVIVAL: the run-boundary change is now an OPTIONAL confidence
        # BOOST, NOT a hard gate. The old v2 returned here whenever there was no
        # fresh model run, so quick_flip placed 0 trades in the Req-22/25 logs --
        # it could ONLY ever fire on a run boundary. We now ALSO take plain
        # mispricing (edge >= min_edge); a real run move just corroborates it.
        change = self.detect_changes(city, market_type, bucket_probs, now, run_id)
        changed_labels = set(change.affected_buckets) if change else set()
        if change:
            log.info(
                f"  RUN CHANGE: {city} {market_type} {change.station_icao} "
                f"shifted {change.delta_c:.1f}C ({change.old_primary_bucket}->"
                f"{change.new_primary_bucket}) on {model} run"
            )

        # Rank every bucket by edge (our_prob - market_price); most mispriced first.
        ranked = []
        for bp in bucket_probs:
            label = bp.bucket_label
            token_id = token_ids.get(label)
            if not token_id:
                continue
            market_price = float(market_prices.get(label, 0.99) or 0.99)
            our_prob = float(getattr(bp, 'probability', 0.0) or 0.0)
            ranked.append((our_prob - market_price, bp, label, token_id, market_price, our_prob))
        ranked.sort(key=lambda r: r[0], reverse=True)

        placed = 0
        for edge, bp, label, token_id, market_price, our_prob in ranked:
            if placed >= self.max_per_market:
                break
            if market_price > self.max_entry_price or market_price < 0.03:
                continue

            # TWO entry paths: (a) EARLY-MISPRICING -- our model sees edge >=
            # min_edge even with NO fresh run; (b) RUN-CHANGE -- a bucket the new
            # model run actually moved. Either qualifies; both can be true.
            run_changed = label in changed_labels
            if edge < self.min_edge and not run_changed:
                continue

            # Dedup cooldown: don't re-signal the same token repeatedly.
            last = self._recent_signals.get(token_id)
            if last and (now - last).total_seconds() / 60.0 < self.cooldown_min:
                continue

            target_price = min(0.95, our_prob * 0.9)
            if target_price <= market_price * 1.05:
                continue
            expected_roi = (target_price - market_price) / market_price * 100.0
            if expected_roi < self.target_roi_pct:
                continue

            # Confidence is DERIVED FROM EDGE (so the early path can actually
            # fire), taking the max of the edge-confidence and the ensemble
            # agreement, then boosted by a real run move / publish window / stale book.
            prev_price = self._last_prices.get(f"{city}_{market_type}_{label}")
            stale = prev_price is not None and abs(market_price - prev_price) < self.stale_eps
            edge_conf = max(0.0, min(1.0, edge / max(self.min_edge, 0.01) * 0.6))
            agree = float(getattr(bp, 'confidence', 0.0) or 0.0)
            conf = max(edge_conf, agree)
            if run_changed:
                conf += self.window_boost
            if in_window:
                conf += self.window_boost
            if stale:
                conf += self.stale_boost
            conf = min(1.0, conf)
            if conf < self.min_confidence:
                continue

            size_usd = min(balance * self.size_pct, self.max_size_usd)
            if size_usd < 1.0:
                continue
            shares = size_usd / market_price if market_price > 0 else 0
            delta_c = abs(change.delta_c) if change else 0.0
            hold_minutes = min(self.max_hold_minutes, 30 + int(delta_c * 10))
            path = "+run" if run_changed else "+early"
            tags = path + ("+window" if in_window else "") + ("+stale" if stale else "")

            signals.append(QuickFlipSignal(
                market_title=market_title,
                bucket_label=label,
                token_id=token_id,
                direction="BUY",
                entry_price=market_price,
                target_price=target_price,
                entry_reason=(
                    f"FLIP[{model}{tags}]: edge {edge:+.0%} "
                    f"buy {label} @ {market_price:.3f} -> {target_price:.3f} "
                    f"({expected_roi:.0f}% ROI, conf {conf:.0%}, <={hold_minutes}m)"
                ),
                forecast_change=change,
                confidence=conf,
                expected_hold_minutes=hold_minutes,
                expected_roi_pct=expected_roi,
                size_usd=size_usd,
                shares=shares,
                our_prob=our_prob,
            ))
            self._recent_signals[token_id] = now
            placed += 1

        self._update_last_prices(city, market_type, market_prices)
        return signals


# ── MULTI-OUTCOME SPREAD DETECTOR (kept for compatibility) ──

def find_spread_arbitrage(
    bucket_probs: list,
    market_prices: dict,
    token_ids: dict,
    balance: float,
) -> List[dict]:
    """Detect systematic underpricing (bucket prices sum < ~0.85) and surface the
    top underpriced cluster. Retained for compatibility; the peak_cluster strategy
    is the productionized, peak-centered version of this idea."""
    opportunities = []
    prices_list = [market_prices.get(bp.bucket_label, 0) for bp in bucket_probs]
    market_sum = sum(p for p in prices_list if p > 0)
    if market_sum <= 0:
        return []
    mispriced = []
    for bp in bucket_probs:
        mp = market_prices.get(bp.bucket_label, 0.99)
        if mp <= 0 or bp.probability <= 0:
            continue
        edge_ratio = bp.probability / max(mp, 0.01)
        edge = bp.probability - mp
        mispriced.append((bp, mp, edge, edge_ratio))
    mispriced.sort(key=lambda x: x[3], reverse=True)
    if market_sum < 0.85 and len(mispriced) >= 2:
        best = mispriced[:3]
        total_cost = sum(m[1] for m in best)
        total_prob = sum(m[0].probability for m in best)
        if total_cost < total_prob:
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
