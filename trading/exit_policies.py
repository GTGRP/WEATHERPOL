"""
Scoped exit policies that run in the main loop WITHOUT modifying PositionManager.

Two exits the user asked for:

1. quick_flip PROFIT-ONLY LADDERED exit (Req-27): a flip is an information-
   arbitrage trade and must SELL ONLY IN PROFIT. The ML (XGBoost / GPT) decides
   whether to BOOK A SMALL profit now or let it RUN for more, laddered at
   ~10% / 20% / 30% ROI. Big runners (>= force-book ROI) are ALWAYS booked so we
   never round-trip a winner back to breakeven. A flip that is NOT in profit by
   its hold cap is NEVER booked at a loss or breakeven (the user: "book / flip
   exit closing in loss and breakeven is bad") -- instead it CONVERTS to
   hold-to-resolution and rides to settlement. Set QUICK_FLIP_PROFIT_ONLY_EXIT=0
   for the legacy book-or-cut-at-market behaviour.

2. STRICT thesis-invalidation: the observed / hold-to-resolution book is the
   profit driver, so MOST positions keep holding to resolution. Only the VERY BAD
   ones exit early -- a meaningful (non-tail) position whose price has COLLAPSED
   (the market now says we're almost certainly wrong) AND that still has a real
   bid to sell into, with time left before close. Cheap tails, stale prices,
   near-close positions, and quick_flips all keep holding.

Both reuse PositionManager._close_position(pos, price, reason) so the conserved
ledger, paper-balance credit, paper-trade log and PnL accounting stay correct.
The descriptive reason is now passed STRAIGHT INTO _close_position (instead of
being overwritten on pos.exit_reason AFTER the close logs), so paper_trades.jsonl
records the true exit reason instead of a generic 'manual' (Req-27 logging fix).
"""

from logger import log

try:
    from config import Config
except Exception:  # pragma: no cover
    Config = None


def _cfg(name, default):
    return getattr(Config, name, default) if Config is not None else default


# Lazy ML engine singleton so flips can ask "sell small vs run more" without the
# dashboard having to thread its engine through. Safe no-op when no API key: the
# engine falls back to its local model / a HOLD default, in which case the rules
# ladder below still books at the force-book rung and when the window expires.
_ml_engine = None
_ml_init_failed = False


def _get_ml():
    global _ml_engine, _ml_init_failed
    if _ml_engine is not None:
        return _ml_engine
    if _ml_init_failed:
        return None
    try:
        from ml.decision_engine import MLDecisionEngine
        _ml_engine = MLDecisionEngine()
    except Exception as e:  # pragma: no cover
        _ml_init_failed = True
        log.debug(f"flip-exit ML init failed: {e}")
        return None
    return _ml_engine


def _ml_says_book(pos) -> bool:
    """Ask the ML engine whether to SELL (book) this in-profit flip now or HOLD
    for more. Returns True to book, False to keep running. Defaults to keep
    running when the ML is unavailable -- the rules ladder still force-books big
    runners and books everything once the hold window expires."""
    ml = _get_ml()
    if ml is None:
        return False
    try:
        mtc = pos.minutes_to_close
        res_hours = (mtc / 60.0) if mtc is not None else 24.0
        res = ml.review_position(
            pos.city, pos.bucket_label, pos.entry_price, pos.current_price,
            pos.hold_hours, res_hours,
        )
        return str(res.get('action', 'HOLD')).upper() == 'SELL'
    except Exception as e:
        log.debug(f"flip-exit ML review failed: {e}")
        return False


