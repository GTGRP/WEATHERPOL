# WEATHERPOL — Session Memory

_Last updated: 2026-06-21 (Asia/Calcutta). Maintainer: Gt._

This file is the running source of truth for what has been shipped, what is still
pending, and the **one manual edit** you must make by hand. Keep it updated as
work lands.

---

## 0. THE ONE MANUAL EDIT YOU MUST MAKE  ⚠️

**File:** `dashboard.py`
**Method:** `_evaluate_market`
**What:** pass `market_type` into the probability engine so high/low-temperature
markets resolve against the day's MAX/MIN (not a single hourly reading).

**WHEN:** Do this whenever you want the daily MAX/MIN refinement active. Until
you do, the engine still works — it just defaults to the hourly temperature
(`temp_c`), which is harmless but slightly less accurate for
`highest_temperature` / `lowest_temperature` markets.

**WHERE / EXACT LINE:** In `dashboard.py`, inside `_evaluate_market`, find this
exact call (the variable is `bucket_probs`, and the call spans 3 lines):

```python
# BEFORE (current code — find this):
bucket_probs = self.engine.estimate_bucket_probabilities(
    forecasts, buckets, target_time
)

# AFTER (add market_type=market.market_type as the last argument):
bucket_probs = self.engine.estimate_bucket_probabilities(
    forecasts, buckets, target_time, market_type=market.market_type
)
```

**Notes:**
- The ONLY change is appending `, market_type=market.market_type` after
  `target_time` on the middle line. Nothing else changes.
- `market` is the `WeatherMarket` already in scope in `_evaluate_market`; it has
  `.market_type` ∈ {`highest_temperature`, `lowest_temperature`}.
- The engine signature already accepts `market_type: str = None`, so no other
  file needs editing.
- **WHY it couldn't be auto-pushed:** `dashboard.py` (~74 KB) is too large for
  the GitHub file tool to reproduce in full without risking a truncated/
  corrupted write, so this one line is left for a manual edit rather than
  risking the whole file.

---

## 1. SHIPPED — live on `main`

### A. ML wiring fix (ML stopped vetoing everything)
- **Root cause:** `forecast_temp` was never passed to the LLM (defaulted such
  that the model vetoed nearly all signals).
- **Fix:** `ml/decision_engine.py` `validate_signal(...)` now receives
  `forecast_temp` (+ `n_models`). Landed via PR #8 (merge `a9fffeeb`).

### B. ML settings tab now opens (was crashing)
- **Root cause:** `bot/telegram_ui.py` `_settings_view` called an undefined
  `self._label(k)` on the string/choice row. Only the **ML tab** has string
  keys (model dropdowns), so opening it raised `AttributeError` — that's why
  only that tab failed.
- **Fix:** replaced with `self._LABELS.get(k, k)` on all rows; added `_LABELS`
  entries for `DRAWDOWN_GATE_ENABLED` ("Drawdown-Gate") and
  `QUICK_FLIP_BOOK_OR_CUT` ("Flip book-or-cut").

### C. Risk tab: book-or-cut + drawdown controls
- `bot/settings_store.py`: exposed `QUICK_FLIP_BOOK_OR_CUT` (enable/disable) and
  the drawdown gate + daily/weekly drawdown limits + cooldown in the **Risk**
  group. (Config knobs already existed in `config.py`; this just made them
  editable from settings.)
  - New BOOL key surfaced: `DRAWDOWN_GATE_ENABLED`.
  - New NUM keys surfaced: `MAX_DAILY_DRAWDOWN_PCT`, `MAX_WEEKLY_DRAWDOWN_PCT`,
    `DRAWDOWN_COOLDOWN_MINUTES`.
  - Risk group title: `Risk & Sizing` → `Risk, Drawdown & Sizing`.

### D. Prediction accuracy — more data, tighter ensemble
- **Two new global providers** wired into `data/weather_fetcher.py` `fetch_all`,
  each gated on its API key with try/except failover:
  - **WeatherAPI.com** — `forecast.json`, 3-day hourly; model `WAPI`; weight 0.80.
  - **Visual Crossing** — Timeline API, hourly; model `VC`; weight 0.80.
- **Richer fields** captured from every source where available: humidity, wind,
  precipitation, cloud cover, and each day's **max/min** temperature.
- **Open-Meteo** now also pulls `temperature_2m_max` / `temperature_2m_min`
  per model.
