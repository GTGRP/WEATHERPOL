"""
Position Manager v2 — Full lifecycle with stop-loss, take-profit, weekly memory.

Features:
- Per-position P&L tracking (individual + aggregate)
- Stop-loss: auto-sell if price drops below threshold
- Take-profit: auto-sell if price rises above target
- Weekly performance memory (tracks weekly stats for ML learning)
- Context cleanup: free memory for closed/resolved markets
- Separate tracking: bought, holding, sold, won, lost, redeemed
- Market context pool: only active markets consume memory
"""

import os
import json
import time
import requests
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field

from config import Config
from logger import log



@dataclass
class PositionRules:
    """Per-position risk management rules."""
    stop_loss_pct: float = -80.0       # sell if ROI drops below this %
    take_profit_pct: float = 300.0     # sell if ROI exceeds this %
    take_profit_price: float = 0.50    # sell YES token if price rises above this
    max_hold_hours: float = 48.0       # max time to hold before forced review
    trailing_stop: bool = False        # enable trailing stop
    trail_pct: float = 20.0            # trailing stop percentage from peak


@dataclass
class TrackedPosition:
    """A position being tracked by the bot."""
    id: str
    market_title: str
    bucket_label: str
    token_id: str
    condition_id: str
    entry_price: float
    shares: float
    cost_usd: float
    current_price: float
    current_value: float
    entry_time: datetime
    resolution_time: Optional[datetime]
    strategy: str
    city: str = ''
    slug: str = ''
    # Status lifecycle: open → (won|lost|sold|redeemed)
    status: str = 'open'
    pnl: float = 0.0
    realized_pnl: float = 0.0
    redeemable: bool = False
    # Risk management
    peak_price: float = 0.0           # highest price seen (for trailing stop)
    stop_loss_pct: float = -80.0
    take_profit_price: float = 0.50
    # Metadata
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    exit_reason: str = ''             # 'resolution', 'stop_loss', 'take_profit', 'manual'


    @property
    def unrealized_pnl(self) -> float:
        if self.status == 'open':
            return self.current_value - self.cost_usd
        return self.pnl

    @property
    def roi_pct(self) -> float:
        if self.cost_usd <= 0:
            return 0
        return (self.unrealized_pnl / self.cost_usd) * 100

    @property
    def hold_hours(self) -> float:
        return (datetime.now(timezone.utc) - self.entry_time).total_seconds() / 3600

    @property
    def is_expired(self) -> bool:
        if self.resolution_time:
            return datetime.now(timezone.utc) > self.resolution_time
        return False


@dataclass
class WeeklyStats:
    """Weekly performance summary for ML memory."""
    week_start: str           # ISO date
    trades: int = 0
    wins: int = 0
    losses: int = 0
    pnl: float = 0.0
    roi_pct: float = 0.0
    best_city: str = ''
    best_strategy: str = ''
    avg_entry_price: float = 0.0
    avg_edge: float = 0.0


@dataclass
class MarketContext:
    """Active market context (freed when market resolves)."""
    slug: str
    city: str
    market_type: str
    resolution_time: Optional[datetime]
    our_probability: float = 0.0
    market_price: float = 0.0
    edge: float = 0.0
    forecast_temp: float = 0.0
    n_models: int = 0
    last_updated: float = 0.0
    active: bool = True



