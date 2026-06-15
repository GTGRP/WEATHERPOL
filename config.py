"""
Weather Trading Bot — Configuration

Polymarket weather market sniper with multi-source forecasts.
Supports paper (dry-run) and live trading modes.

OVERHAUL NOTE: defaults now favor the observation-driven edge — the
Late Observed-Temperature strategy is the PRIMARY strategy, fee-aware EV gating
is on, liquidity awareness adapts to thin books (no hard blocking by default),
and the old forecast-only strategies (PeakBasket / Confident) are demoted to
opt-in.
"""

import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    """Central configuration for weather trading bot."""

    VERSION = "2.1.0"
    VERSION_NAME = "Weather Sniper Pro — Observed Edge"

    # ===================================================================
    # TRADING MODE — paper = dry-run (no real orders), live = real money
    # ===================================================================
    TRADING_MODE = os.getenv('TRADING_MODE', 'paper')  # 'paper' or 'live'
    STARTING_BALANCE = float(os.getenv('STARTING_BALANCE', '3.0'))
    # Master trading switch — toggled live via Telegram /start /stop. When False
    # the bot keeps scanning/monitoring but places NO new trades.
    TRADING_ENABLED = os.getenv('TRADING_ENABLED', '1') == '1'

    # ===================================================================
    # POLYMARKET WALLET (reused from polymarket-bot-v2)
    # ===================================================================
    POLY_PRIVATE_KEY = os.getenv('POLY_PRIVATE_KEY', '')
    POLY_FUNDER_ADDRESS = os.getenv('POLY_FUNDER_ADDRESS', '')
    POLY_PROXY_WALLET = os.getenv('POLY_PROXY_WALLET', '')
    POLY_API_KEY = os.getenv('POLY_API_KEY', '')
    POLY_API_SECRET = os.getenv('POLY_API_SECRET', '')
    POLY_PASSPHRASE = os.getenv('POLY_PASSPHRASE', '')
    POLY_SIGNATURE_TYPE = int(os.getenv('POLY_SIGNATURE_TYPE', '3'))
    POLY_CHAIN_ID = int(os.getenv('POLY_CHAIN_ID', '137'))

    # ===================================================================
    # BUILDER RELAYER (required for live V2 trading)
    # ===================================================================
    POLY_BUILDER_API_KEY = os.getenv('POLY_BUILDER_API_KEY', '')
    POLY_BUILDER_SECRET = os.getenv('POLY_BUILDER_SECRET', '')
    POLY_BUILDER_PASSPHRASE = os.getenv('POLY_BUILDER_PASSPHRASE', '')
    POLY_BUILDER_CODE = os.getenv('POLY_BUILDER_CODE', '')
    AUTO_REDEEM_INTERVAL = int(os.getenv('AUTO_REDEEM_INTERVAL', '120'))

    # ===================================================================
    # V2 CONTRACT ADDRESSES (Polygon mainnet)
    # ===================================================================
    PUSD_CONTRACT = '0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB'
    CTF_EXCHANGE_V2 = '0xE111180000d2663C0091e4f400237545B87B996B'
    NEG_RISK_CTF_EXCHANGE = '0xe2222d279d744050d28e00520010520000310F59'

    # ===================================================================
    # API ENDPOINTS (V2 — same as polymarket-bot-v2)
    # ===================================================================
    GAMMA_API_URL = 'https://gamma-api.polymarket.com'
    CLOB_API_URL = 'https://clob.polymarket.com'
    POLYMARKET_WS_URL = 'wss://ws-subscriptions-clob.polymarket.com/ws/market'
    POLYGON_RPC_URL = os.getenv('POLYGON_RPC_URL', '')
    CLOB_RELAY_URL = os.getenv('CLOB_RELAY_URL', '')

    # Fee: weather = 0% maker (GTC), 5% taker (FOK)
    MAKER_FEE_RATE = 0.0
    TAKER_FEE_RATE = 0.05
    MAKER_PREFERRED = True  # always GTC limit = 0% fee
    # Fee-aware EV gating: when 1, EV/break-even checks assume the conservative
    # TAKER fee (5% x p x (1-p)). Keep on unless you are confident every fill is
    # a resting maker order.
    ASSUME_TAKER_FILLS = os.getenv('ASSUME_TAKER_FILLS', '1') == '1'

    # ===================================================================
    # WEATHER API KEYS
    # ===================================================================
    OPENWEATHER_API_KEY = os.getenv('OPENWEATHER_API_KEY', '')
    # Open-Meteo: no key needed (free, 10k calls/day)
    # weather.gov: no key needed (US gov free)
    # Open-Meteo endpoints to round-robin across. The free tier allows ~10k
    # calls/day; alternating across mirrors (or a self-hosted instance) spreads
    # the budget and reduces the chance of a single-IP rate limit. Comma-sep.
    # Add a second URL here and the fetcher alternates automatically.
    OPEN_METEO_ENDPOINTS = [e.strip() for e in os.getenv(
        'OPEN_METEO_ENDPOINTS',
        'https://api.open-meteo.com/v1/forecast'
    ).split(',') if e.strip()]
    # How long a forecast fetch is cached (seconds). Lower = fresher data + more
    # API calls; keep within the daily budget.
    WEATHER_FORECAST_CACHE_SECONDS = int(os.getenv('WEATHER_FORECAST_CACHE_SECONDS', '300'))

    # ===================================================================
    # TRADING PARAMETERS
    # ===================================================================
    # Sniper strategy: buy buckets priced below this when forecast is strong
    SNIPER_MAX_ENTRY_PRICE = float(os.getenv('SNIPER_MAX_ENTRY_PRICE', '0.15'))
    # SELLABILITY FLOOR (early-exit strategies): a leg that plans to SELL before
    # resolution needs a real bid to exit into. Below ~5c the book often has no
    # bid, so we only require this floor for strategies that intend to flip.
    # Hold-to-resolution legs (e.g. late-observed) bypass it — their EV is
    # already fee-cleared and they never need to sell.
    MIN_ENTRY_PRICE = float(os.getenv('MIN_ENTRY_PRICE', '0.05'))
    # HARD DUST FLOOR (all strategies): below this a leg can't even rest on the
    # 1c-tick venue. This is the only absolute price block now — cheap EV+ tails
    # above it are allowed when held to resolution (the 90%-WR wallet's edge).
    ABS_PRICE_FLOOR = float(os.getenv('ABS_PRICE_FLOOR', '0.01'))
    # Trade BOTH daily-high and daily-low markets. The observation-driven
    # strategy locks the high in the afternoon and the low overnight, and trades
    # the NO side of dead buckets either way. Set to 1 to restrict to highs only.
    HIGHEST_TEMP_ONLY = os.getenv('HIGHEST_TEMP_ONLY', '0') == '1'
    # Minimum edge (our probability - market price) to enter
    MIN_EDGE_TO_ENTER = float(os.getenv('MIN_EDGE_TO_ENTER', '0.10'))
    # Kelly criterion fraction (conservative)
    KELLY_FRACTION = float(os.getenv('KELLY_FRACTION', '0.15'))
    # Maximum bet as % of balance
    MAX_BET_PCT = float(os.getenv('MAX_BET_PCT', '0.20'))
    # Minimum order size on Polymarket
    MIN_ORDER_SIZE = 1.0
    # Maximum concurrent positions
    MAX_POSITIONS = int(os.getenv('MAX_POSITIONS', '10'))
    # Maximum exposure per single market (% of balance)
    MAX_SINGLE_MARKET_PCT = float(os.getenv('MAX_SINGLE_MARKET_PCT', '0.30'))
    # Risk Management (weather markets are BINARY -> no traditional SL/TP)
    # Instead: hold to resolution OR sell early at profit
    # "Stop-loss" only applies to LOCK-IN trades that went wrong
    STOP_LOSS_PCT = float(os.getenv('STOP_LOSS_PCT', '-95'))  # almost never triggers
    TRAILING_STOP_PCT = float(os.getenv('TRAILING_STOP_PCT', '30'))
    # Only arm the trailing stop after a BIG run-up (peak >= this multiple of
    # entry). Higher = give winners more room before a trailing exit can fire.
    # The user flagged trailing/stop exits cutting good positions too early.
    TRAILING_MIN_PEAK_MULT = float(os.getenv('TRAILING_MIN_PEAK_MULT', '3.0'))
    # PROFIT-TAKE: sell if price rises above this BEFORE resolution (early profit)
    EARLY_PROFIT_THRESHOLD = float(os.getenv('EARLY_PROFIT_THRESHOLD', '0.60'))
    # For confident strategy: never sell (hold to resolution)
    CONFIDENT_NEVER_SELL = os.getenv('CONFIDENT_NEVER_SELL', '1') == '1'

    # ===================================================================
    # BEST-KELLY FACTOR SIZING (trading/sizing.py) — multi-factor allocation.
    # Replaces the flat %-Kelly that dumped ~25% of balance on the first signal
    # and starved later (better) markets. Stake now scales with a composite
    # signal-strength score (edge + win-probability + grade + realized win-rate)
    # onto an ABSOLUTE-USD tier ladder: base $3, good $5, very good $10, perfect
    # $15 MAX per position. A portfolio RESERVE + per-scan deploy caps keep cash
    # for opportunities that appear later in the same scan.
    # ===================================================================
    KELLY_FACTOR_SIZING_ENABLED = os.getenv('KELLY_FACTOR_SIZING_ENABLED', '1') == '1'
    KELLY_TIER_BASE_USD = float(os.getenv('KELLY_TIER_BASE_USD', '3.0'))       # weakest valid signal
    KELLY_TIER_GOOD_USD = float(os.getenv('KELLY_TIER_GOOD_USD', '5.0'))       # good signal
    KELLY_TIER_VGOOD_USD = float(os.getenv('KELLY_TIER_VGOOD_USD', '10.0'))    # very good signal
    KELLY_TIER_PERFECT_USD = float(os.getenv('KELLY_TIER_PERFECT_USD', '15.0'))  # perfect signal (HARD MAX)
    KELLY_GOOD_STRENGTH = float(os.getenv('KELLY_GOOD_STRENGTH', '0.40'))      # strength >= -> good tier
    KELLY_VGOOD_STRENGTH = float(os.getenv('KELLY_VGOOD_STRENGTH', '0.65'))    # strength >= -> very-good tier
    KELLY_PERFECT_STRENGTH = float(os.getenv('KELLY_PERFECT_STRENGTH', '0.85'))  # strength >= -> perfect tier
    KELLY_W_EDGE = float(os.getenv('KELLY_W_EDGE', '0.35'))                    # weight: post-fee edge
    KELLY_W_PROB = float(os.getenv('KELLY_W_PROB', '0.25'))                    # weight: P(win)
    KELLY_W_GRADE = float(os.getenv('KELLY_W_GRADE', '0.20'))                  # weight: stability grade
    KELLY_W_WINRATE = float(os.getenv('KELLY_W_WINRATE', '0.20'))             # weight: realized strategy win-rate
    KELLY_EDGE_FULL = float(os.getenv('KELLY_EDGE_FULL', '0.25'))              # edge counted as max strength
    KELLY_WINRATE_PRIOR = float(os.getenv('KELLY_WINRATE_PRIOR', '0.45'))      # win-rate prior before enough trades
    KELLY_WINRATE_FULL_TRUST_N = int(os.getenv('KELLY_WINRATE_FULL_TRUST_N', '20'))  # trades to fully trust observed WR
    KELLY_MAX_FRACTION = float(os.getenv('KELLY_MAX_FRACTION', '0.25'))        # per-trade safety cap vs live balance

    # PORTFOLIO GUARD — stop the "allocates all funds immediately" drain. Keep a
    # cash reserve, cap how much of the portfolio is deployed, and cap how much
    # NEW money + how many NEW buys a single scan may place, so capital is left
    # for the good markets that appear later in the same scan.
    PORTFOLIO_GUARD_ENABLED = os.getenv('PORTFOLIO_GUARD_ENABLED', '1') == '1'
    PORTFOLIO_RESERVE_PCT = float(os.getenv('PORTFOLIO_RESERVE_PCT', '0.15'))  # always keep >= this % of portfolio in cash
    PORTFOLIO_MAX_DEPLOY_PCT = float(os.getenv('PORTFOLIO_MAX_DEPLOY_PCT', '0.85'))  # never deploy beyond this % of portfolio
    MAX_DEPLOY_PER_SCAN_PCT = float(os.getenv('MAX_DEPLOY_PER_SCAN_PCT', '0.30'))  # new $ per scan <= this % of portfolio
    MAX_BUYS_PER_SCAN = int(os.getenv('MAX_BUYS_PER_SCAN', '6'))               # max NEW buys placed in one scan

    # PER-STRATEGY SIZE MULTIPLIER — lean toward what wins, away from what loses.
    # peak_cluster is the only net-positive strategy in the logs -> boost it;
    # late_observed_yes was 0% WR over 21 trades -> shrink it. 1.0 = neutral.
    STRATEGY_SIZE_MULT = {
        'peak_cluster': float(os.getenv('SIZE_MULT_PEAK_CLUSTER', '1.25')),
        'late_observed_yes': float(os.getenv('SIZE_MULT_LATE_OBSERVED_YES', '0.6')),
        'late_observed_no': float(os.getenv('SIZE_MULT_LATE_OBSERVED_NO', '1.0')),
        'quick_flip': float(os.getenv('SIZE_MULT_QUICK_FLIP', '1.0')),
    }

    # ML SIZING/VETO INFLUENCE — the ML engine (ml/decision_engine.py) is now
    # actually consulted on the trade path (it was wired but never called).
    # A SKIP with high confidence vetoes the buy; otherwise its confidence
    # scales size between MIN and MAX mult. Safe no-op when ML_API_KEY is unset
    # (the engine returns a local BUY@0.7 fallback, so nothing is blocked).
    ML_DECISION_ENABLED = os.getenv('ML_DECISION_ENABLED', '1') == '1'
    ML_VETO_CONF = float(os.getenv('ML_VETO_CONF', '0.66'))                    # SKIP with conf >= this vetoes the buy
    ML_SIZE_MIN_MULT = float(os.getenv('ML_SIZE_MIN_MULT', '0.7'))            # size mult at low ML confidence
    ML_SIZE_MAX_MULT = float(os.getenv('ML_SIZE_MAX_MULT', '1.2'))            # size mult at high ML confidence

    # ===================================================================
    # FEATURE TOGGLES (enable/disable without breaking anything)
    # ===================================================================
    SNIPER_ENABLED = os.getenv('SNIPER_ENABLED', '0') == '1'
    SPREAD_ENABLED = os.getenv('SPREAD_STRATEGY_ENABLED', '0') == '1'
    SELECTIVE_SNIPER_ENABLED = os.getenv('SELECTIVE_SNIPER_ENABLED', '0') == '1'
    # Early-mispricing / forecast-change sniper: buy a freshly mispriced bucket
    # before the book adjusts and flip on the correction (or hold if structural).
    # Enabled by default now that it is wired into the scan loop.
    QUICK_FLIP_ENABLED = os.getenv('QUICK_FLIP_ENABLED', '1') == '1'
    CORRELATION_ARB_ENABLED = os.getenv('CORRELATION_ARB_ENABLED', '0') == '1'
    # Demoted: forecast-only single-bucket bet. Off by default — the observed
    # strategy supersedes it. Flip to 1 to run it as a second opinion.
    CONFIDENT_ENABLED = os.getenv('CONFIDENT_ENABLED', '0') == '1'
    # Enabled by default: stability is a GRADE applied across strategies.
    STABILITY_ENABLED = os.getenv('STABILITY_ENABLED', '1') == '1'
    LOCKIN_ENABLED = os.getenv('LOCKIN_ENABLED', '1') == '1'
    ML_ENABLED = os.getenv('ML_ENABLED', '1') == '1'
    TELEGRAM_ENABLED = os.getenv('TELEGRAM_ENABLED', '1') == '1'
    COPY_TRADING_ENABLED = os.getenv('COPY_TRADING_ENABLED', '0') == '1'
    ADAPTIVE_EXIT_ENABLED = os.getenv('ADAPTIVE_EXIT_ENABLED', '1') == '1'
    AUTO_REDEEM_ENABLED = os.getenv('AUTO_REDEEM_ENABLED', '1') == '1'
    DRAWDOWN_GATE_ENABLED = os.getenv('DRAWDOWN_GATE_ENABLED', '1') == '1'

    # ===================================================================
    # LATE OBSERVED-TEMPERATURE STRATEGY (PRIMARY) — trade the locked extreme
    # Once the local day's peak/trough has passed, the observed max/min is a
    # hard floor/ceiling on the settled value while the book still prices stale
    # forecast uncertainty. YES the bucket that's locked in; NO the buckets that
    # observed data has made impossible. All gating is fee-aware.
    # ===================================================================
    LATE_OBSERVED_ENABLED = os.getenv('LATE_OBSERVED_ENABLED', '1') == '1'
    LATE_OBSERVED_NO_SIDE = os.getenv('LATE_OBSERVED_NO_SIDE', '1') == '1'      # also buy NO on dead buckets
    LATE_OBSERVED_MIN_LOCK = float(os.getenv('LATE_OBSERVED_MIN_LOCK', '0.70'))  # min lock-confidence to trade
    LATE_OBSERVED_MIN_EDGE = float(os.getenv('LATE_OBSERVED_MIN_EDGE', '0.10'))  # post-fee probability cushion
    LATE_OBSERVED_MAX_YES_PRICE = float(os.getenv('LATE_OBSERVED_MAX_YES_PRICE', '0.95'))
    # Cheap-tail allowance for the HOLD-to-resolution primary strategy. Lower
    # than the global sellability floor because these legs never need to sell —
    # this is exactly the sub-5c tail the reference 90%-WR wallet lives in.
    LATE_OBSERVED_MIN_ENTRY_PRICE = float(os.getenv('LATE_OBSERVED_MIN_ENTRY_PRICE', '0.02'))
    LATE_OBSERVED_NO_MIN_PRICE = float(os.getenv('LATE_OBSERVED_NO_MIN_PRICE', '0.04'))
    LATE_OBSERVED_NO_MAX_PRICE = float(os.getenv('LATE_OBSERVED_NO_MAX_PRICE', '0.97'))
    LATE_OBSERVED_BASE_FRACTION = float(os.getenv('LATE_OBSERVED_BASE_FRACTION', '0.06'))
    LATE_OBSERVED_MAX_FRACTION = float(os.getenv('LATE_OBSERVED_MAX_FRACTION', '0.25'))
    LATE_OBSERVED_MAX_LEGS = int(os.getenv('LATE_OBSERVED_MAX_LEGS', '4'))
    # --- Signal-strength -> absolute-USD allocation ladder (replaces flat %-Kelly) ---
    # The late-observed stake now scales with a composite signal-strength score
    # (post-fee edge + weather grade): a VERY good signal deploys up to
    # SIZE_MAX_USD, a mid one lands mid-ladder, a barely-passing one stays near
    # SIZE_FLOOR_USD. This fixes the "every buy suddenly uses ~25% of balance /
    # ~$25" drain — MAX_FRACTION above is now only a per-trade SAFETY cap vs the
    # live balance, not the sizing target. Tune the ladder here.
    LATE_OBSERVED_SIZE_FLOOR_USD = float(os.getenv('LATE_OBSERVED_SIZE_FLOOR_USD', '3.0'))   # weakest valid signal (~$3-4)
    LATE_OBSERVED_SIZE_MAX_USD = float(os.getenv('LATE_OBSERVED_SIZE_MAX_USD', '15.0'))      # strongest signal (HARD MAX $15)
    LATE_OBSERVED_EDGE_FULL = float(os.getenv('LATE_OBSERVED_EDGE_FULL', '0.25'))            # post-fee edge counted as max strength
    LATE_OBSERVED_W_EDGE = float(os.getenv('LATE_OBSERVED_W_EDGE', '0.6'))                   # weight of edge in the strength score
    LATE_OBSERVED_W_GRADE = float(os.getenv('LATE_OBSERVED_W_GRADE', '0.4'))                 # weight of grade in the strength score

    # ===================================================================
    # CITY FILTER (which cities to trade — empty = all)
    # ===================================================================
    ENABLED_CITIES = [c.strip() for c in os.getenv('ENABLED_CITIES', '').split(',') if c.strip()]
    # If empty -> trade all cities. If set -> only trade these.
    # Example: ENABLED_CITIES=tokyo,seoul,ankara,london

    # ===================================================================
    # DRAWDOWN GATE — pause trading if drawdown exceeds threshold
    # ===================================================================
    MAX_DAILY_DRAWDOWN_PCT = float(os.getenv('MAX_DAILY_DRAWDOWN_PCT', '30'))
    MAX_WEEKLY_DRAWDOWN_PCT = float(os.getenv('MAX_WEEKLY_DRAWDOWN_PCT', '50'))
    DRAWDOWN_COOLDOWN_MINUTES = int(os.getenv('DRAWDOWN_COOLDOWN_MINUTES', '60'))

    # ===================================================================
    # LOCK-IN STRATEGY (buy near-certain outcomes at $0.90+ for safe profit)
    # ===================================================================
    LOCKIN_MIN_PRICE = float(os.getenv('LOCKIN_MIN_PRICE', '0.90'))
    LOCKIN_MIN_CONFIDENCE = float(os.getenv('LOCKIN_MIN_CONFIDENCE', '0.85'))
    LOCKIN_MAX_BET_PCT = float(os.getenv('LOCKIN_MAX_BET_PCT', '0.40'))

    # ===================================================================
    # STABILITY STRATEGY — trade only predictable city-days, adjacent buckets
    # ===================================================================
    STABILITY_MIN_SCORE = float(os.getenv('STABILITY_MIN_SCORE', '0.62'))        # predictable threshold
    STABILITY_NEIGHBOR_SPAN = int(os.getenv('STABILITY_NEIGHBOR_SPAN', '1'))     # +/-1 -> buy 3 buckets
    STABILITY_MAX_LEG_PRICE = float(os.getenv('STABILITY_MAX_LEG_PRICE', '0.60'))
    STABILITY_MIN_EDGE = float(os.getenv('STABILITY_MIN_EDGE', '0.08'))          # basket edge vs cost
    STABILITY_MAX_FRACTION = float(os.getenv('STABILITY_MAX_FRACTION', '0.25'))  # max % of balance per basket
    STABILITY_EARLY_EXIT_PRICE = float(os.getenv('STABILITY_EARLY_EXIT_PRICE', '0.85'))
    STABILITY_EXIT_HOURS_BEFORE = float(os.getenv('STABILITY_EXIT_HOURS_BEFORE', '1.0'))

    # ===================================================================
    # PEAK BASKET — forecast-only directional-peak strategy (DEMOTED).
    # Superseded by the observed-temperature strategy; off by default. Flip to 1
    # to run it (e.g. early in the day before the extreme is locked).
    # ===================================================================
    PEAK_BASKET_ENABLED = os.getenv('PEAK_BASKET_ENABLED', '0') == '1'
    PEAK_MIN_STABILITY = float(os.getenv('PEAK_MIN_STABILITY', '0.45'))        # minimum grade to trade
    PEAK_MAX_PEAK_PRICE = float(os.getenv('PEAK_MAX_PEAK_PRICE', '0.85'))     # don't buy peak if market already knows
    PEAK_MAX_NEIGHBOR_PRICE = float(os.getenv('PEAK_MAX_NEIGHBOR_PRICE', '0.60'))  # neighbor cap
    PEAK_MAX_BASKET_COST = float(os.getenv('PEAK_MAX_BASKET_COST', '0.95'))   # sum(leg prices) < this -> >=5% profit on any win
    PEAK_MIN_EDGE = float(os.getenv('PEAK_MIN_EDGE', '0.03'))                 # P(win) - basket_cost minimum
    PEAK_BASE_FRACTION = float(os.getenv('PEAK_BASE_FRACTION', '0.05'))       # base % of balance per basket
    PEAK_MAX_FRACTION = float(os.getenv('PEAK_MAX_FRACTION', '0.25'))         # max % of balance per basket (when everything aligns)
    PEAK_MIN_MODELS = int(os.getenv('PEAK_MIN_MODELS', '2'))                  # minimum ensemble models required
    # Fee-aware profit floor (Req-25 equal-shares correction): the per-share
    # basket cost must satisfy  cost <= 1 - PEAK_FEE_BUFFER - PEAK_MIN_NET_PROFIT,
    # so whichever single leg wins ($1/share under equal-shares allocation)
    # covers the WHOLE basket cost PLUS a net profit margin AFTER fees. These two
    # knobs cap the effective max basket cost alongside PEAK_MAX_BASKET_COST.
    PEAK_FEE_BUFFER = float(os.getenv('PEAK_FEE_BUFFER', '0.02'))             # taker-fee headroom reserved on the winning leg
    PEAK_MIN_NET_PROFIT = float(os.getenv('PEAK_MIN_NET_PROFIT', '0.03'))     # minimum net profit margin required after fees

    # ===================================================================
    # BASKET QUALITY — predict the max temp, then buy an adjacent basket whose
    # COMBINED cost < BASKET_MAX_COST so ANY single winning leg nets profit.
    # ===================================================================
    BASKET_MAX_COST = float(os.getenv('BASKET_MAX_COST', '0.85'))            # sum(leg prices) must be below this
    BASKET_TIGHT_GRADE = float(os.getenv('BASKET_TIGHT_GRADE', '0.80'))      # grade >= this (with high conf) -> tight 2-leg basket
    BASKET_TIGHT_CONFIDENCE = float(os.getenv('BASKET_TIGHT_CONFIDENCE', '0.70'))  # center-bucket confidence >= this for tight basket

    # ===================================================================
    # SNIPER GATE — the lone cheap-tail sniper only fires on high-conviction,
    # stable city-days. Require a strong grade AND high model confidence.
    # ===================================================================
    SNIPER_MIN_GRADE = float(os.getenv('SNIPER_MIN_GRADE', '0.70'))         # stability grade required for a lone sniper buy
    SNIPER_MIN_CONFIDENCE = float(os.getenv('SNIPER_MIN_CONFIDENCE', '0.60'))  # model confidence required for a lone sniper buy
    SNIPER_MIN_PROBABILITY = float(os.getenv('SNIPER_MIN_PROBABILITY', '0.12'))

    # ===================================================================
    # QUICK_FLIP v2 — run-boundary forecast-change flip + EARLY-MISPRICING entry.
    # KEEPS BOTH entry paths: (a) an early/opening mispricing where a bucket's
    # model probability sits well above its market price (edge >= MIN_EDGE), and
    # (b) a NEW forecast RUN that moved the ensemble mean (run-change = BOOST,
    # not a hard gate). Stale book + publish window add confidence BOOSTS. Dedups
    # re-signals, sizes small, caps candidates per market, and TRULY exits via
    # book-or-cut at the hold cap. All values below equal the in-code getattr
    # defaults — editable here now.
    # ===================================================================
    QUICK_FLIP_MIN_DELTA_C = float(os.getenv('QUICK_FLIP_MIN_DELTA_C', '1.0'))            # min ensemble-mean move (C) across runs to signal
    QUICK_FLIP_MIN_EDGE = float(os.getenv('QUICK_FLIP_MIN_EDGE', '0.08'))                 # early-mispricing entry: model prob - price >= this
    QUICK_FLIP_MAX_PER_MARKET = int(os.getenv('QUICK_FLIP_MAX_PER_MARKET', '3'))          # cap flip candidates per market per scan
    QUICK_FLIP_MIN_CONFIDENCE = float(os.getenv('QUICK_FLIP_MIN_CONFIDENCE', '0.6'))      # min confidence required after boosts
    QUICK_FLIP_MAX_ENTRY = float(os.getenv('QUICK_FLIP_MAX_ENTRY', '0.85'))               # don't chase an already-priced bucket
    QUICK_FLIP_MAX_HOLD_MIN = int(os.getenv('QUICK_FLIP_MAX_HOLD_MIN', '120'))            # book-or-cut after this many minutes
    QUICK_FLIP_TARGET_ROI = float(os.getenv('QUICK_FLIP_TARGET_ROI', '15.0'))             # take-profit ROI% target
    QUICK_FLIP_SIZE_PCT = float(os.getenv('QUICK_FLIP_SIZE_PCT', '0.05'))                 # base size as % of balance
    QUICK_FLIP_MAX_SIZE_USD = float(os.getenv('QUICK_FLIP_MAX_SIZE_USD', '10.0'))         # hard $ cap per flip
    QUICK_FLIP_SIGNAL_COOLDOWN_MIN = int(os.getenv('QUICK_FLIP_SIGNAL_COOLDOWN_MIN', '30'))  # dedup: don't re-signal same bucket within N min
    QUICK_FLIP_WINDOW_MIN = int(os.getenv('QUICK_FLIP_WINDOW_MIN', '20'))                 # publish-window length (min) for the boost
    QUICK_FLIP_WINDOW_BOOST = float(os.getenv('QUICK_FLIP_WINDOW_BOOST', '0.10'))         # confidence boost inside the publish window
    QUICK_FLIP_STALE_BOOST = float(os.getenv('QUICK_FLIP_STALE_BOOST', '0.10'))           # confidence boost when the market price is stale
    QUICK_FLIP_STALE_EPS = float(os.getenv('QUICK_FLIP_STALE_EPS', '0.01'))               # |price-prev| below this counts as stale
    QUICK_FLIP_MAX_CONCURRENT = int(os.getenv('QUICK_FLIP_MAX_CONCURRENT', '6'))          # max simultaneous open flips
    QUICK_FLIP_TIME_EXIT = os.getenv('QUICK_FLIP_TIME_EXIT', '1') == '1'                  # enforce book-or-cut at the hold cap

    # ===================================================================
    # PEAK_CLUSTER — NEW parallel any-one-wins basket. Estimate the peak bucket
    # (argmax model probability), buy a window of adjacent buckets whose
    # COMBINED per-share cost stays below PEAK_CLUSTER_MAX_COST, so ANY single
    # winning leg pays $1 > cost = net profit after fees. HOLDS TO RESOLUTION
    # (never stop-lossed/trailed — the whole basket only works if every leg rides
    # to settlement). Widened per Req-23: span +/-3, up to 7 legs, combined cost
    # up to 0.97 ("under 95-97¢ after fees is OK"). Runs ALONGSIDE the others.
    # ===================================================================
    PEAK_CLUSTER_ENABLED = os.getenv('PEAK_CLUSTER_ENABLED', '1') == '1'
    PEAK_CLUSTER_SPAN = int(os.getenv('PEAK_CLUSTER_SPAN', '3'))                          # +/- buckets around the estimated peak
    PEAK_CLUSTER_MAX_COST = float(os.getenv('PEAK_CLUSTER_MAX_COST', '0.97'))             # combined per-share cost ceiling (any win still profits after fees)
    PEAK_CLUSTER_MIN_LEGS = int(os.getenv('PEAK_CLUSTER_MIN_LEGS', '2'))                  # minimum legs for a valid basket
    PEAK_CLUSTER_MAX_LEGS = int(os.getenv('PEAK_CLUSTER_MAX_LEGS', '7'))                  # 2-7 neighbouring buckets per the design
    PEAK_CLUSTER_MIN_EDGE = float(os.getenv('PEAK_CLUSTER_MIN_EDGE', '0.03'))             # combined prob - cost minimum
    PEAK_CLUSTER_MIN_CONF = float(os.getenv('PEAK_CLUSTER_MIN_CONF', '0.55'))             # min center-bucket confidence
    PEAK_CLUSTER_MAX_CENTER_PRICE = float(os.getenv('PEAK_CLUSTER_MAX_CENTER_PRICE', '0.85'))  # skip if the peak is already fully priced
    PEAK_CLUSTER_BASE_FRACTION = float(os.getenv('PEAK_CLUSTER_BASE_FRACTION', '0.05'))   # base % of balance per basket
    PEAK_CLUSTER_MAX_FRACTION = float(os.getenv('PEAK_CLUSTER_MAX_FRACTION', '0.20'))     # max % of balance per basket
    PEAK_CLUSTER_MAX_USD = float(os.getenv('PEAK_CLUSTER_MAX_USD', '15.0'))               # hard $ cap per basket
    PEAK_CLUSTER_TRADE_DECIDED = os.getenv('PEAK_CLUSTER_TRADE_DECIDED', '0') == '1'      # run inside the lock window? off by default

    # ===================================================================
    # SAFETY PEAK — focused HIGH-confidence 1-2 bucket directional peak (Req-25
    # fix #4). Distinct from peak_cluster (wide 3-7 leg any-one-wins basket) and
    # peak_basket (looser directional/symmetric basket): this is the TIGHTEST,
    # most PATIENT peak play. It fires ONLY when the peak estimate is accurate
    # and high-confidence (tight ensemble std + many agreeing models + strong
    # grade + high peak-bucket confidence), then buys the peak bucket plus
    # EXACTLY ONE directional safety neighbour (warming->+1, cooling->-1,
    # stable->the shoulder the ensemble mean leans toward) in EQUAL SHARES, so
    # whichever single bucket resolves to $1 covers the other leg's loss PLUS a
    # net profit AFTER fees. Holds to resolution. OFF by default — enable once
    # validated. Defaults below mirror the in-code getattr() fallbacks.
    # ===================================================================
    SAFETY_PEAK_ENABLED = os.getenv('SAFETY_PEAK_ENABLED', '0') == '1'
    SAFETY_PEAK_MIN_GRADE = float(os.getenv('SAFETY_PEAK_MIN_GRADE', '0.65'))             # min stability grade (accurate, predictable day)
    SAFETY_PEAK_MIN_MODELS = int(os.getenv('SAFETY_PEAK_MIN_MODELS', '3'))                # min ensemble models agreeing
    SAFETY_PEAK_MAX_STD = float(os.getenv('SAFETY_PEAK_MAX_STD', '1.2'))                  # max ensemble spread (C); tight = accurate
    SAFETY_PEAK_MIN_CONFIDENCE = float(os.getenv('SAFETY_PEAK_MIN_CONFIDENCE', '0.70'))   # min peak-bucket confidence
    SAFETY_PEAK_MAX_PEAK_PRICE = float(os.getenv('SAFETY_PEAK_MAX_PEAK_PRICE', '0.85'))   # don't chase an already-priced peak
    SAFETY_PEAK_MAX_NEIGHBOR_PRICE = float(os.getenv('SAFETY_PEAK_MAX_NEIGHBOR_PRICE', '0.60'))  # neighbour price cap
    SAFETY_PEAK_FEE_BUFFER = float(os.getenv('SAFETY_PEAK_FEE_BUFFER', '0.02'))           # taker-fee headroom on the winning leg
    SAFETY_PEAK_MIN_NET_PROFIT = float(os.getenv('SAFETY_PEAK_MIN_NET_PROFIT', '0.05'))   # min net profit after fees (any-one-wins)
    SAFETY_PEAK_MIN_EDGE = float(os.getenv('SAFETY_PEAK_MIN_EDGE', '0.05'))               # combined prob - basket cost minimum
    SAFETY_PEAK_BASE_FRACTION = float(os.getenv('SAFETY_PEAK_BASE_FRACTION', '0.05'))     # base % of balance per basket
    SAFETY_PEAK_MAX_FRACTION = float(os.getenv('SAFETY_PEAK_MAX_FRACTION', '0.20'))       # max % of balance per basket
    SAFETY_PEAK_MAX_USD = float(os.getenv('SAFETY_PEAK_MAX_USD', '15.0'))                 # hard $ cap per basket
    SAFETY_PEAK_TRADE_DECIDED = os.getenv('SAFETY_PEAK_TRADE_DECIDED', '0') == '1'        # run inside the lock window? off (forecast-based edge)

    # ===================================================================
    # THESIS-INVALIDATION EXIT — STRICT early exit. Most positions HOLD to
    # resolution; only a non-tail position whose ROI has COLLAPSED (very bad)
    # exits early. Cheap tails, stale prices, and near-close positions all KEEP
    # HOLDING. Values = code defaults; set THESIS_EXIT_ENABLED=0 to disable.
    # ===================================================================
    THESIS_EXIT_ENABLED = os.getenv('THESIS_EXIT_ENABLED', '1') == '1'
    THESIS_EXIT_MAX_ROI_PCT = float(os.getenv('THESIS_EXIT_MAX_ROI_PCT', '-85.0'))        # exit ONLY if ROI <= this (very bad)
    THESIS_EXIT_MIN_ENTRY_PRICE = float(os.getenv('THESIS_EXIT_MIN_ENTRY_PRICE', '0.10')) # tails below this are HELD, never thesis-exited
    THESIS_EXIT_MIN_BID = float(os.getenv('THESIS_EXIT_MIN_BID', '0.02'))                 # need a real bid to exit into
    THESIS_EXIT_MIN_MINUTES_TO_CLOSE = float(os.getenv('THESIS_EXIT_MIN_MINUTES_TO_CLOSE', '60.0'))  # near-close positions are HELD

    # ===================================================================
    # OUTCOME-DECIDED GATE — only HARD-skip a market once its measurement day is
    # FULLY OVER in the city's local time (value recorded, just awaiting UMA).
    # The intraday lock-hour window (same local day, after the peak) is exactly
    # where the late-observed strategy has its edge, so it is NOT skipped — the
    # bot trades the locked-but-unresolved extreme.
    # ===================================================================
    SKIP_DECIDED_MARKETS = os.getenv('SKIP_DECIDED_MARKETS', '1') == '1'
    HIGH_TEMP_LOCK_HOUR = int(os.getenv('HIGH_TEMP_LOCK_HOUR', '18'))       # local hour after which the day's HIGH is considered set
    # PER-STRATEGY lock-window opt-in. When a market is decided/locked but the
    # local day is NOT yet fully over, these decide which strategies still run.
    # Observation strategies (LateObserved, QuickFlip) HAVE their edge in this
    # window, so they default ON (1). Forecast-only strategies (PeakBasket,
    # Confident) have no edge once the extreme is locked, so they default OFF (0).
    # This replaces the old blanket skip that was blocking EVERY strategy.
    LATE_OBSERVED_TRADE_DECIDED = os.getenv('LATE_OBSERVED_TRADE_DECIDED', '1') == '1'
    QUICK_FLIP_TRADE_DECIDED = os.getenv('QUICK_FLIP_TRADE_DECIDED', '1') == '1'
    PEAK_BASKET_TRADE_DECIDED = os.getenv('PEAK_BASKET_TRADE_DECIDED', '0') == '1'
    CONFIDENT_TRADE_DECIDED = os.getenv('CONFIDENT_TRADE_DECIDED', '0') == '1'

    # ===================================================================
    # STABILITY GRADE — stability is a GRADE (not a strategy): it scales
    # size and sets the exit for EVERY strategy.
    # ===================================================================
    GRADE_SIZING_ENABLED = os.getenv('GRADE_SIZING_ENABLED', '1') == '1'
    GRADE_NEUTRAL = float(os.getenv('GRADE_NEUTRAL', '0.60'))          # default grade when stability data is missing
    GRADE_MIN_TO_TRADE = float(os.getenv('GRADE_MIN_TO_TRADE', '0.35'))  # hard skip below this grade
    GRADE_SIZE_MIN_MULT = float(os.getenv('GRADE_SIZE_MIN_MULT', '0.30'))  # size multiplier at grade 0
    GRADE_SIZE_MAX_MULT = float(os.getenv('GRADE_SIZE_MAX_MULT', '1.25'))  # size multiplier at grade 1

    # ===================================================================
    # LIQUIDITY AWARENESS — weather books are thin & asymmetric BY DESIGN. The
    # bot READS the order book and ADAPTS: maker entry at best_bid, trims size
    # on thin/wide books, holds to resolution when too thin to exit. ADAPTIVE by
    # default (STRICT_BLOCK off): we utilise wide-spread weather books instead of
    # skipping them, and only the truly untradeable (no bid / no book) get
    # down-sized + held. Set LIQUIDITY_STRICT_BLOCK=1 to hard-skip instead.
    # ===================================================================
    LIQUIDITY_GUARD_ENABLED = os.getenv('LIQUIDITY_GUARD_ENABLED', '1') == '1'   # read & adapt to the book
    LIQUIDITY_STRICT_BLOCK = os.getenv('LIQUIDITY_STRICT_BLOCK', '0') == '1'     # 0 = adapt (default), 1 = hard-skip failing books
    LIQUIDITY_THIN_SIZE_MULT = float(os.getenv('LIQUIDITY_THIN_SIZE_MULT', '0.7'))  # keep ~70% size on thin/wide books (trim ~30%)
    LIQUIDITY_BOOK_CACHE_SECONDS = int(os.getenv('LIQUIDITY_BOOK_CACHE_SECONDS', '30'))

    # ===================================================================
    # ADAPTIVE EXIT — analyze unfavorable markets and exit properly
    # ===================================================================
    ADAPTIVE_CHECK_INTERVAL = int(os.getenv('ADAPTIVE_CHECK_INTERVAL', '120'))
    ADAPTIVE_SELL_IF_EDGE_LOST = os.getenv('ADAPTIVE_SELL_IF_EDGE_LOST', '1') == '1'
    ADAPTIVE_MIN_HOLD_MINUTES = int(os.getenv('ADAPTIVE_MIN_HOLD_MINUTES', '10'))

    # ===================================================================
    # COPY TRADING (mirror reference wallet trades)
    # ===================================================================
    COPY_WALLET = os.getenv('COPY_WALLET', '0x594edb9112f526fa6a80b8f858a6379c8a2c1c11')
    COPY_SCALE_FACTOR = float(os.getenv('COPY_SCALE_FACTOR', '0.01'))
    COPY_POLL_INTERVAL = int(os.getenv('COPY_POLL_INTERVAL', '30'))

    # ===================================================================
    # MULTI-OUTCOME SPREAD STRATEGY
    # ===================================================================
    SPREAD_NEIGHBOR_DECAY = float(os.getenv('SPREAD_NEIGHBOR_DECAY', '0.4'))
    SPREAD_MAX_COST = float(os.getenv('SPREAD_MAX_COST', '1.50'))

    # ===================================================================
    # SCAN SETTINGS
    # ===================================================================
    SCAN_INTERVAL_SECONDS = int(os.getenv('SCAN_INTERVAL_SECONDS', '60'))
    SCAN_DAYS_AHEAD = int(os.getenv('SCAN_DAYS_AHEAD', '3'))

    # ===================================================================
    # PAPER-REALISM — make the dry run behave like real trading
    # The paper engine fills against the live ask ladder, settles from
    # Polymarket's ACTUAL resolved outcome (so it works after a market closes),
    # freezes stale prices instead of showing 0/random, flags near-certain wins
    # in the final minutes, and asserts a conserved-PnL ledger after every change.
    # ===================================================================
    PAPER_REALISTIC_FILL = os.getenv('PAPER_REALISTIC_FILL', '1') == '1'         # walk the real ask ladder for paper buys
    PAPER_SETTLE_BY_WEATHER = os.getenv('PAPER_SETTLE_BY_WEATHER', '0') == '1'   # weather is CONFIRMATION-only, never the source of truth
    PAPER_PRECLOSE_LOCK_PCT = float(os.getenv('PAPER_PRECLOSE_LOCK_PCT', '0.95'))  # venue price >= this in the final minutes => 'win likely'
    PAPER_PRECLOSE_WINDOW_MIN = float(os.getenv('PAPER_PRECLOSE_WINDOW_MIN', '2'))  # how many minutes before close to flag the lock
    PAPER_TRADE_LOG = os.getenv('PAPER_TRADE_LOG', 'data/paper_trades.jsonl')    # structured per-trade audit log
    PAPER_FREEZE_ON_BAD_PRICE = os.getenv('PAPER_FREEZE_ON_BAD_PRICE', '1') == '1'  # keep last good price instead of writing 0

    # ===================================================================
    # RESOLUTION-STATION VERIFICATION — forecast/observe the EXACT airport the
    # market settles on. Deterministic match first (0 tokens); only calls the
    # cheap verifier LLM when the station is ambiguous or looks different.
    # ===================================================================
    RESOLUTION_VERIFY_ENABLED = os.getenv('RESOLUTION_VERIFY_ENABLED', '1') == '1'
    RESOLUTION_VERIFY_MIN_CONF = float(os.getenv('RESOLUTION_VERIFY_MIN_CONF', '0.6'))
    RESOLUTION_SKIP_ON_UNKNOWN = os.getenv('RESOLUTION_SKIP_ON_UNKNOWN', '0') == '1'
    ML_RESPONSES_URL = os.getenv('ML_RESPONSES_URL', 'https://api.freemodel.dev/v1')
    ML_VERIFY_MODEL = os.getenv('ML_VERIFY_MODEL', 'gpt-5.4-mini')

    # ===================================================================
    # TELEGRAM (optional notifications)
    # ===================================================================
    TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
    TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

    # ===================================================================
    # ML DECISION ENGINE (GPT-5.5 via Freemodel)
    # ===================================================================
    ML_API_URL = os.getenv('ML_API_URL', 'https://vip-sg.freemodel.dev/v1')
    ML_API_KEY = os.getenv('ML_API_KEY', '')
    ML_MODEL = os.getenv('ML_MODEL', 'gpt-5.5')

    # ===================================================================
    # LOGGING
    # ===================================================================
    LOG_FILE = os.getenv('LOG_FILE', 'weather_bot.log')
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')

    # ===================================================================
    # REFERENCE TRADERS (for analysis)
    # ===================================================================
    REFERENCE_TRADERS = [
        '0x594edb9112f526fa6a80b8f858a6379c8a2c1c11',
        '0x331bf91c132af9d921e1908ca0979363fc47193f',
        '0x15ceffed7bf820cd2d90f90ea24ae9909f5cd5fa',
    ]

    # ===================================================================
    # HELPERS
    # ===================================================================
    @classmethod
    def is_paper(cls) -> bool:
        return cls.TRADING_MODE.lower() == 'paper'

    @classmethod
    def is_live(cls) -> bool:
        return cls.TRADING_MODE.lower() == 'live'

    @classmethod
    def is_live_ready(cls) -> bool:
        """Check if all credentials for live trading are set."""
        pk = cls.POLY_PRIVATE_KEY.strip() if cls.POLY_PRIVATE_KEY else ''
        return bool(pk)

    @classmethod
    def get_clob_url(cls) -> str:
        if cls.CLOB_RELAY_URL:
            return cls.CLOB_RELAY_URL.rstrip('/')
        return cls.CLOB_API_URL

    @classmethod
    def get_funder_address(cls) -> str:
        """Resolve funder address (same logic as polymarket-bot-v2)."""
        if cls.POLY_FUNDER_ADDRESS and cls.POLY_FUNDER_ADDRESS.strip():
            return cls.POLY_FUNDER_ADDRESS.strip()
        if cls.POLY_SIGNATURE_TYPE == 2:
            if cls.POLY_PROXY_WALLET and cls.POLY_PROXY_WALLET.strip():
                return cls.POLY_PROXY_WALLET.strip()
            return ''
        if cls.POLY_SIGNATURE_TYPE == 0:
            return cls.derive_wallet_address()
        return ''

    @classmethod
    def derive_wallet_address(cls) -> str:
        pk = cls.POLY_PRIVATE_KEY.strip() if cls.POLY_PRIVATE_KEY else ''
        if not pk:
            return ''
        try:
            from eth_account import Account
            if not pk.startswith('0x'):
                pk = '0x' + pk
            return Account.from_key(pk).address
        except Exception:
            return ''

    @classmethod
    def print_status(cls):
        mode = 'PAPER (DRY-RUN)' if cls.is_paper() else 'LIVE'
        print(f"\n{'='*60}")
        print(f"WEATHER SNIPER v{cls.VERSION} — {cls.VERSION_NAME}")
        print(f"{'='*60}")
        print(f"Mode:        {mode}")
        print(f"Balance:     ${cls.STARTING_BALANCE:.2f} pUSD")
        print(f"Primary:     LateObserved {'ON' if cls.LATE_OBSERVED_ENABLED else 'OFF'} "
              f"(NO-side {'ON' if cls.LATE_OBSERVED_NO_SIDE else 'OFF'}, "
              f"size ${cls.LATE_OBSERVED_SIZE_FLOOR_USD:.0f}-${cls.LATE_OBSERVED_SIZE_MAX_USD:.0f})")
        print(f"Kelly:       factor-sizing {'ON' if cls.KELLY_FACTOR_SIZING_ENABLED else 'OFF'} "
              f"(tiers ${cls.KELLY_TIER_BASE_USD:.0f}/${cls.KELLY_TIER_GOOD_USD:.0f}/"
              f"${cls.KELLY_TIER_VGOOD_USD:.0f}/${cls.KELLY_TIER_PERFECT_USD:.0f})")
        print(f"Portfolio:   guard {'ON' if cls.PORTFOLIO_GUARD_ENABLED else 'OFF'} "
              f"(reserve {cls.PORTFOLIO_RESERVE_PCT:.0%}, per-scan {cls.MAX_DEPLOY_PER_SCAN_PCT:.0%}, "
              f"max {cls.MAX_BUYS_PER_SCAN} buys)")
        print(f"ML:          decision {'ON' if cls.ML_DECISION_ENABLED else 'OFF'} "
              f"(veto>={cls.ML_VETO_CONF:.0%}, size x{cls.ML_SIZE_MIN_MULT}-{cls.ML_SIZE_MAX_MULT})")
        print(f"QuickFlip:   {'ON' if cls.QUICK_FLIP_ENABLED else 'OFF'} "
              f"(edge>={cls.QUICK_FLIP_MIN_EDGE:.0%} or run-change, hold<={cls.QUICK_FLIP_MAX_HOLD_MIN}m, max {cls.QUICK_FLIP_MAX_CONCURRENT})")
        print(f"PeakCluster: {'ON' if cls.PEAK_CLUSTER_ENABLED else 'OFF'} "
              f"(span+/-{cls.PEAK_CLUSTER_SPAN}, {cls.PEAK_CLUSTER_MIN_LEGS}-{cls.PEAK_CLUSTER_MAX_LEGS} legs, cost<{cls.PEAK_CLUSTER_MAX_COST}, HOLD)")
        print(f"SafetyPeak:  {'ON' if cls.SAFETY_PEAK_ENABLED else 'OFF'} "
              f"(grade>={cls.SAFETY_PEAK_MIN_GRADE}, conf>={cls.SAFETY_PEAK_MIN_CONFIDENCE:.0%}, std<={cls.SAFETY_PEAK_MAX_STD}C, peak+1nb, HOLD)")
        print(f"ThesisExit:  {'ON' if cls.THESIS_EXIT_ENABLED else 'OFF'} "
              f"(only ROI<={cls.THESIS_EXIT_MAX_ROI_PCT:.0f}% & entry>={cls.THESIS_EXIT_MIN_ENTRY_PRICE})")
        print(f"Trailing:    {cls.TRAILING_STOP_PCT:.0f}% from peak, armed after {cls.TRAILING_MIN_PEAK_MULT}x entry")
        print(f"Lock-window: LateObs={'Y' if cls.LATE_OBSERVED_TRADE_DECIDED else 'N'} "
              f"Flip={'Y' if cls.QUICK_FLIP_TRADE_DECIDED else 'N'} "
              f"Peak={'Y' if cls.PEAK_BASKET_TRADE_DECIDED else 'N'} "
              f"Cluster={'Y' if cls.PEAK_CLUSTER_TRADE_DECIDED else 'N'} "
              f"Confident={'Y' if cls.CONFIDENT_TRADE_DECIDED else 'N'}")
        print(f"Min Edge:    {cls.MIN_EDGE_TO_ENTER*100:.0f}% | fee-aware taker={cls.ASSUME_TAKER_FILLS}")
        print(f"Liquidity:   {'STRICT' if cls.LIQUIDITY_STRICT_BLOCK else 'adaptive'} (thin x{cls.LIQUIDITY_THIN_SIZE_MULT})")
        print(f"Paper:       realistic-fill={cls.PAPER_REALISTIC_FILL} preclose>={cls.PAPER_PRECLOSE_LOCK_PCT:.0%} freeze={cls.PAPER_FREEZE_ON_BAD_PRICE}")
        print(f"Resolve:     station-verify={'ON' if cls.RESOLUTION_VERIFY_ENABLED else 'OFF'} (min_conf={cls.RESOLUTION_VERIFY_MIN_CONF})")
        print(f"Scan:        every {cls.SCAN_INTERVAL_SECONDS}s")
        print(f"{'='*60}\n")
