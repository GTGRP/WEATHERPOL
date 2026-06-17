# SESSION_MEMORY companion — June 17, 2026 (Notion AI / GTGRP) — REQUEST 27

> Dated companion to SESSION_MEMORY.md (the main diary is ~75KB+ and can no longer be rewritten
> whole via the GitHub API without risking truncation of earlier history, which its header forbids).
> APPEND-ONLY, last entry at the bottom, never delete or prepend — same rule as the main file.
> Continues the June 15-16 Req-24/25/26 entry (companion SESSION_MEMORY_2026-06-15_REQ24-26.md).

Same agent (Notion AI) / same remote `https://github.com/GTGRP/WEATHERPOL`, via GitHub MCP
(sandbox offline + non-persistent; pure modules `py_compile`-clean; dashboard/config/telegram reasoned).
**ALL Req-27 work is on the `dev` branch** (not `main`), so the user reviews/redeploys `dev` before merging.
SETTLEMENT DESIGN STILL LOCKED (paper closes as real ~99%; weather-API = CONFIRMATION only;
Polymarket resolved value = truth). Tone: normal technical, NO ELI10. NOT touched this request.

## CONTEXT — the live `/analysis` the user pasted (Jun 17 13:10) that motivated Req-27
PAPER Bal $19.82 | PnL -$14.24 (-14.2%) | WR 29% (40W/100L) | Trades 192 | Open 25 | Redeemed $83.63.
- Winners: `safety_peak_neighbor_cool +$16.96`, `late_observed_no +$9.33`.
- Losers: `late_observed_yes -$14.20`, `peak_cluster -$9.02`, `quick_flip -$8.83`, `safety_peak_peak -$7.89`.
Key read: the COOL neighbour of safety_peak was the single best performer; the peak/peak leg and the YES
leg bled; quick_flip booked too many breakeven/loss exits; peak_cluster was only buying ONE leg.

## REQUEST 27 (verbatim intent, Jun 17 13:43; "yes do all" + "yes proceed do all as i said" 14:33)
1. Add strategy ENABLE/DISABLE toggles in the Telegram bot, and expose ALL missing config vars in Telegram.
2. Redesign the `/settings` message UI — the +/- was hard to see / not good.
3. quick_flip: buy early when the market opens or is mispriced; ALWAYS sell in profit based on its edge/prob;
   let the XGBoost ML decide whether to sell quickly for a small profit or hold for 10/20/30% more.
   KILL the book-or-cut exits that were closing flips at breakeven / loss.
4. peak_cluster: must buy 3-7 legs (the min-3-legs env was set but it still bought only ONE leg — bug).
5. observed_no is good -> give it a small edge boost AND more capital.
6. observed_yes -> fix it so it can also win based on all the data.
7. MERGE the peak + safety_peak strategies into ONE strategy named `peaker` (they were buying the same
   position twice as a duplicate). 4 modes, all combined cost < 95 cents: (1) peaker 1 leg stable;
   (2) basket warmer = peak + (+1); (3) basket cooler = peak + (-1); (4) both neighbours -> cluster.
8. Focus on / calibrate around the WINNING `safety_peak_neighbor_cool`; the peak/peak leg busts while the
   cool neighbour wins -> refactor peaker to lean on the cool find so we win.

## WHAT I CHANGED + WHY (all on `dev`)

### Phase 1+2 — peaker merge + peak_cluster fix + dashboard wiring (ONE commit `dda88050`)
- **NEW `strategies/peaker.py` (blob `901451e6`)** — merges the old `safety_peak.py` + `peak_basket.py` into ONE
  strategy with 4 modes, each combined cost kept under `PEAKER_MAX_COST` (0.95):
  - mode `solo` (1 leg) when confidence is very high (>= `PEAKER_SOLO_MIN_CONFIDENCE`);
  - mode `warm` = peak + (+1) bucket when the trend warms (`PEAKER_WARM_SIZE_MULT`);
  - mode `cool` = peak + (-1) bucket when the trend cools (`PEAKER_COOL_SIZE_MULT`, `PEAKER_COOL_EDGE_RELAX`);
  - mode `cluster` = both neighbours when the spread is wide.
  CALIBRATED around the winning cool neighbour: `PEAKER_PREFER_COOL` biases toward the cool leg, the cool leg
  gets a SIZE multiplier (more capital) and an edge RELAX (easier to fire), and `PEAKER_PEAK_BIAS_BUCKETS`
  nudges the chosen peak DOWN so we lean to the cool side that actually wins. All legs hold_to_resolution.
  No duplicate buys (single entry point replaces the two old strategies that double-bought).
