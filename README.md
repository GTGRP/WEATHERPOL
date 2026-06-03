# Weather Sniper Bot

Polymarket weather market trading bot that exploits slow-moving temperature markets using multi-source forecasts.

## Strategy

**Sniper:** Buy cheap temperature bucket outcomes ($0.007-$0.15) that our ensemble forecast strongly favors. Hold to resolution for 9x-142x returns.

**Spread:** Buy multiple adjacent temperature buckets with decaying allocation. Profit even if actual temp is ±1°C from forecast.

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Copy env file
cp .env.example .env

# 3. Run in paper mode (default, safe)
python dashboard.py

# 4. Run single scan
python dashboard.py --once

# 5. Run live (real money)
python dashboard.py --live
```

## Architecture

```
dashboard.py           Main loop (scan → forecast → analyze → trade)
├── data/
│   ├── weather_fetcher.py   Multi-source forecasts (Open-Meteo, OWM, weather.gov)
│   ├── probability_engine.py Ensemble → bucket probability distribution
│   ├── market_scanner.py     Find weather markets on Polymarket
│   └── clob_client.py        Order placement (CLOB V2)
├── strategies/
│   ├── sniper_strategy.py    Buy cheap mispriced buckets
│   └── spread_strategy.py    Multi-outcome spread bets
├── trading/
│   └── executor.py           Paper + live execution, position tracking
├── config.py                  All configuration
└── logger.py                  Structured logging
```

## Weather Models Used

| Source | Models | Key Required |
|--------|--------|-------------|
| Open-Meteo | ECMWF, GFS, ICON, JMA, GEM | No (free) |
| OpenWeatherMap | OWM proprietary | Yes (free tier) |
| weather.gov | NWS (US only) | No |

## Paper Mode

Default mode. Simulates all trades, tracks P&L, generates signals without spending real money. Perfect for testing and parameter tuning.

```bash
python dashboard.py --paper --balance 10.0
```

## Deploy

### Railway
```bash
railway up
```

### Local
```bash
python dashboard.py
```

## Configuration

All settings in `.env` — see `.env.example` for full list.

Key parameters:
- `TRADING_MODE`: paper or live
- `SNIPER_MAX_ENTRY_PRICE`: max price to buy (default $0.15)
- `MIN_EDGE_TO_ENTER`: minimum edge required (default 10%)
- `KELLY_FRACTION`: Kelly sizing conservatism (default 0.15)
- `SCAN_INTERVAL_SECONDS`: how often to scan (default 60s)
