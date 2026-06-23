# SESSION MEMORY -- 2026-06-23 (Notion AI / GTGRP)

> NEW DATED session-memory file. The rolling diary `SESSION_MEMORY.md` is the
> append-only history ("all AI agents add changes at the END, append at last,
> don't delete or add in front"). Going forward, per the user's instruction,
> each session ALSO gets its own dated `SESSION_MEMORY_YYYY-MM-DD.md` so the
> diary is never overwritten/summarised in place again.
>
> IMPORTANT (diary preservation): the FULL rolling diary is preserved in git
> history at commit `e60166ed` (blob `466f148a`) and is also mirrored across the
> existing dated snapshots (`SESSION_MEMORY_2026-06-15/-17/-19/-20.md`). NO diary
> content was lost. To put the full diary back as the live `SESSION_MEMORY.md`,
> use GitHub -> SESSION_MEMORY.md -> History -> the version at `e60166ed` ->
> Revert/Restore (one click, byte-for-byte lossless). A future agent with a
> file-copy tool can also restore it directly.

Settlement design LOCKED: paper closes ~99% realistic; weather-API = CONFIRMATION
only; Polymarket's resolved value = the source of truth. NOT changed. Tone:
normal technical. Branch policy this period: push to `dev` ONLY; `main` untouched.

---

## This session (Jun 23) -- what changed

### 1. main vs dev sync (earlier today)
- `dev` was a strict ancestor of `main` (missing PR#9 5-provider weather +
  ML-settings fix + Risk-tab book-or-cut/drawdown gate, the telegram_ui
  truncation recovery, and the concise SESSION_MEMORY).
- Synced main -> dev via PR #10, merge commit `8d7a765a`. `dev` now carries all
  of main's work. `main` left at `e079d377` (unchanged).
- Wired `dashboard.py` to pass `market_type` into
  `probability_engine.estimate_bucket_probabilities(forecasts, buckets,
  target_time, market_type=market.market_type)` (the `bucket_probs` call in
  `_evaluate_market`). Commit `151824d3` on `dev` (verified: clean +1/-1 diff,
  file intact at 74182 bytes). This makes high/low markets use the daily
  MAX/MIN temp field.

### 2. Req-33 -- quick_flip dedicated exit system (THE losing-big fix)
User report: "if loss it must exit at 5 percent loss, but I saw it does not exit
and had 70 percent loss." Confirmed root cause in
`trading/exit_policies.check_flip_exits`:
- The -5% stop was checked AFTER the `hold_to_resolution` guard.
- When a FLAT flip hit the hold cap with `QUICK_FLIP_BOOK_OR_CUT` OFF, it was
  marked `hold_to_resolution=True`, which then EXEMPTED it from the stop on
  every later cycle -> it rode the loss down to -70%.

Fix (commit on `dev`, file `trading/exit_policies.py`):
- The hard -5% stop (`flip_stop`) is now the FIRST check for every quick_flip
  position, run BEFORE the `hold_to_resolution` guard. A flip can never again
  ride a loss past -5%, even if parked to resolution.
- Gated by NEW toggle `QUICK_FLIP_HARD_STOP_ENABLED` (default ON) so it can be
  turned OFF from Telegram.
- Upside path unchanged and matches the user's spec: at >= +10% target, if ML
  is enabled it asks BOOK-vs-HOLD and may let the position run to 15/20/40%+
  (`QUICK_FLIP_USE_ML_PROFIT` / `decide_profit_hold`); with no ML it BOOKS at
  +10% (never round-trips a winner). Flat-at-cap is still BOOK-OR-CUT.
- The flat-at-cap branch now only sets `hold_to_resolution` for UPSIDE; the -5%
  stop stays armed.

### 3. Telegram toggle
`bot/settings_store.py`: added `QUICK_FLIP_HARD_STOP_ENABLED` to BOOL_KEYS and to
the "Flip" tab of /settings, and defaulted it True on `Config` at import (a small
shim to AVOID a full `config.py` rewrite -- mirror into `config.py` when
convenient). Telegram label falls back to the key name (telegram_ui.py was NOT
edited -- it is ~1800 lines and only partially loadable here; not worth the
truncation risk for a cosmetic label).

### 4. File-integrity audit (user asked to confirm nothing truncated)
Read end-to-end and confirmed COMPLETE / not truncated on `dev`:
`dashboard.py`, `data/probability_engine.py`, `trading/exit_policies.py`,
`strategies/quick_flip.py`, `bot/settings_store.py`, `config.py`,
`ml/decision_engine.py`, `data/weather_fetcher.py`, `bot/telegram_ui.py`
(blob `dc10e3850c`, the recovered-good version), `trading/position_manager.py`.
Confirmed the ML methods the exit relies on exist with matching signatures
(`decide_profit_hold`, `review_position`). No bad/truncated files found.

---

## MANUAL STEPS FOR THE USER (which line / where / when)

1. **Redeploy `dev` on Railway** to pick up the quick_flip stop-loss fix.
2. **Env vars on Railway** (set once): `WEATHERAPI_API_KEY` and
   `VISUALCROSSING_API_KEY` (enables the 5-provider ensemble + daily max/min).
   Optional: `QUICK_FLIP_HARD_STOP_ENABLED=1` (default already ON).
3. **Rotate exposed secrets** (they were shared in chat): the Railway API token
   and the Alchemy key inside `POLYGON_RPC_URL`.
4. **No manual code line edit is needed any more on `dev`** -- the dashboard
   `bucket_probs = self.engine.estimate_bucket_probabilities(forecasts, buckets,
   target_time, market_type=market.market_type)` wiring is already applied on
   `dev`. (On `main` it is still the old 3-arg call if you ever promote work
   there.)
5. **To turn the quick_flip stop OFF/ON**: Telegram -> /settings -> Flip tab ->
   QUICK_FLIP_HARD_STOP_ENABLED. Tune the level with QUICK_FLIP_STOP_LOSS_PCT
   (default -5) and the target with QUICK_FLIP_TARGET_ROI (default 10).

---

## Prior requests recap (condensed; full detail in the diary at e60166ed)

- Req-24/25/26 (Jun 15-16, on `dev`): peak_cluster dynamic 3-7 legs; thesis exit
  no longer mis-counted; corrected peak_basket; NEW peaker +1/-1 safety basket;
  weather-API failover (429/403 -> cooldown + failover, auto-recover);
  `/analysis` Telegram report + downloadable CSV; quick_flip revival re-applied.
- Req-27/28 (settings + quick_flip v3): full /settings tabs; quick_flip v3 =
  high-confidence mispricing only, 10% target, smaller/fewer, NO-side flips.
- Req-29/30/31/32: starting-balance UX; book-or-cut toggle; profit cap (300% ML
  decision); ML wiring fixed (`<think>` strip, token/timeout budgets, mid-trade
  decide_profit_hold + review_position); split models (gpt-5.4-mini decisions,
  gpt-5.5 /mlanalysis); validate_signal no longer fabricates 0.0C.
- Jun 23: main/dev full sync (PR#10), market_type wiring, and THIS Req-33
  quick_flip stop-loss fix.

## Strategy intent (user's words, for future agents)
Find markets with a HIGH chance of ~10% profit and book it: e.g. late-observed
NO where the bucket is ~80% certain to resolve to 0/1, or an early-market peak
that's mispriced and tends to rise. Enter, book +10%. On reaching +10%, ask the
ML if it can make MORE than 10% -- if yes, let the ML decide when to exit by
raising the target (15/20%+). If it goes against us, HARD-exit at -5% loss
(now enforced for every flip via QUICK_FLIP_HARD_STOP_ENABLED).