- **FIX `strategies/peak_cluster.py` (blob `02ba0b56`, 209L)** — the 1-leg bug: it was selecting legs but the
  greedy add stopped after the first because of a cost-accumulation/short-circuit error. Now it floors to
  `PEAK_CLUSTER_MIN_LEGS` (default 3) and adds up to MAX_LEGS while combined cost < MAX_COST.
- **`dashboard.py` (blob `1c0a6922`, 1235L)** — wired peaker into the scan loop (replaces the safety_peak +
  peak_basket blocks), passes real reason/grade/lock_confidence/signal, keeps peak_cluster Box grouping. The old
  SAFETY_PEAK_* / PEAK_BASKET_* env keys remain in config as harmless dead config; peaker runs by default via
  getattr defaults, so `SAFETY_PEAK_ENABLED` / `PEAK_BASKET_ENABLED` env (still on in Railway) are NO-OPS now.

### Phase 3 — quick_flip profit-only ladder + ML sell decision (`exit_policies.py` + `config.py`)
- **`trading/exit_policies.py` (commit `389f046e`, blob `a6793a3d`, 8771B)** — rewrote `check_flip_exits(pm)` as a
  PROFIT-ONLY ladder (no more loss/breakeven book-or-cut): book a flip only when ROI clears
  `QUICK_FLIP_MIN_BOOK_ROI_PCT`; a lazy `_get_ml()` + `_ml_says_book(pos)` lets the XGBoost model decide between
  booking a small profit (mid ladder `QUICK_FLIP_LADDER_MID_ROI_PCT`) vs holding for more
  (`QUICK_FLIP_LADDER_RUN_ROI_PCT`), with a `QUICK_FLIP_FORCE_BOOK_ROI_PCT` hard take. `check_thesis_exits` now
  passes a real exit_reason into `_close_position` (fixes the silent/mislabeled exit-reason logging).
- **`config.py` (commit `1a31b416`, blob `c401005290`, 52790B, 708L)** — added the quick_flip profit-ladder knobs,
  the full PEAKER_* knob set, `QUICK_FLIP_PROFIT_ONLY_EXIT` / `QUICK_FLIP_USE_ML_EXIT` toggles, and the peaker
  toggles. Kept SAFETY_PEAK_* / PEAK_BASKET_* as dead config. `Config.is_paper()` present.

### Phase 4 — observed YES/NO calibration (`strategies/late_observed_temp.py`, commit `325d99f1`, blob `e4ee08ef`, 318L)
- observed_NO: small edge boost + more capital via `STRATEGY_SIZE_MULT[late_observed_no]=1.3` (config) and a
  relaxed NO entry band.
- observed_YES: re-gated with 6 edits (its own `LATE_OBSERVED_YES_MIN_LOCK` / `LATE_OBSERVED_YES_MIN_EDGE`,
  separate from the NO gates) so it only fires when the full data actually supports a YES win, and sized DOWN
  (`STRATEGY_SIZE_MULT[late_observed_yes]=0.6`).

### Phase 5 — Telegram: tabbed `/settings` + every config gate exposed + strategy toggles
- **NEW `settings_store.py` (commit `cab1fedf`, blob `c34294cf`, 294L)** — runtime settings layer over
  `data/runtime_settings.json`: `group_keys(group_id)`, `_coerce`, `set_value`, `toggle`, `bump(key,direction)`
  (int round / float 4dp, clamps to min/max/step), `snapshot()`, `_persist()`, `load_into_config()` (applied at
  boot). 24 BOOL_KEYS (strategy enable/disable + master TRADING_ENABLED + exit/guard toggles), 73 NUM_KEYS
  `key:(min,max,step,is_int)`, 8 GROUPS (tabs): main/risk/lateobs/quickflip/peaker/cluster/exits/sniper.