def check_flip_exits(pm):
    """Profit-only laddered exit for quick_flip positions.

    Books small/mid profits (ML-gated), ALWAYS books big runners, and NEVER cuts
    a flip at a loss/breakeven on the timer -- a flip that isn't profitable by
    its hold cap converts to hold-to-resolution instead.
    """
    if not _cfg('QUICK_FLIP_TIME_EXIT', True):
        return []
    default_max = float(_cfg('QUICK_FLIP_MAX_HOLD_MIN', 120))
    profit_only = bool(_cfg('QUICK_FLIP_PROFIT_ONLY_EXIT', True))
    use_ml = bool(_cfg('QUICK_FLIP_USE_ML_EXIT', True))
    min_book = float(_cfg('QUICK_FLIP_MIN_BOOK_ROI_PCT', 10.0))
    mid_book = float(_cfg('QUICK_FLIP_LADDER_MID_ROI_PCT', 20.0))
    force_book = float(_cfg('QUICK_FLIP_FORCE_BOOK_ROI_PCT', 30.0))
    triggered = []
    converted = False
    for pos in pm.get_open_positions():
        if pos.strategy != 'quick_flip':
            continue
        # Flips already converted to hold-to-resolution ride to settlement.
        if getattr(pos, 'hold_to_resolution', False):
            continue
        if getattr(pos, 'current_price_stale', False):
            continue  # don't act on a frozen / garbage price
        price = pos.current_price
        if price <= 0:
            continue
        roi = pos.roi_pct
        max_hold = float(getattr(pos, 'flip_max_hold_minutes', 0) or default_max)
        held_min = pos.hold_hours * 60.0
        window_over = held_min >= max_hold

        # 1) Always book a big runner (don't round-trip a winner).
        if roi >= force_book:
            pm._close_position(pos, price, 'flip_book_run')
            triggered.append(pos)
            log.info(f"⏲️  FLIP BOOK-RUN: {pos.city} {pos.bucket_label[:28]} "
                     f"ROI={roi:+.0f}% @ ${price:.4f} PnL=${pos.pnl:+.2f}")
            continue

        # 2) In profit on a lower rung: ML decides sell-small vs run-more.
        if roi >= min_book:
            sell = True
            if use_ml and not window_over:
                # Let it run toward the next rung unless the ML says book now.
                sell = _ml_says_book(pos)
            if sell:
                reason = 'flip_book_mid' if roi >= mid_book else 'flip_book_small'
                pm._close_position(pos, price, reason)
                triggered.append(pos)
                log.info(f"⏲️  FLIP BOOK ({reason.split('_')[-1]}): {pos.city} "
                         f"{pos.bucket_label[:28]} ROI={roi:+.0f}% @ ${price:.4f} "
                         f"PnL=${pos.pnl:+.2f}")
            continue

        # 3) Not in profit yet.
        if not window_over:
            continue  # still inside the window -- keep waiting for profit
        if profit_only:
            # NEVER book at a loss/breakeven on the timer. Convert to
            # hold-to-resolution so the flip rides to settlement instead.
            pos.hold_to_resolution = True
            pos.take_profit_price = 0.99
            pos.exit_reason = 'flip_to_hold'
            converted = True
            log.info(f"⏳ FLIP→HOLD: {pos.city} {pos.bucket_label[:28]} "
                     f"ROI={roi:+.0f}% not profitable by {held_min:.0f}m — "
                     f"holding to resolution (profit-only exit)")
        else:
            # Legacy book-or-cut at market.
            pm._close_position(pos, price, 'flip_timeout')
            triggered.append(pos)
            log.info(f"⏲️  FLIP CUT: {pos.city} {pos.bucket_label[:28]} "
                     f"held {held_min:.0f}m @ ${price:.4f} PnL=${pos.pnl:+.2f}")
    if triggered:
        pm._save_state()
        pm._assert_ledger()
    elif converted:
        pm._save_state()
    return triggered


def check_thesis_exits(pm):
    """STRICT early exit: only VERY BAD, non-tail, exitable positions sell early.
    Everything else keeps holding to resolution."""
    if not _cfg('THESIS_EXIT_ENABLED', True):
        return []
    max_roi = float(_cfg('THESIS_EXIT_MAX_ROI_PCT', -85.0))
    min_entry = float(_cfg('THESIS_EXIT_MIN_ENTRY_PRICE', 0.10))
    min_bid = float(_cfg('THESIS_EXIT_MIN_BID', 0.02))
    min_mtc = float(_cfg('THESIS_EXIT_MIN_MINUTES_TO_CLOSE', 60.0))
    triggered = []
    for pos in pm.get_open_positions():
        if pos.strategy == 'quick_flip':
            continue  # flips use their own profit-only exit
        if getattr(pos, 'current_price_stale', False):
            continue  # don't act on a stale price
        if pos.entry_price < min_entry:
            continue  # cheap tail = the edge -> hold to resolution
        if pos.current_price < min_bid:
            continue  # no real bid to exit into -> hold to resolution
        mtc = pos.minutes_to_close
        if mtc is not None and mtc < min_mtc:
            continue  # too close to close -> let it resolve
        if pos.roi_pct > max_roi:
            continue  # not "very bad" yet -> keep holding
        pm._close_position(pos, pos.current_price, 'thesis_invalidated')
        triggered.append(pos)
        log.info(f"🚫 THESIS EXIT: {pos.city} {pos.bucket_label[:28]} "
                 f"ROI={pos.roi_pct:.0f}% @ ${pos.current_price:.4f} "
                 f"(very bad — cut to recover residual)")
    if triggered:
        pm._save_state()
        pm._assert_ledger()
    return triggered
