# Session Memory - Request 28 (2026-06-19)

WeatherPol paper-trading bot. Branch: dev. Builds on REQ27 (46f20d60).

## Request 28 (verbatim intent)
Add a proper Telegram lifecycle: on Railway deploy the bot must NOT auto-trade;
it sends "bot initialized successfully" plus an inline keyboard [Start Trading]
[Settings] [Restart]. Trading begins ONLY when the user presses Start or types
`start`. Restart clears ALL positions and starts fresh. Starting balance is
customizable in Settings. Fix peak_cluster opening with only 1 leg (always
loses). Redesign peaker as a SEPARATE strategy: anchor on the market's
high-prob bucket (~60% winning / ~40% upside), cross-validate with our model,
buy on confirmation; peaker cool/warm basket = peak bucket + (-1C / +1C)
neighbour when the combined cost < 95c AND the trend is cooling/warming, bought
and grouped as ONE unit and labeled "peaker cool/warm basket" in Telegram,
status and /analysis. Improve quick_flip: high-confidence mispricing only, 10%
profit target, faster exit, never cut at a loss/breakeven, also use NO-side
buys. Improve all other strategies from the data. DO NOT touch late_observed_no
(it is the good one). Always adjust the models since we keep losing.

## What shipped this request (all pushed to dev)

### 1. Telegram lifecycle (bot/telegram_ui.py, dashboard.py)
- Railway deploy no longer auto-trades: dashboard forces TRADING_ENABLED=False
  after loading settings; the scan loop only trades once enabled.
- send_startup_ready() posts "bot initialized successfully" + main keyboard
  [Start Trading | Settings | Restart].
- Start button / `start` / `/start` / `/resume` enable trading.
- Restart button -> confirm prompt -> _do_restart() -> dashboard.restart_fresh()
  -> pm.reset_fresh(): clears all positions and resets the balance.
- Starting balance is customizable from Settings (settings_store STARTING_BALANCE).

### 2. peaker redesign (strategies/peaker.py)
- Anchors on the market's highest-probability bucket (the venue's implied
  winner) and cross-validates against our model before buying; a bare favourite
  bought solo is roughly breakeven, so the edge comes from the basket.
- peaker cool basket: when our peak == market peak AND the trend is cooling, add
  the -1C neighbour; if peak + (-1C) < 95c, buy BOTH and group as one
  "peaker cool basket".
- peaker warm basket: warming trend -> +1C neighbour, same < 95c rule, grouped
  as "peaker warm basket".

### 3. peak_cluster 1-leg fix (dashboard.py)
- Root cause was per-leg placement in the dashboard. Now placed atomically
  (basket_leg=True) so a cluster never opens as a lone leg.

### 4. quick_flip v3 (strategies/quick_flip.py, trading/exit_policies.py)
- High-confidence mispricing only, 10% profit target, faster exit, never cut at
  a loss or breakeven (profit-only ladder; hold otherwise). NO-side buys wired.

### 5. position_manager.py
- BASKET_STRATEGIES = (peak_cluster, peaker_cool_basket, peaker_warm_basket).
- All basket strategies are always hold-to-resolution.
- Grouped close/resolution notifications cover all basket strategies.
- New reset_fresh(starting_balance=None): clears positions, contexts, counters,
  cluster numbering; resets balance; persists; logs RESTART FRESH.

### 6. config.py / bot/settings_store.py
- TRADING_ENABLED defaults off; STARTING_BALANCE configurable; new peaker knobs
  and quick_flip NO-side knobs added with settings gates.

## Untouched on purpose
- late_observed_no (the good strategy) - left exactly as-is.

## Still open
- Further data-driven tuning of the other strategies (sniper / confident /
  spread / stability).

## Operator actions
- Redeploy dev on Railway to pick up these changes.
- Rotate the Railway API token (it was exposed in chat).