- **`bot/telegram_ui.py` (commit `60e233d4`, sandbox blob `0fb5ff63`, 953L)** — redesigned `/settings` as a
  TABBED panel: header shows PAPER/LIVE + master trading ON/OFF; toggle rows (checkmark/cross) for strategies;
  numeric gate rows rendered `[- step][KEY = value][+ step]` (the clearer +/- the user asked for); tab buttons
  switch groups (`st:<gid>`); `/settings <tab>` opens a specific tab; `/help` updated. New helpers `_LABELS`
  (24 entries), `_fmt_num`, group-aware `_settings_view(group)` / `send_settings(group, edit_message_id)`, and
  `_handle_callback` handles `st:` tab-switch + `tg:`/`up:`/`dn:` with the group carried through.

## CAVEAT for the user / next agent (told to the user)
- Enable/disable toggles + master trading apply IMMEDIATELY (live-read).
- Numeric gates that strategies cache in `__init__` persist to `data/runtime_settings.json` and take full effect
  on the NEXT restart (`load_into_config()` runs at boot). User redeploys anyway, so they end up in sync.
- Railway env still has SAFETY_PEAK_* / PEAK_BASKET_* enabled -> now NO-OPS (peaker runs by default). Harmless.

## WHEN / WHY pushed (GTGRP/WEATHERPOL `dev`)
June 17, 2026 (IST). Commit order: `dda88050` (peaker + peak_cluster + dashboard, Phase 1+2) ->
`389f046e` (exit_policies, Phase 3) -> `1a31b416` (config, Phase 3) -> `325d99f1` (late_observed_temp, Phase 4)
-> `cab1fedf` (settings_store, Phase 5) -> `60e233d4` (telegram_ui, Phase 5, verified on fetch-back).
`dev` HEAD after Req-27 = `60e233d4`. Each file was `py_compile`/AST-clean before push; telegram_ui.py was
fetched back from `dev` and confirmed to carry all 4 edits with no corruption.

## ERRORS & LESSONS (recorded honestly for future AI)
- **telegram_ui.py divider mangle (root cause found):** `computer.readFile` mangles box-drawing RUNS (U+2550 `=`)
  to the replacement char in its DISPLAY, but it ALSO faithfully shows REAL on-disk `\ufffd` bytes. One line had
  3 ACTUAL `\ufffd` bytes baked in (inherited from an earlier mangled readback that got copied into the
  hand-staged canonical). LESSON: a `\ufffd` in a readback can be a REAL on-disk byte -- diagnose codepoints with
  a Python `ord(c)>127` scan before trusting/pushing; `grep -c` for U+FFFD can give a false 0. Normalized all
  box-drawing dividers to ASCII `=` and scrubbed the stray `\ufffd` before the final push.
- **`get_file_contents` truncates very large files** (SESSION_MEMORY.md ~75KB+ comes back cut off mid-file), so
  the main diary can NOT be safely rewritten whole -- hence this dated companion. Same reason as the June 15 split.
- **push_files / create_or_update_file need FULL file content (no patch); ONE file per logical commit from REAL
  source; verify after push; sandbox `/data/wp/` is NOT persistent so re-stage each turn.**

## STILL PENDING (user / next agent)
- **User -> Railway:** REDEPLOY `dev` to pick up all Req-27 commits. Then in Telegram exercise `/settings`
  (tabs, toggles, +/- on numeric gates), confirm strategy enable/disable works, and send a fresh `/analysis`
  after a run so we can confirm: peaker leans cool and wins; peak_cluster buys 3-7 legs; quick_flip only books
  in profit (ML picks small-vs-hold); observed_no boosted, observed_yes no longer bleeding.
- **Optional cleanup:** delete orphaned `strategies/safety_peak.py` (blob `3ef06ed5`, 346L) +
  `strategies/peak_basket.py` (blob `f77ef5f3`, 370L) once confirmed peaker covers them (need current SHAs for
  delete_file). Config keeps SAFETY_PEAK_* / PEAK_BASKET_* as harmless dead config.
- **Security:** the Railway API token was exposed earlier in this thread -- rotate it.
- **Deferred (unchanged):** quiet `Redeemed:` log spam; testable `decide_placement()`; CI (pytest+ruff); cap
  `data/paper_trades.jsonl`; weather.gov stray `{`; 3 `no_coords` cities; backtest leg-sizing; failover for
  `data/stability.py`; cluster dedup.
