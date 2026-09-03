# V6.11 active forex liquidity/lines strategy

`FX_LIQUIDITY_LINES_V1` covers the 28 liquid pairs formed by EUR, GBP, USD,
JPY, CHF, CAD, AUD, and NZD. It deliberately excludes exotics by default because
their wider spreads and less predictable liquidity conflict with the supplied
course material. Every entry remains on IG DEMO; `live_money_execution` is
always `false`.

## Buy setup

Every mandatory check must pass on completed candles:

1. H4 has bullish HH/HL structure.
2. An external H4 trendline through confirmed higher lows is rising and intact.
3. Price is in H4 discount (with a 0.25 M15 ATR zone tolerance).
4. Price sweeps and reclaims sell-side liquidity: rolling/equal/old lows or a
   previous day, week, or month low.
5. M15 closes through structure with BOS, CHoCH, CISD, or MSS displacement.
6. Price retests an order block, fair-value gap, or break zone.
7. The latest completed M15 candle confirms the buy. Supported confirmations
   include bullish engulfing, hammer/rejection, piercing line, morning star,
   marubozu/displacement, and inside-bar breakout.
8. The stop beyond the sweep/zone plus a 0.15 ATR buffer leaves at least 2R to
   opposing liquidity.
9. A weekday session relevant to either currency is open and no configured
   high-impact news blackout is active.

The sell setup is the exact inverse: bearish H4 LH/LL structure, a falling and
intact external line, premium location, buy-side sweep/reclaim, downward
structure shift, bearish retest candle, and at least 2R to sell-side liquidity.

An internal M15 trendline is calculated separately. A touch adds confidence for
entry timing, but it is not allowed to override a missing mandatory structure,
liquidity, candle, session, news, or risk check.

## Timeframes and sessions

- H4: primary bias, external structure, premium/discount, external line.
- H1: confirms structure context and classifies BOS versus reversal shift.
- M15: sweep, structure break, OB/FVG retest, internal line, and candle entry.
- The still-forming M15 candle and incomplete H1/H4 aggregates are ignored.
- London: 08:00-17:00 `Europe/London` for EUR/GBP/CHF.
- New York: 08:00-17:00 `America/New_York` for USD/CAD.
- Tokyo: 09:00-18:00 `Asia/Tokyo` for JPY.
- Sydney: 08:00-17:00 `Australia/Sydney` for AUD/NZD.

The active sessions and their timestamps are recorded in UTC and South African
time. Overlap is a confidence bonus, not a substitute for the complete setup.

## News and risk controls

Optional high-impact blackout windows are supplied through
`JASONG_HIGH_IMPACT_NEWS_WINDOWS_JSON`. A malformed configured value fails
closed. Set `FOREX_NEWS_GUARD_REQUIRED=true` to fail closed when no calendar is
configured.

```text
CATEGORY_RISK_PER_TRADE_PCT=1.0       # capped to 0.10-1.00
FOREX_MAX_DAILY_ENTRIES=4             # capped to 1-12
FOREX_MAX_DAILY_ENTRIES_PER_PAIR=1    # capped to 1-3
JASONG_ACTIVE_EXECUTION_MARKETS=GOLD,FOREX_ALL
CATEGORY_AUTOTRADE=true
```

The execution engine uses a structural stop and minimum 2R target, sizes down to
the IG increment, refuses an unsafe minimum size, blocks duplicate setup IDs,
blocks duplicate account-wide EPIC exposure, and enforces currency exposure
tags. Missing tradeability, quote, spread, session, or setup evidence means no
entry.

## Validation

```bash
python -m unittest discover -s tests -p 'test*.py' -v
python backend/clean_core_smoke.py
```

Useful diagnostics:

```text
GET /health
GET /market-categories/status
GET /market-categories/FOREX
GET /category-portfolio/status
GET /trade-excursions/status
```
