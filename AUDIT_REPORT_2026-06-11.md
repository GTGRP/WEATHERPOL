# WEATHERPOL — Repository Audit Report

**Audit taken on:** June 11, 2026 (~16:30 IST / ~11:00 UTC)
**Auditor:** Notion AI (principal-level technical audit, requested by GTGRP)
**Repo:** https://github.com/GTGRP/WEATHERPOL — branch `main`
**Commit audited:** `cf4a933…` (the HEAD at audit time; pre-fix)
**Method:** Read-only static analysis via GitHub MCP. No code was modified during the audit itself. The
implementation that followed the audit (commit `5dce428…` and the two pushes before it) is recorded in
`SESSION_MEMORY.md`, not here.
**Constraint honored:** Analysis only — this document made no code changes.

---

## 1. Executive Summary

**Overall health grade: C** (a capable, feature-rich prototype with one correctness bug that defeats its entire
purpose, plus thin automated-test/CI coverage). The project is a Polymarket weather-market trading bot in Python
with a genuinely strong, well-thought-out domain edge (forecast the EXACT resolution airport; trade the
observed/locked daily extreme). The code is readable, the configuration is unusually well-documented, and the
safety design (paper mode, fee-aware gating, conserved-PnL ledger, station verification) is above prototype grade.

However, at audit time the bot **does not place a single trade** despite generating signals — a single blanket
"outcome decided" gate `return`s at the top of `_evaluate_market` before any strategy runs, so the primary
Late-Observed strategy never executes. This is the dominant risk and the user's reported symptom. Secondary risks:
the 5c price floor silently blocks the cheap sub-5c tails the strategy depends on, and there is effectively no
enforced CI / automated regression safety net around the core trading logic.

- **Top 3 risks:** (1) Blanket decided-gate kills all trading (Critical). (2) 5c sellability floor blocks the
  cheap-tail edge for hold-to-resolution legs (High). (3) No CI / lint / lockfile and the core placement logic is
  not unit-tested, so any refactor can silently break trading (High).
