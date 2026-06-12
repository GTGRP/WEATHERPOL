"""
Scoped exit policies that run in the main loop WITHOUT modifying PositionManager.

Two exits the user asked for:

1. quick_flip BOOK-OR-CUT (time-boxed): a flip is an information-arbitrage trade,
   not a hold-to-resolution bet. If it hasn't already hit its take-profit target
   (handled by PositionManager.check_risk_triggers via take_profit_price) within
   its expected hold window, we EXIT at market — booking the profit if we're up,
   cutting the loss if we're down. Flips must truly exit; they never rot to $0.

2. STRICT thesis-invalidation: the observed / hold-to-resolution book is the
   profit driver, so MOST positions keep holding to resolution. Only the VERY BAD
   ones exit early — a meaningful (non-tail) position whose price has COLLAPSED
   (the market now says we're almost certainly wrong) AND that still has a real
   bid to sell into, with time left before close. Cheap tails, stale prices, and
   near-close positions all keep holding.

Both reuse PositionManager._close_position(pos, price, 'manual') so the conserved
ledger, paper-balance credit, paper-trade log and PnL accounting stay correct;
we then overwrite pos.exit_reason with a descriptive label for the audit trail.
"""

from logger import log

try:
    from config import Config
except Exception:  # pragma: no cover
    Config = None


def _cfg(name, default):
    return getattr(Config, name, default) if Config is not None else default


def check_flip_exits(pm):
    """Book-or-cut every quick_flip position past its hold window."""
    if not _cfg('QUICK_FLIP_TIME_EXIT', True):
        return []
    default_max = float(_cfg('QUICK_FLIP_MAX_HOLD_MIN', 120))
    triggered = []
    for pos in pm.get_open_positions():
        if pos.strategy != 'quick_flip':
            continue
        if getattr(pos, 'current_price_stale', False):
            continue  # don't act on a frozen / garbage price
        max_hold = float(getattr(pos, 'flip_max_hold_minutes', 0) or default_max)
        held_min = pos.hold_hours * 60.0
        if held_min < max_hold:
            continue
        price = pos.current_price
        if price <= 0:
            continue
        pm._close_position(pos, price, 'manual')
        pos.exit_reason = 'flip_timeout'
        booked = 'BOOK' if pos.pnl >= 0 else 'CUT'
        triggered.append(pos)
        log.info(f"⏲️  FLIP {booked}: {pos.city} {pos.bucket_label[:28]} "
                 f"held {held_min:.0f}m @ ${price:.4f} PnL=${pos.pnl:+.2f}")
    if triggered:
        pm._save_state()
        pm._assert_ledger()
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
            continue  # flips use their own book-or-cut
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
        pm._close_position(pos, pos.current_price, 'manual')
        pos.exit_reason = 'thesis_invalidated'
        triggered.append(pos)
        log.info(f"🚫 THESIS EXIT: {pos.city} {pos.bucket_label[:28]} "
                 f"ROI={pos.roi_pct:.0f}% @ ${pos.current_price:.4f} "
                 f"(very bad — cut to recover residual)")
    if triggered:
        pm._save_state()
        pm._assert_ledger()
    return triggered
