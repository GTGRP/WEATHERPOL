"""
Runtime settings store — change the bot's behavior live (from Telegram) without
editing .env or restarting. Overrides are applied as attributes on `Config`
(read at call-time across the code) and persisted to data/runtime_settings.json
so they survive restarts.

Two kinds of tunables:
  BOOL_KEYS — on/off toggles (strategies, strict liquidity, master trading switch)
  NUM_KEYS  — numeric gates with (min, max, step, is_int)
"""

import json
import os
from typing import Tuple, Dict

from config import Config
from logger import log

SETTINGS_PATH = 'data/runtime_settings.json'

# On/off toggles exposed to Telegram (tick boxes).
BOOL_KEYS = [
    'TRADING_ENABLED',
    'SNIPER_ENABLED', 'SPREAD_ENABLED', 'CONFIDENT_ENABLED', 'STABILITY_ENABLED',
    'LIQUIDITY_GUARD_ENABLED', 'LIQUIDITY_STRICT_BLOCK',
    'GRADE_SIZING_ENABLED', 'SKIP_DECIDED_MARKETS', 'ML_ENABLED',
]

# Numeric gates: key -> (min, max, step, is_int)
NUM_KEYS: Dict[str, tuple] = {
    'BASKET_MAX_COST':          (0.50, 0.99, 0.05, False),
    'GRADE_MIN_TO_TRADE':       (0.00, 1.00, 0.05, False),
    'SNIPER_MIN_GRADE':         (0.00, 1.00, 0.05, False),
    'SNIPER_MIN_CONFIDENCE':    (0.00, 1.00, 0.05, False),
    'SNIPER_MIN_PROBABILITY':   (0.00, 1.00, 0.02, False),
    'BASKET_TIGHT_GRADE':       (0.00, 1.00, 0.05, False),
    'BASKET_TIGHT_CONFIDENCE':  (0.00, 1.00, 0.05, False),
    'MIN_EDGE_TO_ENTER':        (0.00, 0.50, 0.02, False),
    'LIQUIDITY_THIN_SIZE_MULT': (0.10, 1.00, 0.10, False),
    'STABILITY_EARLY_EXIT_PRICE': (0.50, 0.99, 0.05, False),
    'MAX_BET_PCT':              (0.05, 1.00, 0.05, False),
    'HIGH_TEMP_LOCK_HOUR':      (0, 23, 1, True),
}


def _coerce(key: str, value):
    if key in BOOL_KEYS:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ('1', 'true', 'on', 'yes', 'y')
    spec = NUM_KEYS.get(key)
    if spec:
        lo, hi, _step, is_int = spec
        v = int(round(float(value))) if is_int else float(value)
        return max(lo, min(hi, v))
    return value


def set_value(key: str, value) -> Tuple[bool, str]:
    """Set a tunable to an explicit value (used by /set KEY VALUE)."""
    key = key.upper()
    if key not in BOOL_KEYS and key not in NUM_KEYS:
        return False, f"unknown setting '{key}'"
    try:
        v = _coerce(key, value)
    except (ValueError, TypeError):
        return False, f"invalid value for {key}: {value!r}"
    setattr(Config, key, v)
    _persist()
    return True, f"{key} = {v}"


def toggle(key: str) -> Tuple[bool, str]:
    """Flip a boolean toggle (used by tick-box buttons / /toggle KEY)."""
    key = key.upper()
    if key not in BOOL_KEYS:
        return False, f"'{key}' is not a toggle"
    cur = bool(getattr(Config, key, False))
    setattr(Config, key, not cur)
    _persist()
    return True, f"{key} = {not cur}"


def bump(key: str, direction: int) -> Tuple[bool, str]:
    """Step a numeric gate up (+1) or down (-1) by its configured step."""
    key = key.upper()
    spec = NUM_KEYS.get(key)
    if not spec:
        return False, f"'{key}' is not a numeric gate"
    lo, hi, step, is_int = spec
    cur = float(getattr(Config, key, lo))
    nxt = cur + direction * step
    nxt = max(lo, min(hi, nxt))
    if is_int:
        nxt = int(round(nxt))
    else:
        nxt = round(nxt, 4)
    setattr(Config, key, nxt)
    _persist()
    return True, f"{key} = {nxt}"


def snapshot() -> Tuple[Dict[str, bool], Dict[str, float]]:
    bools = {k: bool(getattr(Config, k, False)) for k in BOOL_KEYS}
    nums = {k: getattr(Config, k, None) for k in NUM_KEYS}
    return bools, nums


def _persist():
    try:
        os.makedirs('data', exist_ok=True)
        data = {k: bool(getattr(Config, k, False)) for k in BOOL_KEYS}
        data.update({k: getattr(Config, k, None) for k in NUM_KEYS})
        with open(SETTINGS_PATH, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log.debug(f"settings persist failed: {e}")


def load_into_config():
    """Apply persisted overrides onto Config at startup."""
    try:
        if not os.path.exists(SETTINGS_PATH):
            return
        with open(SETTINGS_PATH) as f:
            data = json.load(f)
        n = 0
        for k, v in data.items():
            if k in BOOL_KEYS or k in NUM_KEYS:
                setattr(Config, k, _coerce(k, v))
                n += 1
        if n:
            log.info(f"Loaded {n} runtime settings overrides from {SETTINGS_PATH}")
    except Exception as e:
        log.debug(f"settings load failed: {e}")
