# Session Memory - Req-29 + Req-30 (backfilled diary)

- **Dates covered:** Req-29 = 2026-06-19; Req-30 + Req-30b = 2026-06-20 (Asia/Calcutta)
- **Branch:** `dev`
- **Why this file exists:** Reqs 18-28 each got a diary entry, but Req-29 and Req-30 shipped as code commits with no companion diary. This backfills them so the record is continuous up to Req-31 (`docs/SESSION_2026-06-20_REQ31_ML.md`). Reconstructed from the dev commit history + session context; commit SHAs are cited so each claim is verifiable.

---

## Req-29 - Telegram UX, balance rebase, quick-flip +10/-5 exit (2026-06-19)

### What was requested
- Cleaner Telegram control surface (manual close, history, sortable positions, exportable trade log, error capture, an ML-style trade report).
- A type-to-set starting-balance flow with an explicit confirm, and a fix for a balance bug where the paper book showed 100 instead of the configured 300.
- Quick-flip should book winners and cut losers on a fixed band instead of round-tripping to break-even.

### What was done
- **Telegram batch** (`eddcf1bf`): `/close` manual-sell; `/done` closed+open history; positions sortable by strategy; `/analysis` CSV export; `/aisummary` runtime-error capture; `/mlanalysis` trade report (heuristic, with an ML-ready hook).
- **Balance UX + trade log** (`ce895475`): type-to-set balance with an OK/Apply summary leading into Start; `/analysis` emitted as a CSV-style trade-log document; sortable status; startup-ready wiring.
- **Balance rebase bug** (`dad9d77c`): added `PositionManager.apply_starting_balance()` - rebases the live paper ledger to the configured `STARTING_BALANCE` when the book is empty (fixes the 300 -> 100 balance bug).
- **Quick-flip exit policy** (`54692b6e`, `03c3be48`): `check_flip_exits` now cuts a flip at `<= QUICK_FLIP_STOP_LOSS_PCT` (-5%) and books at `>= QUICK_FLIP_TARGET_ROI` (+10%, ML-gated run vs book); big runners always booked; window-expiry books at market. No longer converts losers to hold-to-resolution.
- **Mislabel fix** (`96d69e89`): exempted quick-flips from the generic take-profit/stop in `check_risk_triggers`, so flips are booked only by the +10%/-5% policy (fixes a 'TAKE PROFIT' label appearing on a -27% PnL close).
- **Settings registration** (`3251e088`): `STARTING_BALANCE` + `QUICK_FLIP_STOP_LOSS_PCT` made Telegram-configurable.

### Errors / corrections found
- Win-rate / balance accounting: market sells (thesis-exit etc.) had historically not been counted; the exit-policy + label fixes above closed the quick-flip side of that.
- Exit-reason relabeling: the real exit reason is now passed straight into `_close_position` so the trade log records the true reason rather than 'manual'.

---

## Req-30 - ML core + ML-managed exits + Telegram ML wiring (2026-06-20)

### What was requested
- A real ML decision layer with rich context, plus a local fallback profit ladder.
- ML-managed exits: flip +10/-5, a 300% profit cap, and book-or-cut.
- Expose the new ML/exit toggles in Telegram and wire the ML engine into the bot.

### What was done
- **ML core** (`bbde09e1`): rich-context decision engine + local profit ladder + ML-managed exit policies (flip +10/-5, 300% cap, book-or-cut).
- **Config flags** (`0197bedf`): ML profit-cap (300%) + quick-flip book-or-cut / ML-profit toggles.
- **Telegram toggles** (`52bcbbfd`): book-or-cut, ML-profit, and the 300% profit-cap exposed in `/settings`.
- **Wiring** (`2bb605b7`): ML attached to Telegram via `attach_ml`; global profit-cap check now runs each cycle; fixed a funnel-log glyph.
- **Req-30b** (`2a05787f`, "fgg"): small follow-up landed on top of Req-30; verified as the dev HEAD before Req-31.

### Known limitation carried into Req-31
- The ML engine existed and was wired, but the LLM path was effectively dormant: the parser only handled fenced JSON, `max_tokens` was tiny, and the timeout was 8s - which silently fell back to local whenever a reasoning model (`gpt-5.5`) was used. This is exactly what Req-31 fixed (see `docs/SESSION_2026-06-20_REQ31_ML.md`).

---

## Diary index (for continuity)

- `SESSION_MEMORY.md` - rolling log through ~Req-23/24 (+ Req-18/22/23 entries).
- `SESSION_MEMORY_2026-06-15_REQ24-26.md`
- `SESSION_MEMORY_2026-06-17_REQ27.md`
- `SESSION_MEMORY_2026-06-19_REQ28.md`
- `SESSION_MEMORY_2026-06-20_REQ29-30.md` (this file)
- `docs/SESSION_2026-06-20_REQ31_ML.md` - Req-31 ML overhaul.
- `AUDIT_REPORT_2026-06-11.md` - earlier audit.

## Locked invariants (unchanged across these requests)
- Realistic paper engine + settlement: the weather API is CONFIRMATION only; the Polymarket resolved value is the source of truth.
- Ledger invariant in `_close_position` is stats-only for W/L counters; balance math is untouched.

## Deferred
- Req-28 tuning (sniper/confident/spread/stability) remains deferred; do NOT touch `late_observed_no`.