- **Daily MAX/MIN by market type:** `data/probability_engine.py`
  `estimate_bucket_probabilities(...)` now accepts optional `market_type`:
  - `highest_temperature` → keys off `temp_max_c`
  - `lowest_temperature`  → keys off `temp_min_c`
  - otherwise / missing  → falls back to hourly `temp_c`
  - `_weighted_ensemble` takes an optional `temp_field` and uses it per-point
    when present, else `temp_c`.
  - Model weights added: `WAPI` 0.80, `VC` 0.80.
- **PRs:** shipped via PR #9 (squash merge `fd0da4c`).

---

## 2. ACTION ITEMS FOR YOU (Gt)

1. **Set the two new env vars in Railway, then redeploy latest `main`:**
   - `WEATHERAPI_API_KEY=...`
   - `VISUALCROSSING_API_KEY=...`
   - Without these keys, the two new providers are **silently skipped** (no
     errors, just fewer ensemble members).
2. **Make the manual `dashboard.py` edit** (see section 0).
3. **Rotate exposed secrets** (treat as compromised): the Railway token and the
   Alchemy key embedded in `POLYGON_RPC_URL`. Never commit/keep secrets in plain
   text on pages or in the repo.
4. **Never commit secrets:** `ML_API_KEY`, `POLY_*`, Telegram token, and the
   weather API keys must live in env only.

---

## 3. PENDING / DEFERRED (not yet implemented)

- [ ] **`dashboard.py` `market_type` plumbing** — the one manual line in
      section 0 (deferred because the file is too large to auto-push safely).
- [ ] **Thread live forecast into dashboard `_ml_adjust`** — currently
      `self.ml.validate_signal(city, bucket_label, entry_price, our_prob, edge)`
      passes **no** `forecast_temp`. Wiring the live forecast temp here would let
      the LLM reason with the actual prediction (the engine fix in 1.A already
      supports the arg).
- [ ] **Delete scratch branch `pm-flip-fix`** (`302e7221`) — leftover, safe to
      remove.
- [ ] **Req-28 family (NOT touched):** sniper / confident / spread / stability
      tuning. Explicitly leave `late_observed_no` logic alone.

---

## 4. KNOWN PRE-EXISTING ISSUES (intentionally NOT fixed)

- **`weather.gov` backup is US-only and has a broken `points_url` f-string**
  (`f"{{https://api.weather.gov/points/{lat:.4f}}},{lon:.4f}"`). Left untouched —
  out of scope; the source is geofenced to the US anyway.
- **`_weighted_ensemble` model-name parsing** uses
  `model_key.split('_', 1)[1]`, which mis-parses underscored source names
  (`open_meteo` → `meteo`, `weather_gov` → `gov`) and falls back to the default
  0.7 weight for those. The new providers (`weatherapi` → `WAPI`,
  `visualcrossing` → `VC`) have no underscore in the model token and resolve to
  their intended 0.80 weight correctly. Documented; not changing parsing now to
  avoid disturbing existing behavior.

---

## 5. ARCHITECTURE QUICK REFERENCE

- **Settlement / truth:** Polymarket's resolved value is the source of truth.
  Weather APIs are **confirmation only**, never settlement.
- **Forecast flow:** `WeatherFetcher.fetch_all(lat, lon, city, target_time)` →
  list of `ForecastPoint` → `ProbabilityEngine.estimate_bucket_probabilities(...)`
  → normal-CDF per bucket → `find_edge(...)` (edge > 0.05 & confidence > 0.3).
- **`ForecastPoint` fields:** `source, model, location, timestamp, temp_c,
  temp_min_c, temp_max_c, humidity_pct, wind_speed_kmh, precip_mm,
  cloud_cover_pct, confidence, fetched_at`.
- **`WeatherMarket.market_type`** ∈ {`highest_temperature`, `lowest_temperature`};
  also `.measurement_date`, 11 buckets, YES+NO tokens/prices.
- **Forecast sources & weights:** ECMWF/ECMWF_IFS04 0.95, HRRR 0.88, ICON 0.85,
  UKMO 0.83, JMA 0.82, GFS/NWS 0.80, **WAPI 0.80**, **VC 0.80**, GEM 0.75,
  OWM 0.70 (default 0.70).
- **Settings store:** groups main/ml/risk/lateobs/quickflip/peaker/cluster/
  exits/sniper; persisted to `data/runtime_settings.json`.
- **ML:** `mini` model for trade decisions, `gpt-5.5` for `/mlanalysis`; LLM when
  key present, local fallback otherwise.
- **Deploy:** Railway; start command `python dashboard.py`.
