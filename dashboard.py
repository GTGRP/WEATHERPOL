"""
Weather Sniper Bot — Main Dashboard / Trading Loop

Flow:
1. Scan Polymarket for active weather markets (slug-based, confirmed pattern)
2. For each market: fetch multi-source forecasts
3. Run probability engine → find mispriced buckets
4. Run strategies (LateObserved primary + QuickFlip + optional PeakBasket/Confident) → signals
5. Execute trades (paper or live)
6. Monitor positions, check resolutions, redeem winners
7. Send Telegram notifications

Usage:
    python dashboard.py              # paper mode (default)
    python dashboard.py --live       # live trading
    python dashboard.py --once       # single scan then exit
    python dashboard.py --status     # print status and exit
"""

import sys
import os
import time
import argparse
from datetime import datetime, timezone

from config import Config
from logger import log
from data.weather_fetcher import WeatherFetcher, get_city_coords, CITY_COORDS
from data.probability_engine import ProbabilityEngine
from data.market_scanner import MarketScanner, MARKET_CITIES
from data.observed_weather import ObservedWeather
from strategies.peak_basket import PeakBasketStrategy
from strategies.confident_strategy import ConfidentStrategy
from strategies.late_observed_temp import LateObservedTempStrategy
from strategies.quick_flip import QuickFlipStrategy
from data.stability import StabilityEngine
from data.liquidity_guard import LiquidityGuard
from data.clob_client import ClobClient
from data.market_timing import outcome_decided, city_local_now
from data.resolution_rules import StationResolver
from trading.position_manager import PositionManager
from bot.telegram_ui import TelegramBot
from bot import settings_store
from ml.decision_engine import MLDecisionEngine
try:
    from ml.resolution_verifier import ResolutionVerifier
except Exception:  # keep the bot runnable even if the verifier import fails
    ResolutionVerifier = None


