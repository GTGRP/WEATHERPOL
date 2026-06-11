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
    # PROFIT-TAKE: sell if price rises above this BEFORE resolution (early profit)
    EARLY_PROFIT_THRESHOLD = float(os.getenv('EARLY_PROFIT_THRESHOLD', '0.60'))
    # For confident strategy: never sell (hold to resolution)
    CONFIDENT_NEVER_SELL = os.getenv('CONFIDENT_NEVER_SELL', '1') == '1'

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
    # OUTCOME-DECIDED GATE — only HARD-skip a market once its measurement day is
    # FULLY OVER in the city's local time (value recorded, just awaiting UMA).
    # The intraday lock-hour window (same local day, after the peak) is exactly
    # where the late-observed strategy has its edge, so it is NOT skipped — the
    # bot trades the locked-but-unresolved extreme.
    # ===================================================================
    SKIP_DECIDED_MARKETS = os.getenv('SKIP_DECIDED_MARKETS', '1') == '1'
    HIGH_TEMP_LOCK_HOUR = int(os.getenv('HIGH_TEMP_LOCK_HOUR', '18'))       # local hour after which the day's HIGH is considered set

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
              f"(NO-side {'ON' if cls.LATE_OBSERVED_NO_SIDE else 'OFF'})")
        print(f"QuickFlip:   {'ON' if cls.QUICK_FLIP_ENABLED else 'OFF'}")
        print(f"Min Edge:    {cls.MIN_EDGE_TO_ENTER*100:.0f}% | fee-aware taker={cls.ASSUME_TAKER_FILLS}")
        print(f"Kelly:       {cls.KELLY_FRACTION}")
        print(f"Liquidity:   {'STRICT' if cls.LIQUIDITY_STRICT_BLOCK else 'adaptive'} (thin x{cls.LIQUIDITY_THIN_SIZE_MULT})")
        print(f"Paper:       realistic-fill={cls.PAPER_REALISTIC_FILL} preclose>={cls.PAPER_PRECLOSE_LOCK_PCT:.0%} freeze={cls.PAPER_FREEZE_ON_BAD_PRICE}")
        print(f"Resolve:     station-verify={'ON' if cls.RESOLUTION_VERIFY_ENABLED else 'OFF'} (min_conf={cls.RESOLUTION_VERIFY_MIN_CONF})")
        print(f"Scan:        every {cls.SCAN_INTERVAL_SECONDS}s")
        print(f"{'='*60}\n")