- **Top 3 opportunities:** (1) Make the decided-gate per-strategy so observation strategies trade the lock window
  (immediately unblocks trading). (2) Wire the already-built but dormant `strategies/quick_flip.py` into the scan
  loop (the user's requested "fast mispricing" strategy already exists). (3) Add stable Asian markets + stations
  and round-robin the free Open-Meteo budget to scale coverage cheaply.

---

## 2. Repo Map (Phase 1 — Discovery & Mapping)

**Purpose:** Automated sniper for Polymarket daily high/low TEMPERATURE markets. Edge thesis: most retail traders
forecast the city center, but each market resolves to a SPECIFIC airport station (Wunderground/METAR). The bot
forecasts the exact station and, once the day's extreme is observed/locked, trades the bucket the recorded value
guarantees while the order book still prices stale forecast uncertainty.

**Stack / runtime:** Python 3.13; `python-dotenv`, `pytz`, `python-dateutil`, `pandas`, `requests`,
`py_clob_client`, `web3`/`eth_account` (live trading), Telegram bot UI. Deployed on Railway (`railway.toml`,
`Procfile`, `runtime.txt`). Single long-running process (`dashboard.py` → `run_loop`).

**Entry points:** `dashboard.py main()` → `WeatherBot.run_loop()` (continuous) / `--once` / `--status` / `--live`.

**Main control/data flow:**
`MarketScanner.scan_weather_markets` (slug-based, parallel) → for each market `WeatherBot._evaluate_market`:
resolve exact station → outcome-decided gate → `WeatherFetcher.fetch_all` (multi-model Open-Meteo) →
`ProbabilityEngine.estimate_bucket_probabilities` → StabilityEngine GRADE → strategies (LateObserved primary;
PeakBasket / Confident demoted) → `_place` (grade gate + price floor + LiquidityGuard maker entry) →
`PositionManager.add_position` → paper realism / resolution via `MarketResolver` → Telegram notify.

**Key directories (one line each):**
- `dashboard.py` — main loop, single `_place` placement path for all strategies, dashboard render. (God-file: ~600 lines.)
- `config.py` — central, heavily-documented `Config` class; all tunables are env-overridable.
- `data/` — market scanning, weather fetching, probability/stability engines, liquidity guard, CLOB client,
  market timing (`outcome_decided`, `city_local_now`), resolution rules, weather stations, market resolver.
- `strategies/` — late_observed_temp (primary), peak_basket, confident, quick_flip (dormant), selective_sniper,
  spread, unique_edges, sniper.
- `trading/` — position_manager (lifecycle + paper realism + ledger), paper_engine (pure realism math), executor.
- `ml/` — decision_engine (GPT-5.5, dormant), local_model (XGBoost), resolution_verifier (LLM station check).
- `bot/` — telegram_ui, settings_store (runtime overrides).
- `backtest/` — honest no-lookahead backtests + Modal large-scale calibration scripts.
- `tests/` — 3 plain-python test files (test_overhaul, test_resolution_rules, test_strategy). No pytest/CI config.

**What surprised me (positively):** the config file documents the *intent* of behaviors (cheap-tail allowance,
"only hard-skip when fully over", per-strategy demotion) that the dashboard code did NOT yet implement — the docs
ran ahead of the code. The settlement design is correctly disciplined: weather is CONFIRMATION only, Polymarket's
resolved outcome is the source of truth.

---

## 3. Audit Report (Phase 2 — evidence-based, severity-rated)

Findings labeled **[FACT]** (verifiable in code) or **[JUDGMENT]** (assessment).

### Architecture & design
- **[FACT] CRITICAL — Blanket decided-gate stops ALL trading.** In `dashboard.py` `_evaluate_market`, the
  `if Config.SKIP_DECIDED_MARKETS:` block calls `outcome_decided(...)` and, when `decided` is true, logs
  `⛔ DECIDED` and `return`s — terminating the function before the Late-Observed / PeakBasket / Confident blocks
  run. Since the Late-Observed edge exists PRECISELY when the extreme is locked (i.e. `decided` is true), the
  primary strategy is structurally unreachable. Consequence: signals are generated upstream in other code paths,
  but `_evaluate_market` never reaches placement → zero trades. This matches the user's report exactly.
- **[JUDGMENT] MEDIUM — `dashboard.py` is a god-file.** `_evaluate_market` mixes coordinate resolution, gating,
  forecasting, probability, grading, and three strategy execution blocks; `_place` mixes grade gate, price floor,
  liquidity adaptation, sizing, and exit policy. Hard to unit-test in isolation; high churn risk.
- **[JUDGMENT] LOW — strategy blocks are near-duplicated.** Each strategy block repeats the
  signal→legs→`_place`→`notify_trade` shape; a small dispatch table would reduce drift.

### Code quality
- **[FACT] HIGH — 5c sellability floor blocks the cheap-tail edge.** `_place` rejects any leg with
  `entry_price < Config.MIN_ENTRY_PRICE` (~0.05) for ALL strategies, including hold-to-resolution legs. The
  reference 90%-WR wallet lives in sub-5c tails, and the config docstring explicitly says hold legs should bypass
  this floor — but the code does not. Consequence: even after the decided-gate is fixed, the most profitable
  entries are silently dropped (`⏭️ PRICE FLOOR`).
- **[FACT] MEDIUM — dormant code in the decision path.** The GPT-5.5 `ml/decision_engine.py` is NOT in the trade
  decision path (confirmed in prior session notes); `strategies/quick_flip.py` and `selective_sniper.py` exist but
  are never instantiated/called by `dashboard.py`. This is dead weight and a source of confusion.
- **[FACT] LOW — `weather.gov` points URL has a stray `{`.** Noted in `data/weather_fetcher.py`; low impact
  because Open-Meteo is the primary source, but it will throw for the US-gov fallback path.
- **[JUDGMENT] LOW — broad `except Exception` swallowing.** Many `try/except Exception: log.debug(...)` blocks
  (station resolve, observed-state fetch, stability). Reasonable for a resilient long-running bot, but they can
  mask systematic failures; pair with a per-cycle rejection/skip summary.

### Security
- **[FACT] OK — no hardcoded secrets.** Keys/wallet come from env (`os.getenv`) via `config.py`; `.env.example`
  documents them. Live trading requires `POLY_PRIVATE_KEY` only from env. One sentence: healthy.
- **[JUDGMENT] LOW — private key handled in-process for live mode** (`derive_wallet_address` via `eth_account`).
  Expected for a self-custody trading bot; ensure Railway env vars are not logged.

### Testing
- **[FACT] HIGH — core placement/gating logic is not unit-tested.** `tests/` has 3 files but no coverage of
  `_place` floors, the decided-gate, or `_evaluate_market` strategy gating. There is no pytest/CI config, so tests
  are run ad hoc. Consequence: the Critical gate bug shipped undetected.
- **[JUDGMENT] MEDIUM — paper-engine math IS tested** (prior `test_paper_modules.py`, 24/24) — good — but the
  dashboard wiring around it is not.

### Performance
- **[FACT] OK — scanning is parallel + cached.** `MarketScanner` uses a `ThreadPoolExecutor(10)` + 30s cache;
  weather fetch batches models. One sentence: healthy for the workload.
- **[FACT] MEDIUM — Open-Meteo budget is single-endpoint.** All forecast calls hit one URL; the free tier is
  ~10k/day and a single IP can be rate-limited. No round-robin or configurable cache TTL at audit time.
- **[JUDGMENT] LOW — `data/paper_trades.jsonl` grows unbounded.** Append-only audit log with no rotation/cap.

### Dependencies
- **[FACT] MEDIUM — no lockfile.** `requirements.txt` is unpinned-ish; no `requirements.lock`/hashes. Repro builds
  can drift. `web3`/`pandas`/`xgboost` are heavy; XGBoost is only used by a dormant local model.

### DevEx & operations
- **[FACT] HIGH — no CI/CD, no enforced lint/format.** No GitHub Actions, no ruff/flake8/black gate. Logging is
  good (rich emoji-tagged log lines), deployment is clean (Railway). The gap is purely the automated safety net.
- **[FACT] OK — observability is strong.** Per-trade structured JSONL log, conserved-PnL ledger assert, dashboard
  with token usage. Above prototype grade.

### Documentation
- **[FACT] MEDIUM — config docstrings describe behavior the code didn't implement.** `config.py` documents the
  cheap-tail bypass, "only hard-skip when fully over", and per-strategy demotion, but `dashboard.py` at audit time
  did none of these. Stale-relative-to-code docs that contradict behavior. (The follow-up implementation closed
  this gap.)
- **[FACT] OK — `SESSION_MEMORY.md` is an excellent running diary.** Chronological, append-only, detailed.

### Strengths (preserve these)
- Disciplined settlement design (Polymarket = truth, weather = confirmation).
- Genuine, documented domain edge (exact-station forecasting + observed-lock).
- Heavily-documented, fully env-overridable configuration.
- Realistic paper engine with conserved-PnL ledger and per-trade audit log.
- Resilient long-running loop with graceful SIGTERM/SIGINT shutdown.

---

## 4. Improvement Strategy (Phase 3)

**Themes that explain most findings:**
1. **One global gate doing a per-strategy job.** The decided-skip is correct for forecast strategies and wrong for
   observation strategies. *Target state:* gate per strategy; only hard-skip when the local day is fully over.
   *Principle:* gates should encode the edge condition, not a blanket assumption.
2. **Code lagging behind its own documented intent.** Config promises behavior the code doesn't do. *Target
   state:* the placement floors and gating match the documented rules. *Principle:* docs and code converge.
3. **No automated safety net around core logic.** *Target state:* `_place`/gating logic extracted into pure,
   unit-tested functions with a CI gate. *Principle:* the money path is the most-tested path.
4. **Dormant complexity.** Dead ML/strategy modules add confusion. *Target state:* wire what's needed
   (QuickFlip), document or remove the rest.

**Explicit trade-offs (NOT fixing now, and why):** Don't introduce a full `open→locked→ended→settled→redeemed`
state machine (current flags cover the practical need; high effort, low marginal payoff). Don't migrate off
`requests`/threads to async (works fine at this scale). Don't remove the dormant GPT-5.5/XGBoost path yet — it's
harmless and may be revived; just document it. These match a maturing-prototype, not an enterprise service.

**Definition of done (measurable):** bot places trades in paper within one scan cycle on a locked market; sub-5c
hold legs are accepted; QuickFlip fires; zero Critical findings; core placement/gating logic has unit tests; CI
fails on lint errors.

---

## 5. Task Plan (Phase 4)

### Quick wins (high impact, S effort — do immediately)
- **QW1 — Per-strategy decided-gate.** Replace the blanket `return` with: hard-skip only when the local day is
  fully over; otherwise flag a lock window and gate each strategy by a `*_TRADE_DECIDED` flag. *Files:* dashboard.py,
  config.py. *Acceptance:* a locked market reaches the Late-Observed block and places. *Risk:* medium (core path).
- **QW2 — Cheap-tail floor fix.** Hard dust floor for all; 5c floor only for non-hold legs. *Files:* dashboard.py,
  config.py. *Acceptance:* a 2–4c hold leg is accepted; a 0.5c leg is rejected as dust.
- **QW3 — Wire QuickFlip.** Instantiate + call `strategies/quick_flip.py` in the loop. *Files:* dashboard.py.
- **QW4 — Open-Meteo round-robin + cache TTL.** *Files:* config.py, data/weather_fetcher.py.
- **QW5 — Add stable Asian markets + stations.** *Files:* data/market_scanner.py, data/weather_stations.py,
  data/weather_fetcher.py.

### Milestone 0 — Safety net (before further refactors)
- **M0.1 — Extract `decide_placement()` pure function** from `_place` (grade gate + floors + sizing) and unit-test
  it. Effort M. Risk low. Depends on QW1/QW2.
- **M0.2 — Add CI (GitHub Actions): pytest + ruff; pin a lockfile.** Effort M. Risk low.

### Milestone 1 — Critical fixes (correctness)
- QW1, QW2 (above). Effort S each. Risk medium. No dependencies. (These ARE the critical fixes.)

### Milestone 2 — High-leverage improvements
- QW3, QW4, QW5. Effort S–M. Risk low–medium.
- **M2.1 — Per-cycle rejection summary log line** (counts of GRADE/PRICE/LIQ/DECIDED skips) for fast diagnosis.

### Milestone 3 — Quality & polish
- Fix `weather.gov` points URL stray `{`; cap `paper_trades.jsonl`; document or remove dormant GPT-5.5/XGBoost
  path; extend round-robin to `observed_weather.py`; bucket-center alignment audit.

### Top-3 implementation sketches
1. **Per-strategy decided-gate (QW1).** Approach: compute `decided, why = outcome_decided(...)`; if decided,
   compute `fully_over = city_local_now(lat,lon).date() > market.measurement_date.date()`; `return` only when
   `fully_over` (log `⛔ OVER`); else set `in_lock_window=True` (log `🔓 LOCK WINDOW`). Gate each strategy:
   `if ENABLED and (not in_lock_window or getattr(Config,'<S>_TRADE_DECIDED', default))`. Gotcha: scope
   `fully_over` so `in_lock_window` stays False when `decided` is False; import `city_local_now`.
2. **Cheap-tail floor (QW2).** Approach: `abs_floor = getattr(Config,'ABS_PRICE_FLOOR',0.01)`; reject
   `entry_price < abs_floor` (dust) always; reject `(not hold_hint) and entry_price < MIN_ENTRY_PRICE`. Mirror at
   the maker-reprice (`fill_price`). Gotcha: thin-book logic later sets `hold_hint=True`, so apply the maker check
   after that — hold legs forced by a thin book may rest cheap.
3. **Wire QuickFlip (QW3).** Approach: `from strategies.quick_flip import QuickFlipStrategy`; `self.quick_flip=...`;
   after the Late-Observed block call `self.quick_flip.evaluate(title, bucket_probs, prices, bids, token_ids,
   balance, city, market_type)`; for each signal `_place(..., hold_hint=False, early_exit_price=target_price,
   apply_grade_size=False, strategy='quick_flip')`. Gotcha: confirm the `evaluate` signature + signal fields from
   the module before wiring; pass `market_prices` as the bids fallback if a separate bids dict isn't available.

---

## 6. Open Questions (need a human decision)
- **Live vs paper:** how many paper cycles / what win-rate threshold before flipping to `--live`?
- **Cheap-tail risk appetite:** confirmed sub-5c is fine if net-positive after fees — is there a max % of balance
  per cheap-tail leg you want enforced beyond the existing Kelly/fraction caps?
- **Asian-market selection:** which specific Asian cities are "stable + good-winning" enough to keep? (Audit added
  delhi/bangkok/shanghai/osaka/jakarta/manila/kuala-lumpur — should any be dropped?)
- **Dormant ML:** keep the GPT-5.5 + XGBoost path for future revival, or remove it to cut dependency weight?
- **Open-Meteo scaling:** do you have a second endpoint / self-hosted mirror to add to the round-robin, or stay
  single-endpoint within the 10k/day budget?

---

## 7. Post-audit note (development that followed this report)

**This audit was taken on June 11, 2026 (~16:30 IST).** After delivering it, the user authorized code changes
(via a 3-question survey), and further development + pushes were made the same afternoon to act on these findings:
- `187b919` — config.py (adaptive liquidity, cheap-tail floors, QuickFlip enable, Open-Meteo round-robin config).
- `cf4a933` — late_observed_temp.py + market_scanner.py + weather_stations.py + weather_fetcher.py (sub-5c hold
  tails, stable Asian markets + stations, Open-Meteo round-robin).
- `5dce428` — dashboard.py + config.py (THE zero-trades fix: per-strategy decided gate, cheap-tail `_place`
  floors, QuickFlip wired into the loop, `*_TRADE_DECIDED` flags).

**For the full chronological record of what was reported, what the user instructed, what was suggested, and what
was implemented and why — refer to `SESSION_MEMORY.md` (June 11, 2026 entry).** That diary is the source of truth
for ongoing development; this file is a point-in-time snapshot of the pre-fix audit.