class PositionManager:
    """Full position lifecycle manager with risk controls and memory."""

    def __init__(self):
        self.positions: List[TrackedPosition] = []
        self.weekly_history: List[WeeklyStats] = []
        self.market_contexts: Dict[str, MarketContext] = {}
        self.paper_balance = Config.STARTING_BALANCE
        self.total_deposited = Config.STARTING_BALANCE
        self.total_redeemed = 0.0
        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        self._state_file = 'data/positions.json'
        self._weekly_file = 'data/weekly_memory.json'
        self._session = requests.Session()
        self._session.headers['User-Agent'] = f'WeatherSniper/{Config.VERSION}'
        self._load_state()
        self._load_weekly()

    # ═══════════════════════════════════════════════════════════════
    # BALANCE
    # ═══════════════════════════════════════════════════════════════

    def get_balance(self) -> float:
        if Config.is_paper():
            return self.paper_balance
        return self._get_onchain_balance() or self.paper_balance

    def get_portfolio_value(self) -> float:
        open_val = sum(p.current_value for p in self.positions if p.status == 'open')
        return self.get_balance() + open_val

    def get_total_pnl(self) -> float:
        realized = sum(p.pnl for p in self.positions if p.status != 'open')
        unrealized = sum(p.unrealized_pnl for p in self.positions if p.status == 'open')
        return realized + unrealized

    def _get_onchain_balance(self) -> Optional[float]:
        wallet = Config.POLY_PROXY_WALLET
        if not wallet:
            return None
        try:
            resp = self._session.get(
                'https://data-api.polymarket.com/positions',
                params={'user': wallet}, timeout=10,
            )
            # Derive from known positions
            return None
        except Exception:
            return None


    # ═══════════════════════════════════════════════════════════════
    # POSITION LIFECYCLE
    # ═══════════════════════════════════════════════════════════════

    def add_position(self, token_id: str, condition_id: str, entry_price: float,
                     shares: float, cost_usd: float, market_title: str,
                     bucket_label: str, strategy: str, city: str = '',
                     slug: str = '', resolution_time: datetime = None,
                     edge: float = 0.0) -> Optional[TrackedPosition]:
        """Add new position — checks balance FIRST, only tracks if order succeeds."""
        # ═══ VALIDATE SHARES (must be positive) ═══
        if shares <= 0 or cost_usd <= 0 or entry_price <= 0:
            return None

        # ═══ DUPLICATE GUARD ═══
        # Block re-buying the SAME outcome with the SAME strategy while an order
        # for it is still open or pending. A DIFFERENT strategy on the same market
        # (e.g. spread leg vs confident) is allowed.
        for p in self.positions:
            if (p.token_id == token_id and p.strategy == strategy
                    and p.status in ('open', 'pending')):
                log.debug(f"⏭️  SKIP dup: {city} {bucket_label[:25]} already {p.status} [{strategy}]")
                return None

        # ═══ MIN ORDER: GTC needs ≥5 shares AND ≥ $1 notional ═══
        # Polymarket GTC floors to 5 shares; FOK/FAK need ≥ $1. We require both
        # (max of the two) so no order is dust. Applied in PAPER too so paper
        # simulates exactly what the venue would accept — otherwise grade/liquidity
        # size-trimming can produce sub-minimum "0-share" orders that can't fill.
        # Bump the spend up to the minimum (capped at balance by the check below).
        min_notional = max(Config.MIN_ORDER_SIZE, round(5 * entry_price, 2))
        if cost_usd < min_notional:
            cost_usd = min_notional
            shares = cost_usd / entry_price

        # ═══ BALANCE CHECK (after min-order bump so it reflects real spend) ═══
        # Skip (don't retry) when balance is insufficient — surfaced at INFO so
        # it's visible. Freed balance from sells/resolutions is picked up next scan.
        if not Config.is_paper():
            available = self.get_live_balance()   # CLOB query (10s cache, force-refreshed after each order)
            if available is not None and cost_usd > available:
                log.info(f"⏭️  SKIP {city} {bucket_label[:22]} — need ${cost_usd:.2f}, only ${available:.2f} (waiting for positions to resolve)")
                return None
        else:
            if cost_usd > self.paper_balance:
                log.info(f"⏭️  SKIP {city} {bucket_label[:22]} — need ${cost_usd:.2f}, only ${self.paper_balance:.2f} (waiting for positions to resolve)")
                return None

        # Determine take-profit based on entry price
        if entry_price < 0.05:
            tp_price = min(0.50, entry_price * 8)
        elif entry_price < 0.15:
            tp_price = min(0.60, entry_price * 5)
        else:
            tp_price = min(0.85, entry_price * 2.5)

        # ═══ LIVE: Place order FIRST. GTC sits in book until filled. ═══
        # CRITICAL FIX: a GTC order != a filled position. Track as PENDING
        # until the fill is confirmed. This prevents phantom positions (wle.txt bug #1).
        if not Config.is_paper():
            order_result = self._place_live_order(token_id, entry_price, cost_usd, shares)
            if not order_result:
                return None  # Order failed — DO NOT track

            order_id = order_result.get('orderID', f"live_{int(time.time())}")

            # Force-refresh balance after placing order (prevents stale-balance cascade)
            if hasattr(self, '_balance_cache_time'):
                self._balance_cache_time = 0

            # Track as PENDING — NOT a filled position yet
            pending = TrackedPosition(
                id=order_id,
                market_title=market_title,
                bucket_label=bucket_label,
                token_id=token_id,
                condition_id=condition_id,
                entry_price=entry_price,
                shares=shares,
                cost_usd=cost_usd,
                current_price=entry_price,
                current_value=shares * entry_price,
                entry_time=datetime.now(timezone.utc),
                resolution_time=resolution_time,
                strategy=strategy,
                city=city,
                slug=slug,
                peak_price=entry_price,
                stop_loss_pct=Config.STOP_LOSS_PCT,
                take_profit_price=tp_price,
                status='pending',    # <-- KEY FIX: pending, not open
            )
            self.positions.append(pending)
            self.total_trades += 1
            pos = pending  # shared tail below returns `pos`
            log.info(
                f"  ORDER PLACED  {city} {bucket_label[:35]}  "
                f"{shares:.0f}sh @ ${entry_price:.4f}  ${cost_usd:.2f}  "
                f"ID={order_id[:20]}...  [{strategy}]  STATUS: PENDING (awaiting fill)"
            )

        else:
            # PAPER MODE
            pos = TrackedPosition(
                id=f"paper_{int(time.time())}_{self.total_trades}",
                market_title=market_title,
                bucket_label=bucket_label,
                token_id=token_id,
                condition_id=condition_id,
                entry_price=entry_price,
                shares=shares,
                cost_usd=cost_usd,
                current_price=entry_price,
                current_value=shares * entry_price,
                entry_time=datetime.now(timezone.utc),
                resolution_time=resolution_time,
                strategy=strategy,
                city=city,
                slug=slug,
                peak_price=entry_price,
                stop_loss_pct=Config.STOP_LOSS_PCT,
                take_profit_price=tp_price,
            )
            self.positions.append(pos)
            self.total_trades += 1
            self.paper_balance -= cost_usd
            log.info(f"\033[92m📋 PAPER BUY: {city} {bucket_label[:30]} | "
                     f"{shares:.0f}sh @ ${entry_price:.4f} | cost=${cost_usd:.2f}\033[0m")

        # Register market context
        if slug and slug not in self.market_contexts:
            self.market_contexts[slug] = MarketContext(
                slug=slug, city=city,
                market_type='highest_temperature' if 'highest' in market_title.lower() else 'lowest_temperature',
                resolution_time=resolution_time,
                edge=edge,
            )

        self._save_state()
        log.info(f"📌 NEW: {city} {bucket_label} | {shares:.0f}sh @ ${entry_price:.4f} "
                 f"| TP=${tp_price:.2f} | SL={Config.STOP_LOSS_PCT}%")
        return pos

    def _place_live_order(self, token_id: str, price: float, size_usd: float, shares: float) -> Optional[Dict]:
        """Place a real GTC limit order on Polymarket CLOB V2."""
        try:
            from data.clob_client import ClobClient
            if not hasattr(self, '_clob_client') or self._clob_client is None:
                self._clob_client = ClobClient()
                self._clob_client.init_py_clob_client(
                    private_key=Config.POLY_PRIVATE_KEY,
                    funder=Config.get_funder_address() or None,
                    signature_type=Config.POLY_SIGNATURE_TYPE,
                )

            # Update balance/allowance with correct V2 params
            try:
                from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
                params = BalanceAllowanceParams(
                    asset_type=AssetType.COLLATERAL,
                    signature_type=Config.POLY_SIGNATURE_TYPE,
                )
                self._clob_client._py_clob_client.update_balance_allowance(params)
            except Exception:
                pass

            result = self._clob_client.place_limit_order(
                token_id=token_id,
                side='BUY',
                price=price,
                size_pusd=size_usd,
                expiration='GTC',
                neg_risk=True,  # Weather markets are ALWAYS neg_risk
            )
            return result
        except Exception as e:
            log.error(f"\033[91m❌ CLOB error: {e}\033[0m")
            return None

    def get_live_balance(self) -> Optional[float]:
        """Get REAL available balance from CLOB (correct V2 API with AssetType)."""
        now = time.time()
        if hasattr(self, '_balance_cache_time') and (now - self._balance_cache_time) < 10:
            return getattr(self, '_balance_cache_value', None)

        try:
            from data.clob_client import ClobClient
            if not hasattr(self, '_clob_client') or self._clob_client is None:
                self._clob_client = ClobClient()
                self._clob_client.init_py_clob_client(
                    private_key=Config.POLY_PRIVATE_KEY,
                    funder=Config.get_funder_address() or None,
                    signature_type=Config.POLY_SIGNATURE_TYPE,
                )

            # CORRECT V2 balance call — requires BalanceAllowanceParams with AssetType.COLLATERAL
            try:
                from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
                params = BalanceAllowanceParams(
                    asset_type=AssetType.COLLATERAL,
                    signature_type=Config.POLY_SIGNATURE_TYPE,
                )
                bal_data = self._clob_client._py_clob_client.get_balance_allowance(params)
                if bal_data:
                    raw = float(bal_data.get('balance', 0))
                    available = raw / 1_000_000  # pUSD has 6 decimals
                    self._balance_cache_value = available
                    self._balance_cache_time = now
                    return available
            except ImportError:
                pass
            except Exception as e:
                log.debug(f"V2 balance API: {e}")

            # Fallback: on-chain pUSD balance via RPC
            wallet = Config.POLY_PROXY_WALLET or Config.derive_wallet_address()
            if wallet:
                onchain = self._clob_client.get_pusd_balance_onchain(wallet)
                if onchain is not None:
                    self._balance_cache_value = onchain
                    self._balance_cache_time = now
                    return onchain
        except Exception as e:
            log.debug(f"Balance fetch error: {e}")

        return None

    def recover_positions_on_start(self):
        """On bot restart: check CLOB for open orders and existing positions."""
        if Config.is_paper():
            return

        log.info("🔄 Recovering positions from CLOB...")
        try:
            from data.clob_client import ClobClient
            if not hasattr(self, '_clob_client') or self._clob_client is None:
                self._clob_client = ClobClient()
                self._clob_client.init_py_clob_client(
                    private_key=Config.POLY_PRIVATE_KEY,
                    funder=Config.get_funder_address() or None,
                    signature_type=Config.POLY_SIGNATURE_TYPE,
                )

            # Get open orders from CLOB (needs valid API key)
            try:
                open_orders = self._clob_client._py_clob_client.get_open_orders()
                if open_orders:
                    log.info(f"  Found {len(open_orders)} open orders on CLOB")
                    for order in open_orders:
                        oid = order.get('id', '')[:12]
                        price = order.get('price', 0)
                        size = order.get('original_size', 0)
                        log.info(f"    Order {oid}... | ${price} x {size}sh")
            except Exception as ce:
                if '401' in str(ce) or 'Unauthorized' in str(ce):
                    log.warning("  CLOB API key invalid/expired — skipping order recovery.")
                    log.warning("  >> Re-derive your API creds: the bot will auto-derive if you")
                    log.warning("  >> clear POLY_API_KEY/SECRET/PASSPHRASE from .env (keep private key).")
                else:
                    log.warning(f"  CLOB order recovery skipped: {str(ce)[:80]}")

            # Get positions from data-api (PUBLIC — no API key needed, works even on 401)
            wallet = Config.POLY_PROXY_WALLET or Config.derive_wallet_address()
            if wallet:
                resp = self._session.get(
                    'https://data-api.polymarket.com/positions',
                    params={'user': wallet}, timeout=10
                )
                if resp.status_code == 200:
                    positions = resp.json()
                    weather_pos = [p for p in positions
                                   if any(w in (p.get('title','') or '').lower()
                                          for w in ['temperature', '°c', '°f'])
                                   and float(p.get('size', 0) or 0) > 0]
                    log.info(f"📋 Found {len(weather_pos)} weather positions on-chain")
                    for p in weather_pos[:5]:
                        title = (p.get('title',''))[:40]
                        size = float(p.get('size', 0))
                        price = float(p.get('avgPrice', 0))
                        cur = float(p.get('curPrice', 0))
                        log.info(f"  📈 {title} | {size:.0f}sh @ ${price:.4f} → ${cur:.4f}")
        except Exception as e:
            log.warning(f"Position recovery failed: {e}")

    def get_open_positions(self) -> List[TrackedPosition]:
        """Return FILLED open positions (NOT pending GTC orders still in the book)."""
        return [p for p in self.positions if p.status == 'open']

    def get_pending_orders(self) -> List[TrackedPosition]:
        """Return orders placed but not yet filled (GTC sitting in the book)."""
        return [p for p in self.positions if p.status == 'pending']

    def sync_pending_orders(self):
        """Poll CLOB to check if pending GTC orders have filled. Moves filled
        orders from 'pending' to 'open' status. THIS is the missing piece that
        caused the phantom position bug."""
        pending = self.get_pending_orders()
        if not pending:
            return

        for pos in pending:
            try:
                # Check if order filled via CLOB status
                if not hasattr(self, '_clob_client') or not self._clob_client:
                    continue
                status = self._clob_client.get_order_status(pos.id)
                if status:
                    filled = float(status.get('filled', 0) or status.get('size_matched', 0) or 0)
                    if filled > 0:
                        actual_price = float(status.get('price', pos.entry_price))
                        pos.shares = filled
                        pos.cost_usd = filled * actual_price
                        pos.current_price = actual_price
                        pos.current_value = filled * actual_price
                        pos.entry_price = actual_price
                        pos.status = 'open'
                        if not Config.is_paper():
                            self.paper_balance -= pos.cost_usd
                        log.info(
                            f"  FILLED  {pos.city} {pos.bucket_label[:35]}  "
                            f"{filled:.0f}sh @ ${actual_price:.4f}  "
                            f"${pos.cost_usd:.2f} cost"
                        )
            except Exception as e:
                log.debug(f"  Order sync {pos.id[:16]}...: {e}")

    def get_positions_by_status(self, status: str) -> List[TrackedPosition]:
        return [p for p in self.positions if p.status == status]

    def get_positions_by_city(self, city: str) -> List[TrackedPosition]:
        return [p for p in self.positions if p.city.lower() == city.lower()]

    def get_redeemable_positions(self) -> List[TrackedPosition]:
        return [p for p in self.positions if p.redeemable and p.status in ('open', 'won')]


    # ═══════════════════════════════════════════════════════════════
    # STOP-LOSS & TAKE-PROFIT
    # ═══════════════════════════════════════════════════════════════

    def check_risk_triggers(self) -> List[TrackedPosition]:
        """Check all open positions for stop-loss / take-profit triggers."""
        triggered = []
        for pos in self.get_open_positions():
            # Update peak price for trailing stop
            if pos.current_price > pos.peak_price:
                pos.peak_price = pos.current_price

            # TAKE-PROFIT: price rose above target
            if pos.current_price >= pos.take_profit_price:
                self._close_position(pos, pos.current_price, 'take_profit')
                triggered.append(pos)
                log.info(f"🎯 TAKE PROFIT: {pos.city} {pos.bucket_label} "
                         f"@ ${pos.current_price:.4f} (entry ${pos.entry_price:.4f}) "
                         f"PnL=${pos.pnl:+.2f}")
                continue

            # STOP-LOSS: ROI dropped below threshold
            if pos.roi_pct <= pos.stop_loss_pct:
                # For very cheap entries, don't stop-loss — hold to resolution
                # (binary markets: either $0 or $1, no point selling at $0.002)
                if pos.entry_price < 0.03:
                    continue  # hold to resolution for ultra-cheap
                self._close_position(pos, pos.current_price, 'stop_loss')
                triggered.append(pos)
                log.info(f"🛑 STOP LOSS: {pos.city} {pos.bucket_label} "
                         f"@ ${pos.current_price:.4f} (ROI={pos.roi_pct:.0f}%)")
                continue

            # TRAILING STOP (if enabled): price dropped X% from peak
            if pos.peak_price > pos.entry_price * 2:
                trail_threshold = pos.peak_price * (1 - Config.TRAILING_STOP_PCT / 100)
                if pos.current_price < trail_threshold:
                    self._close_position(pos, pos.current_price, 'trailing_stop')
                    triggered.append(pos)
                    log.info(f"📉 TRAILING STOP: {pos.city} {pos.bucket_label} "
                             f"@ ${pos.current_price:.4f} (peak=${pos.peak_price:.4f})")

        if triggered:
            self._save_state()
        return triggered

    def _close_position(self, pos: TrackedPosition, exit_price: float, reason: str):
        """Close a position (sell or resolve)."""
        pos.exit_price = exit_price
        pos.exit_time = datetime.now(timezone.utc)
        pos.exit_reason = reason
        pos.status = 'sold' if reason in ('take_profit', 'stop_loss', 'trailing_stop', 'manual') else reason

        # Calculate PnL
        if reason in ('take_profit', 'trailing_stop', 'manual'):
            # Sold at market — PnL = (exit_price - entry_price) * shares
            pos.pnl = (exit_price - pos.entry_price) * pos.shares
            pos.realized_pnl = pos.pnl
        elif reason == 'stop_loss':
            pos.pnl = (exit_price - pos.entry_price) * pos.shares
            pos.realized_pnl = pos.pnl
        elif reason == 'won':
            pos.pnl = pos.shares - pos.cost_usd
            pos.realized_pnl = pos.pnl
            self.wins += 1
        elif reason == 'lost':
            pos.pnl = -pos.cost_usd
            pos.realized_pnl = pos.pnl
            self.losses += 1

        # Credit balance in paper mode
        if Config.is_paper() and reason in ('take_profit', 'stop_loss', 'trailing_stop', 'manual'):
            self.paper_balance += pos.cost_usd + pos.pnl

        # Free market context if no more open positions for this slug
        self._maybe_free_context(pos.slug)


    # ═══════════════════════════════════════════════════════════════
    # RESOLUTION & REDEMPTION
    # ═══════════════════════════════════════════════════════════════

    def check_resolutions(self):
        """Check if any open positions have resolved."""
        open_pos = self.get_open_positions()
        if not open_pos:
            return

        for pos in open_pos:
            try:
                resp = self._session.get(
                    f"{Config.CLOB_API_URL}/price",
                    params={'token_id': pos.token_id, 'side': 'SELL'},
                    timeout=5,
                )
                if resp.status_code == 200:
                    price = float(resp.json().get('price', 0))
                    if price >= 0.99:
                        pos.redeemable = True
                        self._close_position(pos, 1.0, 'won')
                    elif price <= 0.01 and pos.is_expired:
                        self._close_position(pos, 0.0, 'lost')
                elif resp.status_code == 404 and pos.is_expired:
                    self._close_position(pos, 0.0, 'lost')
            except Exception:
                pass

        self._save_state()

    def redeem_position(self, pos: TrackedPosition) -> bool:
        """Redeem a winning position."""
        if not pos.redeemable:
            return False

        if Config.is_paper():
            payout = pos.shares * 1.0
            self.paper_balance += payout
            pos.status = 'redeemed'
            pos.pnl = payout - pos.cost_usd
            self.total_redeemed += payout
            log.info(f"💰 REDEEM: {pos.bucket_label} → +${payout:.2f}")
            self._save_state()
            return True
        else:
            try:
                from data.clob_client import ClobClient
                client = ClobClient()
                success = client.redeem_position(pos.condition_id)
                if success:
                    pos.status = 'redeemed'
                    pos.pnl = pos.shares - pos.cost_usd
                    self.total_redeemed += pos.shares
                    self._save_state()
                    return True
            except Exception as e:
                log.error(f"Redeem failed: {e}")
        return False

    def redeem_all_winning(self) -> int:
        count = 0
        for pos in self.get_redeemable_positions():
            if self.redeem_position(pos):
                count += 1
        return count

    # ═══════════════════════════════════════════════════════════════
    # PRICE UPDATES
    # ═══════════════════════════════════════════════════════════════

    def update_prices(self):
        """Batch update prices for open positions."""
        open_pos = self.get_open_positions()
        if not open_pos:
            return

        for pos in open_pos:
            try:
                resp = self._session.get(
                    f"{Config.CLOB_API_URL}/price",
                    params={'token_id': pos.token_id, 'side': 'SELL'},
                    timeout=3,
                )
                if resp.status_code == 200:
                    price = float(resp.json().get('price', pos.current_price))
                    pos.current_price = price
                    pos.current_value = pos.shares * price
                    if price > pos.peak_price:
                        pos.peak_price = price
            except Exception:
                pass

        self._save_state()


    # ═══════════════════════════════════════════════════════════════
    # CONTEXT MANAGEMENT (free memory for closed markets)
    # ═══════════════════════════════════════════════════════════════

    def _maybe_free_context(self, slug: str):
        """Free market context if no open positions remain for it."""
        if not slug:
            return
        open_for_slug = [p for p in self.positions
                         if p.slug == slug and p.status == 'open']
        if not open_for_slug and slug in self.market_contexts:
            self.market_contexts[slug].active = False
            log.debug(f"Freed context: {slug}")

    def cleanup_contexts(self):
        """Remove all inactive market contexts (frees memory)."""
        inactive = [k for k, v in self.market_contexts.items() if not v.active]
        for k in inactive:
            del self.market_contexts[k]
        if inactive:
            log.info(f"🧹 Cleaned {len(inactive)} inactive market contexts")

    def get_active_context_count(self) -> int:
        """How many active market contexts we're tracking."""
        return sum(1 for v in self.market_contexts.values() if v.active)

    # ═══════════════════════════════════════════════════════════════
    # WEEKLY MEMORY
    # ═══════════════════════════════════════════════════════════════

    def record_weekly_stats(self):
        """Snapshot current week's performance for ML memory."""
        now = datetime.now(timezone.utc)
        week_start = (now - timedelta(days=now.weekday())).strftime('%Y-%m-%d')

        # Positions closed this week
        week_ago = now - timedelta(days=7)
        this_week = [p for p in self.positions
                     if p.status != 'open' and p.exit_time and p.exit_time > week_ago]

        if not this_week:
            return

        wins = [p for p in this_week if p.status in ('won', 'redeemed')]
        losses = [p for p in this_week if p.status == 'lost']
        pnl = sum(p.pnl for p in this_week)

        # Best performing city
        city_pnl: Dict[str, float] = {}
        for p in this_week:
            city_pnl[p.city] = city_pnl.get(p.city, 0) + p.pnl
        best_city = max(city_pnl, key=city_pnl.get) if city_pnl else ''

        # Best strategy
        strat_pnl: Dict[str, float] = {}
        for p in this_week:
            strat_pnl[p.strategy] = strat_pnl.get(p.strategy, 0) + p.pnl
        best_strat = max(strat_pnl, key=strat_pnl.get) if strat_pnl else ''

        stats = WeeklyStats(
            week_start=week_start,
            trades=len(this_week),
            wins=len(wins),
            losses=len(losses),
            pnl=pnl,
            roi_pct=(pnl / max(0.01, self.total_deposited)) * 100,
            best_city=best_city,
            best_strategy=best_strat,
            avg_entry_price=sum(p.entry_price for p in this_week) / max(1, len(this_week)),
        )
        self.weekly_history.append(stats)
        self._save_weekly()
        log.info(f"📅 Weekly: {stats.trades} trades | {stats.wins}W/{stats.losses}L | PnL=${stats.pnl:+.2f}")

    def get_weekly_summary(self) -> str:
        """Get compact weekly summary for ML context."""
        if not self.weekly_history:
            return "No weekly history yet."
        recent = self.weekly_history[-4:]  # last 4 weeks
        lines = []
        for w in recent:
            lines.append(
                f"W{w.week_start}: {w.trades}T {w.wins}W {w.losses}L "
                f"PnL=${w.pnl:+.1f} best={w.best_city}"
            )
        return " | ".join(lines)


    # ═══════════════════════════════════════════════════════════════
    # STATISTICS (per-position + aggregate)
    # ═══════════════════════════════════════════════════════════════

    def get_stats(self) -> Dict:
        """Comprehensive stats."""
        open_pos = self.get_open_positions()
        closed = [p for p in self.positions if p.status != 'open']
        total_closed = self.wins + self.losses
        win_rate = (self.wins / max(1, total_closed)) * 100

        return {
            'mode': 'PAPER' if Config.is_paper() else 'LIVE',
            'balance': self.get_balance(),
            'portfolio_value': self.get_portfolio_value(),
            'total_pnl': self.get_total_pnl(),
            'roi_pct': (self.get_total_pnl() / max(0.01, self.total_deposited)) * 100,
            'total_trades': self.total_trades,
            'open_positions': len(open_pos),
            'wins': self.wins,
            'losses': self.losses,
            'win_rate': win_rate,
            'total_redeemed': self.total_redeemed,
            'avg_entry_price': (sum(p.entry_price for p in self.positions) /
                               max(1, len(self.positions))),
            'active_contexts': self.get_active_context_count(),
            'weekly_summary': self.get_weekly_summary(),
        }

    def get_position_detail(self, pos_id: str) -> Optional[Dict]:
        """Get detailed info for a single position."""
        pos = next((p for p in self.positions if p.id == pos_id), None)
        if not pos:
            return None
        return {
            'id': pos.id, 'city': pos.city, 'bucket': pos.bucket_label,
            'entry': pos.entry_price, 'current': pos.current_price,
            'shares': pos.shares, 'cost': pos.cost_usd,
            'pnl': pos.unrealized_pnl, 'roi_pct': pos.roi_pct,
            'status': pos.status, 'strategy': pos.strategy,
            'hold_hours': pos.hold_hours,
            'take_profit': pos.take_profit_price,
            'stop_loss_pct': pos.stop_loss_pct,
            'peak_price': pos.peak_price,
            'resolution': pos.resolution_time.isoformat() if pos.resolution_time else None,
        }

    def get_per_city_stats(self) -> Dict[str, Dict]:
        """PnL breakdown by city."""
        stats: Dict[str, Dict] = {}
        for p in self.positions:
            if p.city not in stats:
                stats[p.city] = {'trades': 0, 'wins': 0, 'pnl': 0.0}
            stats[p.city]['trades'] += 1
            if p.status in ('won', 'redeemed'):
                stats[p.city]['wins'] += 1
            stats[p.city]['pnl'] += p.pnl if p.status != 'open' else p.unrealized_pnl
        return stats

    def get_per_strategy_stats(self) -> Dict[str, Dict]:
        """PnL breakdown by strategy."""
        stats: Dict[str, Dict] = {}
        for p in self.positions:
            if p.strategy not in stats:
                stats[p.strategy] = {'trades': 0, 'wins': 0, 'pnl': 0.0}
            stats[p.strategy]['trades'] += 1
            if p.status in ('won', 'redeemed'):
                stats[p.strategy]['wins'] += 1
            stats[p.strategy]['pnl'] += p.pnl if p.status != 'open' else p.unrealized_pnl
        return stats

    def record_performance_snapshot(self) -> str:
        """Append a timestamped performance snapshot to disk and log a one-line
        summary. This is our OWN-bot simulation record (per-strategy/per-city
        win-rate, PnL, ROI over time) — distinct from the offline backtest, so we
        can see how the bot actually performs as it runs. Returns the summary."""
        import json
        stats = self.get_stats()
        by_strategy = self.get_per_strategy_stats()
        snap = {
            'ts': datetime.now(timezone.utc).isoformat(),
            'mode': stats['mode'],
            'balance': round(stats['balance'], 2),
            'portfolio_value': round(stats['portfolio_value'], 2),
            'total_pnl': round(stats['total_pnl'], 2),
            'roi_pct': round(stats['roi_pct'], 1),
            'trades': stats['total_trades'],
            'open': stats['open_positions'],
            'wins': stats['wins'], 'losses': stats['losses'],
            'win_rate': round(stats['win_rate'], 1),
            'by_strategy': by_strategy,
            'by_city': self.get_per_city_stats(),
        }
        try:
            os.makedirs('backtest/results', exist_ok=True)
            path = 'backtest/results/paper_performance.json'
            history = []
            if os.path.exists(path):
                try:
                    with open(path) as f:
                        history = json.load(f)
                except Exception:
                    history = []
            history.append(snap)
            history = history[-500:]  # bound file size
            with open(path, 'w') as f:
                json.dump(history, f, indent=2)
        except Exception as e:
            log.debug(f"perf snapshot save failed: {e}")

        parts = []
        for strat, s in by_strategy.items():
            wr = (s['wins'] / max(1, s['trades'])) * 100
            parts.append(f"{strat}:{s['trades']}t {wr:.0f}%WR ${s['pnl']:+.2f}")
        summary = (f"📈 PERF [{snap['mode']}] bal=${snap['balance']:.2f} "
                   f"PnL=${snap['total_pnl']:+.2f} ({snap['roi_pct']:+.0f}%) "
                   f"WR={snap['win_rate']:.0f}% {snap['wins']}W/{snap['losses']}L"
                   + (" | " + " | ".join(parts) if parts else ""))
        log.info(summary)
        return summary


    # ═══════════════════════════════════════════════════════════════
    # PERSISTENCE
    # ═══════════════════════════════════════════════════════════════

    def _save_state(self):
        """Save positions to disk."""
        try:
            os.makedirs('data', exist_ok=True)
            state = {
                'paper_balance': self.paper_balance,
                'total_deposited': self.total_deposited,
                'total_redeemed': self.total_redeemed,
                'total_trades': self.total_trades,
                'wins': self.wins,
                'losses': self.losses,
                'positions': [self._pos_to_dict(p) for p in self.positions[-500:]],
                'market_contexts': {
                    k: {'slug': v.slug, 'city': v.city, 'active': v.active,
                         'edge': v.edge, 'forecast_temp': v.forecast_temp}
                    for k, v in self.market_contexts.items() if v.active
                },
                'last_updated': datetime.now(timezone.utc).isoformat(),
            }
            with open(self._state_file, 'w') as f:
                json.dump(state, f, separators=(',', ':'))
        except Exception as e:
            log.warning(f"Save failed: {e}")

    def _pos_to_dict(self, p: TrackedPosition) -> Dict:
        return {
            'id': p.id, 'mt': p.market_title[:60], 'bl': p.bucket_label,
            'tid': p.token_id, 'cid': p.condition_id,
            'ep': p.entry_price, 'sh': p.shares, 'cu': p.cost_usd,
            'cp': p.current_price, 'cv': p.current_value,
            'et': p.entry_time.isoformat(), 'st': p.strategy,
            'rt': p.resolution_time.isoformat() if p.resolution_time else None,
            'status': p.status, 'pnl': p.pnl, 'rpnl': p.realized_pnl,
            'rdm': p.redeemable, 'city': p.city, 'slug': p.slug,
            'pp': p.peak_price, 'sl': p.stop_loss_pct, 'tp': p.take_profit_price,
            'xp': p.exit_price, 'xr': p.exit_reason,
            'xt': p.exit_time.isoformat() if p.exit_time else None,
        }

    def _load_state(self):
        """Load from disk."""
        try:
            if not os.path.exists(self._state_file):
                return
            with open(self._state_file, 'r') as f:
                state = json.load(f)
            self.paper_balance = state.get('paper_balance', Config.STARTING_BALANCE)
            self.total_deposited = state.get('total_deposited', Config.STARTING_BALANCE)
            self.total_redeemed = state.get('total_redeemed', 0)
            self.total_trades = state.get('total_trades', 0)
            self.wins = state.get('wins', 0)
            self.losses = state.get('losses', 0)
            for pd in state.get('positions', []):
                try:
                    pos = TrackedPosition(
                        id=pd['id'],
                        market_title=pd.get('mt', pd.get('market_title', '')),
                        bucket_label=pd.get('bl', pd.get('bucket_label', '')),
                        token_id=pd.get('tid', pd.get('token_id', '')),
                        condition_id=pd.get('cid', pd.get('condition_id', '')),
                        entry_price=pd.get('ep', pd.get('entry_price', 0)),
                        shares=pd.get('sh', pd.get('shares', 0)),
                        cost_usd=pd.get('cu', pd.get('cost_usd', 0)),
                        current_price=pd.get('cp', pd.get('current_price', 0)),
                        current_value=pd.get('cv', pd.get('current_value', 0)),
                        entry_time=datetime.fromisoformat(pd.get('et', pd.get('entry_time', '2026-01-01T00:00:00+00:00'))),
                        resolution_time=datetime.fromisoformat(pd['rt']) if pd.get('rt') else None,
                        strategy=pd.get('st', pd.get('strategy', '')),
                        status=pd.get('status', 'open'),
                        pnl=pd.get('pnl', 0),
                        realized_pnl=pd.get('rpnl', 0),
                        redeemable=pd.get('rdm', pd.get('redeemable', False)),
                        city=pd.get('city', ''),
                        slug=pd.get('slug', ''),
                        peak_price=pd.get('pp', pd.get('peak_price', 0)),
                        stop_loss_pct=pd.get('sl', -80),
                        take_profit_price=pd.get('tp', 0.50),
                        exit_price=pd.get('xp'),
                        exit_reason=pd.get('xr', ''),
                        exit_time=datetime.fromisoformat(pd['xt']) if pd.get('xt') else None,
                    )
                    self.positions.append(pos)
                except Exception:
                    continue
            log.info(f"Loaded {len(self.positions)} positions")
        except Exception as e:
            log.debug(f"No state: {e}")

    def _save_weekly(self):
        """Save weekly history."""
        try:
            os.makedirs('data', exist_ok=True)
            data = [
                {'ws': w.week_start, 't': w.trades, 'w': w.wins,
                 'l': w.losses, 'pnl': w.pnl, 'bc': w.best_city}
                for w in self.weekly_history[-52:]  # keep 1 year
            ]
            with open(self._weekly_file, 'w') as f:
                json.dump(data, f, separators=(',', ':'))
        except Exception:
            pass

    def _load_weekly(self):
        """Load weekly history."""
        try:
            if not os.path.exists(self._weekly_file):
                return
            with open(self._weekly_file, 'r') as f:
                data = json.load(f)
            for d in data:
                self.weekly_history.append(WeeklyStats(
                    week_start=d['ws'], trades=d['t'], wins=d['w'],
                    losses=d['l'], pnl=d['pnl'], best_city=d.get('bc', ''),
                ))
        except Exception:
            pass