class WeatherBot:
    """Main weather trading bot with full dashboard."""

    def __init__(self):
        settings_store.load_into_config()   # apply persisted runtime overrides (Telegram /settings)
        self.fetcher = WeatherFetcher()
        self.engine = ProbabilityEngine()
        self.scanner = MarketScanner()
        self.peak_basket = PeakBasketStrategy()
        self.confident = ConfidentStrategy()
        # PRIMARY strategy: trade the observed/locked daily extreme (YES + NO).
        self.late_observed = LateObservedTempStrategy()
        # Forecast-change arbitrage: enter a freshly mispriced bucket before the
        # book digests new model data; flips quick or holds if structural.
        self.quick_flip = QuickFlipStrategy()
        self.observed = ObservedWeather()
        self.stability_engine = StabilityEngine()
        # Stability GRADE + liquidity guard (applied across ALL strategies)
        self.liquidity = LiquidityGuard()
        self.clob = ClobClient()          # read-only order-book fetches (no auth needed)
        self._book_cache = {}             # token_id -> (timestamp, book) — short TTL
        self.pm = PositionManager()
        self.ml = MLDecisionEngine()
        # Resolution-station verification: confirm/adjust the EXACT airport each
        # market settles on. The deterministic check is free; the verifier LLM
        # is only consulted when a station looks ambiguous or different.
        self.station_resolver = StationResolver()
        self.resolution_verifier = ResolutionVerifier() if ResolutionVerifier else None
        self.telegram = TelegramBot(position_manager=self.pm, scanner=self.scanner)
        self.scan_count = 0
        self.signals_generated = 0
        self.trades_placed = 0
        self._last_resolution_check = 0
        self._last_daily_summary = ''
        self._last_weekly_record = ''

    def run_once(self):
        """Run a single scan cycle."""
        self.scan_count += 1
        now = datetime.now(timezone.utc)
        log.info(f"\n{'═'*60}")
        log.info(f"🔍 SCAN #{self.scan_count} — {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        log.info(f"{'═'*60}")

        # Step 0: Sync pending orders (GTC fills), check resolutions, redeem
        if not Config.is_paper():
            self.pm.sync_pending_orders()

        if time.time() - self._last_resolution_check > 300:
            self._check_resolutions()
            self.pm.check_risk_triggers()  # stop-loss / take-profit
            self.pm.cleanup_contexts()     # free closed market memory
            self.pm.record_performance_snapshot()  # log + persist our own paper/live performance
            self._last_resolution_check = time.time()

        # Weekly memory recording
        week_str = now.strftime('%Y-W%W')
        if week_str != self._last_weekly_record and now.weekday() == 0:
            self.pm.record_weekly_stats()
            self._last_weekly_record = week_str

        # Step 1: Discover weather markets
        markets = self.scanner.scan_weather_markets(days_ahead=Config.SCAN_DAYS_AHEAD)
        if not markets:
            log.info("No active weather markets found. Waiting...")
            return

        log.info(f"Found {len(markets)} active weather markets")

        # Balance early-out: if free balance can't cover even a minimum order,
        # don't bother scanning for buys this cycle — wait for open positions to
        # sell/resolve and free up capital (rechecked next scan; live re-queries CLOB).
        free_balance = self.pm.get_balance()
        if not Config.TRADING_ENABLED:
            log.info("⏸  Trading DISABLED (/stop) — monitoring & resolving only, no new buys")
        elif free_balance < Config.MIN_ORDER_SIZE:
            log.info(f"⏸  Balance ${free_balance:.2f} < min order ${Config.MIN_ORDER_SIZE:.2f} — "
                     f"skipping buys, waiting for {len(self.pm.get_open_positions())} positions to resolve")
        else:
            # Step 2: Evaluate each market
            for market in markets:
                try:
                    self._evaluate_market(market)
                except Exception as e:
                    log.error(f"Error evaluating {market.title[:40]}: {e}")
                    continue

        # Step 3: Update prices for open positions
        self.pm.update_prices()

        # Step 4: Print dashboard
        self._print_dashboard()

        # Step 5: Daily summary
        today_str = now.strftime('%Y-%m-%d')
        if today_str != self._last_daily_summary and now.hour >= 22:
            self.telegram.send_daily_summary()
            self._last_daily_summary = today_str

    # -----------------------------------------------------------------
    # STABILITY GRADE + LIQUIDITY — applied across every strategy
    # -----------------------------------------------------------------
    def _grade_multiplier(self, grade: float) -> float:
        """Map a stability grade (0..1) to a size multiplier.

        Higher grade = more stable weather = bigger size. Linear between
        GRADE_SIZE_MIN_MULT (grade 0) and GRADE_SIZE_MAX_MULT (grade 1).
        """
        if not Config.GRADE_SIZING_ENABLED:
            return 1.0
        g = max(0.0, min(1.0, grade))
        lo, hi = Config.GRADE_SIZE_MIN_MULT, Config.GRADE_SIZE_MAX_MULT
        return lo + (hi - lo) * g

    def _resolve_station(self, market, city, lat, lon):
        """Confirm/adjust the EXACT resolution station for a market.

        Returns (lat, lon, ok). ok=False means the caller should SKIP this
        market (rules name a station we can't verify and skip-on-unknown is on).
        Logs 📍 confirmed / ⚠️ adjusted / ⛔ skip.
        """
        if not Config.RESOLUTION_VERIFY_ENABLED:
            return lat, lon, True
        try:
            rs = self.station_resolver.resolve(
                city, getattr(market, 'raw', None),
                ml_engine=self.resolution_verifier,
                verify_enabled=Config.RESOLUTION_VERIFY_ENABLED,
                min_conf=Config.RESOLUTION_VERIFY_MIN_CONF,
                skip_on_unknown=Config.RESOLUTION_SKIP_ON_UNKNOWN,
            )
        except Exception as e:
            log.debug(f"station resolve failed {city}: {e}")
            return lat, lon, True
        if rs is None:
            return lat, lon, True
        if rs.source == 'skip' or rs.coords is None:
            log.info(f"   ⛔ STATION {city} — {rs.reason} — skip")
            return lat, lon, False
        if rs.source == 'adjusted-ml':
            log.info(f"   ⚠️  STATION {city} — adjusted to {rs.icao or '?'} "
                     f"({rs.station_name or '?'}) — {rs.reason}")
            return rs.coords[0], rs.coords[1], True
        # confirmed / default: keep the resolver's coords (same as hardcoded).
        log.debug(f"   📍 STATION {city} — {rs.source} {rs.icao or 'hardcoded'} ({rs.reason})")
        if rs.coords:
            return rs.coords[0], rs.coords[1], True
        return lat, lon, True

    def _get_book(self, token_id: str):
        """Fetch the live order book for a token, with a short TTL cache.

        Only called when a signal actually fires, so multiple legs/strategies
        on the same token share one network call. Returns None on failure.
        """
        if not token_id:
            return None
        ttl = Config.LIQUIDITY_BOOK_CACHE_SECONDS
        cached = self._book_cache.get(token_id)
        if cached and (time.time() - cached[0]) < ttl:
            return cached[1]
        book = None
        try:
            book = self.clob.get_orderbook(token_id)
        except Exception as e:
            log.debug(f"orderbook fetch failed {token_id[:10]}: {e}")
        self._book_cache[token_id] = (time.time(), book)
        return book

    def _place(self, *, token_id, condition_id, entry_price, base_size_usd,
               market_title, bucket_label, strategy, city, slug,
               resolution_time, edge=0.0, grade=None, hold_hint=False,
               early_exit_price=None, apply_grade_size=True,
               reason='', lock_confidence=0.0, signal=''):
        """Single placement path for ALL strategies.

        Applies the stability GRADE (gate + size multiplier), the LIQUIDITY
        guard (maker-at-bid entry, skip thin/wide books), then opens the
        position and sets the grade-based exit. Returns the position or None.

        `apply_grade_size=False` skips the size multiplier (used by the
        stability basket, which already scales its legs by the score) while
        still enforcing the grade gate, liquidity guard, and exit rule.

        `reason` / `lock_confidence` / `signal` are observability metadata
        carried into the position so paper logs record WHY each buy fired.
        """
        # Hard safety: never place when trading is disabled (toggled mid-scan via /stop).
        if not Config.TRADING_ENABLED:
            return None
        if grade is None:
            grade = Config.GRADE_NEUTRAL
        if early_exit_price is None:
            early_exit_price = Config.STABILITY_EARLY_EXIT_PRICE

        # -- Grade gate: skip trades on unpredictable city-days --
        if Config.GRADE_SIZING_ENABLED and grade < Config.GRADE_MIN_TO_TRADE:
            log.info(f"   ⏭️  GRADE SKIP {strategy}:{bucket_label[:18]} — grade {grade:.2f} < {Config.GRADE_MIN_TO_TRADE}")
            return None

        # -- PRICE FLOORS --
        # (1) HARD DUST FLOOR (all strategies): below ABS_PRICE_FLOOR a leg can't
        #     even rest on the 1c-tick venue — true junk, always reject.
        abs_floor = getattr(Config, 'ABS_PRICE_FLOOR', 0.01)
        if entry_price < abs_floor:
            log.info(f"   ⏭️  DUST {strategy}:{bucket_label[:18]} — ask ${entry_price:.4f} < ${abs_floor:.2f}")
            return None
        # (2) SELLABILITY FLOOR (early-exit strategies only): a leg that plans to
        #     SELL before resolution needs a real bid (~5c) to exit into. HOLD-to-
        #     resolution legs bypass this — an EV+ 2-4c locked tail never needs a
        #     bid and is exactly the reference 90%-WR wallet's cheap-tail edge.
        if (not hold_hint) and entry_price < Config.MIN_ENTRY_PRICE:
            log.info(f"   ⏭️  PRICE FLOOR {strategy}:{bucket_label[:18]} — ask ${entry_price:.4f} < ${Config.MIN_ENTRY_PRICE:.2f} (no exit bid)")
            return None

        # -- Grade sizing --
        mult = self._grade_multiplier(grade) if apply_grade_size else 1.0
        size_usd = base_size_usd * mult

        # -- Liquidity AWARENESS (adapt, don't block) --
        # Read the book and adjust: enter MAKER at best_bid, trim size on
        # thin/wide books, and force hold-to-resolution when it's too thin to
        # exit. Only skips if LIQUIDITY_STRICT_BLOCK is explicitly enabled.
        fill_price = entry_price
        if Config.LIQUIDITY_GUARD_ENABLED:
            book = self._get_book(token_id)
            if book and book.get('best_bid', 0) > 0:
                bid_depth = book['bids'][0][1] if book.get('bids') else 0.0
                ask_depth = book['asks'][0][1] if book.get('asks') else 0.0
                chk = self.liquidity.can_enter(
                    market_price=entry_price,
                    best_bid=book['best_bid'], best_ask=book['best_ask'],
                    edge=edge, bid_depth=bid_depth, ask_depth=ask_depth,
                )
                # MAKER entry: post at the bid (cheaper, 0% fee, earn the spread).
                fill_price = book['best_bid']
                if not chk.passed:
                    if Config.LIQUIDITY_STRICT_BLOCK:
                        log.info(f"   ⏭️  LIQ SKIP {strategy}:{bucket_label[:18]} — {chk.reason}")
                        return None
                    # Aware mode: trade smaller + hold to resolution (can't rely on an exit).
                    size_usd *= Config.LIQUIDITY_THIN_SIZE_MULT
                    hold_hint = True
                    log.info(f"   💧 LIQ THIN {strategy}:{bucket_label[:18]} — {chk.reason} "
                             f"→ size x{Config.LIQUIDITY_THIN_SIZE_MULT} + hold, maker@{fill_price:.3f}")
            else:
                # No book / no bid: stay at scan price, trim size, and hold.
                if Config.LIQUIDITY_STRICT_BLOCK:
                    log.info(f"   ⏭️  LIQ SKIP {strategy}:{bucket_label[:18]} — no order book")
                    return None
                size_usd *= Config.LIQUIDITY_THIN_SIZE_MULT
                hold_hint = True
                log.info(f"   💧 LIQ NOBOOK {strategy}:{bucket_label[:18]} — no bid → size x{Config.LIQUIDITY_THIN_SIZE_MULT} + hold")

        if fill_price <= 0:
            return None
        # Maker re-pricing must respect the same floors. Dust is always rejected;
        # the sellability floor again applies only to early-exit legs (hold legs,
        # including anything forced to hold by a thin book above, may rest cheap).
        if fill_price < abs_floor:
            log.info(f"   ⏭️  DUST {strategy}:{bucket_label[:18]} — maker fill ${fill_price:.4f} < ${abs_floor:.2f}")
            return None
        if (not hold_hint) and fill_price < Config.MIN_ENTRY_PRICE:
            log.info(f"   ⏭️  PRICE FLOOR {strategy}:{bucket_label[:18]} — maker fill ${fill_price:.4f} < ${Config.MIN_ENTRY_PRICE:.2f} (no exit bid)")
            return None
        shares = size_usd / fill_price

        pos = self.pm.add_position(
            token_id=token_id,
            condition_id=condition_id,
            entry_price=fill_price,
            shares=shares,
            cost_usd=size_usd,
            market_title=market_title,
            bucket_label=bucket_label,
            strategy=strategy,
            city=city,
            slug=slug,
            resolution_time=resolution_time,
            edge=edge,
            reason=reason,
            grade=grade,
            lock_confidence=lock_confidence,
            signal=signal or strategy,
        )
        if pos:
            # Grade-based exit: stable/rain → hold to resolution; else take profit early.
            if hold_hint:
                pos.take_profit_price = 0.99
                pos.exit_reason = 'hold_grade'
            else:
                pos.take_profit_price = early_exit_price
                pos.exit_reason = 'grade_early_exit'
        return pos

    def _evaluate_market(self, market):
        """Evaluate a single weather market for trading opportunities."""
        city = market.city
        city_lower = city.lower().replace(' ', '')

        # Get coordinates
        coords = get_city_coords(city)
        if not coords:
            # Try without spaces and common variants
            for key, val in CITY_COORDS.items():
                if city_lower in key.replace(' ', '') or key.replace(' ', '') in city_lower:
                    coords = val
                    break
        if not coords:
            log.debug(f"No coordinates for: {city}")
            return

        lat, lon = coords

        # -- RESOLUTION-STATION VERIFICATION --
        # Forecast/observe the EXACT airport this market settles on. If the rules
        # name a different station we adjust coordinates (or skip when unknown).
        lat, lon, station_ok = self._resolve_station(market, city, lat, lon)
        if not station_ok:
            return

        # -- HIGHEST-TEMP-ONLY GATE (optional) --
        # When enabled, only trade daily-high markets. Disabled by default now
        # that the observed strategy locks the low overnight and the high in the
        # afternoon, and trades the NO side either way.
        if Config.HIGHEST_TEMP_ONLY and 'highest' not in (market.market_type or '').lower():
            log.debug(f"   ⏭️  SKIP {city} {market.market_type} — highest-temp only")
            return

        # -- OUTCOME-DECIDED GATE (timezone-aware, PER-STRATEGY) --
        # A "decided/locked" day KILLS the edge for forecast-guessing strategies
        # but IS the edge for observation strategies (the recorded extreme is a
        # hard floor/ceiling while the book still prices stale forecast doubt).
        # So this is NO LONGER a blanket skip. We only HARD-skip when the city's
        # local day is FULLY OVER (value recorded, just awaiting UMA payout —
        # nothing left for anyone). Otherwise we flag a LOCK WINDOW and let each
        # strategy opt in below via its own *_TRADE_DECIDED config flag.
        # (This was the bug behind "signals but zero trades": the old gate
        # returned here and LateObserved never even ran.)
        in_lock_window = False
        if Config.SKIP_DECIDED_MARKETS:
            try:
                decided, why = outcome_decided(
                    market.market_type, market.measurement_date, lat, lon
                )
            except Exception as e:
                decided, why = False, ''
                log.debug(f"decided-gate failed {city}: {e}")
            if decided:
                fully_over = False
                try:
                    if market.measurement_date is not None:
                        fully_over = (city_local_now(lat, lon).date()
                                      > market.measurement_date.date())
                except Exception as e:
                    log.debug(f"day-over check failed {city}: {e}")
                if fully_over:
                    log.info(f"   ⛔ OVER {city} {market.market_type.split('_')[0]} "
                             f"{market.measurement_date:%b-%d} — {why} — skip (day fully over, awaiting UMA)")
                    return
                in_lock_window = True
                log.info(f"   🔓 LOCK WINDOW {city} {market.market_type.split('_')[0]} "
                         f"{market.measurement_date:%b-%d} — {why} — observation strategies only")

        # Fetch forecasts
        target_time = market.resolution_time
        forecasts = self.fetcher.fetch_all(lat, lon, city, target_time)
        if not forecasts:
            log.debug(f"No forecasts for {city}")
            return

        # Build bucket list from outcomes (carry BOTH legs: YES + NO).
        buckets = []
        token_ids = {}
        no_token_ids = {}
        condition_ids = {}
        no_prices = {}
        for outcome in market.outcomes:
            label = outcome['label']
            lo = outcome.get('bucket_low', float('-inf'))
            hi = outcome.get('bucket_high', float('inf'))
            buckets.append((label, lo, hi))
            token_ids[label] = outcome.get('token_id', '')
            no_token_ids[label] = outcome.get('token_id_no', '')
            no_prices[label] = outcome.get('price_no', max(0.0, 1.0 - outcome.get('price', 0.5)))
            condition_ids[label] = outcome.get('condition_id', '')

        if not buckets:
            return

        # Run probability engine
        bucket_probs = self.engine.estimate_bucket_probabilities(
            forecasts, buckets, target_time
        )

        # Get market prices (from scan data, already fetched)
        market_prices = {o['label']: o.get('price', 0.5) for o in market.outcomes}

        balance = self.pm.get_balance()

        # -- STABILITY GRADE (computed ONCE, applied to every strategy below) --
        # Stability is a GRADE, not a strategy: it scales position size and
        # decides hold-to-resolution vs early-exit for ALL strategies.
        stab = None
        if Config.STABILITY_ENABLED:
            try:
                stab = self.stability_engine.assess(
                    city, market.resolution_time, lat=lat, lon=lon
                )
            except Exception as e:
                log.debug(f"Stability assess failed {city}: {e}")
        grade = stab.score if stab else Config.GRADE_NEUTRAL
        # Hold to resolution when weather is stable, or when rain pins the high.
        hold_hint = bool(stab and (stab.hold_to_resolution() or stab.rain_block))
        early_exit_price = Config.STABILITY_EARLY_EXIT_PRICE
        if stab:
            log.info(
                f"   📐 GRADE {city}: {grade:.2f} ({stab.trend}"
                f"{', rain-block' if stab.rain_block else ''}) "
                f"× size={self._grade_multiplier(grade):.2f} | "
                f"{'HOLD' if hold_hint else f'exit@{early_exit_price:.2f}'}"
            )

        # ------------------------------------------------------
        # LATE OBSERVED-TEMPERATURE — THE PRIMARY strategy.
        # Once the local day's peak/trough is locked, the observed extreme is a
        # hard floor/ceiling on settlement. YES the locked bucket and NO the
        # buckets the observed data has ruled out, with fee-aware gating.
        # Trades in the LOCK WINDOW by default (that's its whole edge).
        # ------------------------------------------------------
        if Config.LATE_OBSERVED_ENABLED and (
            not in_lock_window or getattr(Config, 'LATE_OBSERVED_TRADE_DECIDED', True)
        ):
            mode = 'low' if 'low' in (market.market_type or '').lower() else 'high'
            observed_state = None
            try:
                observed_state = self.observed.get_state(
                    lat, lon, market.measurement_date, mode
                )
            except Exception as e:
                log.debug(f"observed-state fetch failed {city}: {e}")
            if observed_state is not None:
                obs_signals = self.late_observed.evaluate(
                    market.title, buckets, market_prices, token_ids, balance,
                    city, observed_state,
                    no_prices=no_prices, no_token_ids=no_token_ids,
                    grade=grade, market_type=market.market_type,
                )
                for sig in obs_signals:
                    self.signals_generated += 1
                    log.info(
                        f"   🌡️  OBSERVED {city} {mode} | lock={sig.lock_confidence:.0%} "
                        f"obs={sig.observed_extreme_c:.1f}°C | {len(sig.legs)} legs | {sig.reason}"
                    )
                    for leg in sig.legs:
                        side = leg.side.lower()
                        token = leg.token_id
                        if not token:
                            log.debug(f"      skip {side} {leg.bucket_label[:18]}: no token id")
                            continue
                        pos = self._place(
                            token_id=token,
                            condition_id=condition_ids.get(leg.bucket_label, ''),
                            entry_price=leg.price,
                            base_size_usd=leg.size_usd,
                            market_title=market.title,
                            bucket_label=f"{leg.side} {leg.bucket_label}",
                            strategy=f'late_observed_{side}',
                            city=city,
                            slug=market.slug,
                            resolution_time=market.resolution_time,
                            edge=leg.edge,
                            grade=grade,
                            hold_hint=True,  # observed edge realizes at resolution
                            early_exit_price=early_exit_price,
                            apply_grade_size=False,  # strategy already Kelly-sizes
                            reason=sig.reason,
                            lock_confidence=sig.lock_confidence,
                            signal=f'late_observed_{side}',
                        )
                        if pos:
                            self.trades_placed += 1
                            self.telegram.notify_trade(
                                'BUY', f"{leg.side} {leg.bucket_label}", pos.entry_price,
                                pos.cost_usd, pos.shares, f'late_observed_{side}',
                                edge=leg.edge, city=city,
                            )

        # ------------------------------------------------------
        # QUICK FLIP — forecast-change arbitrage. When fresh model data shifts a
        # bucket's probability, enter BEFORE the book adjusts and flip on the
        # correction (or hold if the move is structural). High win-rate, fast
        # exit. Also runs in the lock window (the forecast-change edge holds).
        # ------------------------------------------------------
        if getattr(Config, 'QUICK_FLIP_ENABLED', False) and (
            not in_lock_window or getattr(Config, 'QUICK_FLIP_TRADE_DECIDED', True)
        ):
            qf_mode = 'low' if 'low' in (market.market_type or '').lower() else 'highest'
            try:
                flip_signals = self.quick_flip.evaluate(
                    market.title, bucket_probs, market_prices, market_prices,
                    token_ids, balance, city=city, market_type=qf_mode,
                )
            except Exception as e:
                flip_signals = []
                log.debug(f"quick_flip eval failed {city}: {e}")
            for fsig in flip_signals:
                self.signals_generated += 1
                log.info(
                    f"   ⚡ FLIP {city} | {fsig.bucket_label[:25]} @ ${fsig.entry_price:.3f} "
                    f"→ ${fsig.target_price:.3f} ({fsig.expected_roi_pct:.0f}% ROI, "
                    f"{fsig.expected_hold_minutes}m) | {fsig.entry_reason}"
                )
                pos = self._place(
                    token_id=fsig.token_id,
                    condition_id=condition_ids.get(fsig.bucket_label, ''),
                    entry_price=fsig.entry_price,
                    base_size_usd=fsig.size_usd,
                    market_title=market.title,
                    bucket_label=fsig.bucket_label,
                    strategy='quick_flip',
                    city=city,
                    slug=market.slug,
                    resolution_time=market.resolution_time,
                    edge=fsig.expected_roi_pct / 100.0,
                    grade=grade,
                    hold_hint=False,            # quick-flip EXITS into the correction
                    early_exit_price=fsig.target_price,
                    apply_grade_size=False,     # quick-flip sizes itself
                    reason=fsig.entry_reason,
                    lock_confidence=fsig.confidence,
                    signal='quick_flip',
                )
                if pos:
                    self.trades_placed += 1
                    self.telegram.notify_trade(
                        'BUY', fsig.bucket_label, pos.entry_price,
                        pos.cost_usd, pos.shares, 'quick_flip',
                        edge=fsig.expected_roi_pct / 100.0, city=city,
                    )

        # ------------------------------------------------------
        # PEAK BASKET — forecast-only directional-peak strategy (DEMOTED, opt-in).
        # Buys the ensemble forecast peak + ONE trend-directional neighbor.
        # Off by default now that the observed strategy supersedes it. Skipped in
        # the lock window (no forecast edge once the extreme is recorded).
        # ------------------------------------------------------
        if Config.PEAK_BASKET_ENABLED and (
            not in_lock_window or getattr(Config, 'PEAK_BASKET_TRADE_DECIDED', False)
        ):
            peak_signals = self.peak_basket.evaluate(
                market.title, bucket_probs, market_prices, token_ids, balance,
                city=city, stability=stab, grade=grade,
            )
            for sig in peak_signals:
                self.signals_generated += 1
                log.info(f"   🎯 {sig.reason}")
                for leg in sig.legs:
                    pos = self._place(
                        token_id=leg.token_id,
                        condition_id=condition_ids.get(leg.bucket_label, ''),
                        entry_price=leg.market_price,
                        base_size_usd=leg.size_usd,
                        market_title=market.title,
                        bucket_label=leg.bucket_label,
                        strategy=f'peak_basket_{leg.role}',
                        city=city,
                        slug=market.slug,
                        resolution_time=market.resolution_time,
                        edge=sig.combined_prob - sig.total_cost,
                        grade=grade,
                        hold_hint=sig.hold_hint,
                        early_exit_price=early_exit_price,
                        apply_grade_size=False,  # PeakBasket sizes itself
                        reason=sig.reason,
                        signal=f'peak_basket_{leg.role}',
                    )
                    if pos:
                        self.trades_placed += 1
                        self.telegram.notify_trade(
                            'BUY', leg.bucket_label, pos.entry_price,
                            pos.cost_usd, pos.shares, f'peak_{leg.role}',
                            edge=sig.combined_prob - sig.total_cost, city=city,
                        )

        # ------------------------------------------------------
        # CONFIDENT — simple peak-only fallback (DEMOTED, opt-in). Only fires
        # when explicitly enabled as a second opinion on peak-only bets. Skipped
        # in the lock window (no forecast edge once the extreme is recorded).
        # ------------------------------------------------------
        if Config.CONFIDENT_ENABLED and (
            not in_lock_window or getattr(Config, 'CONFIDENT_TRADE_DECIDED', False)
        ):
            confident_signals = self.confident.evaluate(
                market.title, bucket_probs, market_prices, token_ids, balance,
            )
            for signal in confident_signals[:1]:
                # Skip if PeakBasket already bought this bucket (dedup).
                self.signals_generated += 1
                log.info(
                    f"   💎 CONFIDENT: {city} | {signal.bucket_label[:25]} @ "
                    f"${signal.market_price:.3f} | P={signal.our_probability:.0%} | "
                    f"Edge={signal.edge:.0%}"
                )
                pos = self._place(
                    token_id=signal.token_id,
                    condition_id=condition_ids.get(signal.bucket_label, ''),
                    entry_price=signal.market_price,
                    base_size_usd=signal.size_usd,
                    market_title=market.title,
                    bucket_label=signal.bucket_label,
                    strategy='confident',
                    city=city,
                    slug=market.slug,
                    resolution_time=market.resolution_time,
                    edge=signal.edge,
                    grade=grade, hold_hint=True, early_exit_price=early_exit_price,
                    reason=f"P={signal.our_probability:.0%} edge={signal.edge:.0%}",
                    signal='confident',
                )
                if pos:
                    self.trades_placed += 1
                    self.telegram.notify_trade(
                        'BUY', signal.bucket_label, pos.entry_price,
                        pos.cost_usd, pos.shares, 'confident',
                        edge=signal.edge, city=city,
                    )

    def _check_resolutions(self):
        """Check if any positions resolved, redeem winners."""
        self.pm.check_resolutions()
        redeemed = self.pm.redeem_all_winning()
        if redeemed > 0:
            log.info(f"💰 Redeemed {redeemed} winning positions")
            self.telegram.send(f"💰 Redeemed {redeemed} winning positions!")

    def _print_dashboard(self):
        """Print comprehensive dashboard."""
        stats = self.pm.get_stats()
        open_pos = self.pm.get_open_positions()
        pending = self.pm.get_pending_orders() if hasattr(self.pm, 'get_pending_orders') else []

        log.info(f"\n{'━'*60}")
        log.info(f"  🌡️  WEATHER SNIPER DASHBOARD")
        log.info(f"{'━'*60}")
        log.info(f"  Mode:        {stats['mode']}")
        log.info(f"  Balance:     ${stats['balance']:.2f}")
        log.info(f"  Portfolio:   ${stats['portfolio_value']:.2f}")
        log.info(f"  Total PnL:   ${stats['total_pnl']:+.2f} ({stats['roi_pct']:+.1f}%)")
        log.info(f"{'─'*60}")
        log.info(f"  Trades:      {stats['total_trades']}")
        log.info(f"  Win Rate:    {stats['win_rate']:.0f}% ({stats['wins']}W / {stats['losses']}L)")
        log.info(f"  Open:        {len(open_pos)} filled + {len(pending)} pending")
        log.info(f"  Redeemed:    ${stats['total_redeemed']:.2f}")
        log.info(f"  Signals:     {self.signals_generated} generated")
        ml_status = self.ml.get_status()
        if ml_status['enabled']:
            log.info(f"  ML Engine:   {ml_status['model']} ({ml_status['tokens_used']} tokens)")
        if self.resolution_verifier is not None:
            vs = self.resolution_verifier.get_status()
            if vs.get('enabled'):
                log.info(f"  Station LLM: {vs['model']} ({vs['tokens_used']} tokens, {vs['cache_size']} cached)")
        log.info(f"  Contexts:    {stats.get('active_contexts', 0)} active markets")
        log.info(f"{'─'*60}")

        if open_pos:
            log.info(f"  FILLED POSITIONS:")
            for p in open_pos[:10]:
                pnl_e = '+' if p.unrealized_pnl >= 0 else ''
                lock = ' 🔒' if getattr(p, 'preclose_locked', False) else ''
                stale = ' ~stale' if getattr(p, 'current_price_stale', False) else ''
                log.info(
                    f"    {pnl_e} {p.city:12} {p.bucket_label[:30]:30} "
                    f"${p.entry_price:.4f}->${p.current_price:.4f} "
                    f"({p.roi_pct:+.0f}%){lock}{stale}"
                )

        if pending:
            log.info(f"  PENDING (in orderbook, awaiting fill):")
            for p in pending[:5]:
                log.info(
                    f"    ... {p.city:12} {p.bucket_label[:30]:30} "
                    f"${p.entry_price:.4f} {p.shares:.0f}sh"
                )

        log.info(f"{'━'*60}\n")

    def print_status(self):
        """Print status and exit."""
        Config.print_status()
        self._print_dashboard()

    def run_loop(self):
        """Main trading loop — Railway/cloud ready with graceful shutdown."""
        import signal

        Config.print_status()
        log.info("Weather Sniper Bot starting...")
        log.info(f"   Scan interval: {Config.SCAN_INTERVAL_SECONDS}s")
        log.info(f"   Days ahead: {Config.SCAN_DAYS_AHEAD}")
        strats = []
        if Config.LATE_OBSERVED_ENABLED: strats.append('LateObserved*')
        if getattr(Config, 'QUICK_FLIP_ENABLED', False): strats.append('QuickFlip')
        if Config.PEAK_BASKET_ENABLED: strats.append('PeakBasket')
        if Config.CONFIDENT_ENABLED: strats.append('Confident')
        if Config.SNIPER_ENABLED: strats.append('Sniper')
        if Config.SPREAD_ENABLED: strats.append('Spread')
        log.info(f"   Strategies: {', '.join(strats) if strats else 'none'}")
        log.info(f"   Telegram: {'ON' if self.telegram.enabled else 'OFF'}")
        ml_status = self.ml.get_status()
        log.info(f"   ML: {ml_status.get('model','?')} (local: {ml_status.get('local_model','?')})")
        log.info("")

        self.telegram.start_polling()
        self.telegram.send("Weather Sniper Bot started! Use /help for commands.")

        if not Config.is_paper():
            self.pm.recover_positions_on_start()

        shutdown_flag = [False]
        force_count = [0]

        def _handle_shutdown(signum, frame):
            force_count[0] += 1
            if force_count[0] >= 2:
                # Second Ctrl+C = force exit immediately
                log.info("Force quit (2nd interrupt). Exiting now.")
                os._exit(0)
            log.info("Stopping... (press Ctrl+C again to force quit)")
            shutdown_flag[0] = True
            # Raise KeyboardInterrupt to break out of any blocking call / sleep
            raise KeyboardInterrupt()

        # SIGTERM (Railway) uses flag-only; SIGINT (Ctrl+C) raises to break immediately
        try:
            signal.signal(signal.SIGTERM, lambda s, f: shutdown_flag.__setitem__(0, True))
        except (ValueError, OSError, AttributeError):
            pass
        try:
            signal.signal(signal.SIGINT, _handle_shutdown)
        except (ValueError, OSError, AttributeError):
            pass

        scan_start = time.time()
        try:
            while not shutdown_flag[0]:
                try:
                    self.run_once()
                except KeyboardInterrupt:
                    break
                except Exception as e:
                    log.error(f"Scan error: {e}")

                if shutdown_flag[0]:
                    break

                elapsed = time.time() - scan_start
                sleep_time = max(1, Config.SCAN_INTERVAL_SECONDS - elapsed)
                try:
                    for _ in range(int(sleep_time)):
                        if shutdown_flag[0]:
                            break
                        time.sleep(1)
                except KeyboardInterrupt:
                    break
                scan_start = time.time()
        except KeyboardInterrupt:
            pass
        finally:
            log.info("Shutting down — saving state...")
            try:
                self.telegram.send("Bot shutting down.")
                self.telegram.stop_polling()
            except Exception:
                pass
            self.pm._save_state()
            log.info("State saved. Goodbye!")


def main():
    parser = argparse.ArgumentParser(description='Weather Sniper Bot')
    parser.add_argument('--live', action='store_true', help='Enable live trading')
    parser.add_argument('--paper', action='store_true', help='Paper/dry-run mode (default)')
    parser.add_argument('--once', action='store_true', help='Run single scan then exit')
    parser.add_argument('--status', action='store_true', help='Print status and exit')
    parser.add_argument('--balance', type=float, help='Override starting balance')
    parser.add_argument('--days', type=int, default=3, help='Days ahead to scan')
    args = parser.parse_args()

    if args.live:
        Config.TRADING_MODE = 'live'
    if args.balance:
        Config.STARTING_BALANCE = args.balance
    if args.days:
        Config.SCAN_DAYS_AHEAD = args.days

    bot = WeatherBot()

    if args.status:
        bot.print_status()
    elif args.once:
        bot.run_once()
        bot._print_dashboard()
    else:
        bot.run_loop()


if __name__ == '__main__':
    main()
