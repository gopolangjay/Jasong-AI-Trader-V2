# V6.12 Adaptive FX Session Momentum

`FX_LIQUIDITY_LINES_V2_ADAPTIVE` covers the same 28 liquid pairs formed by EUR,
GBP, USD, JPY, CHF, CAD, AUD, and NZD. Exotics remain excluded from autonomous
execution. Every entry remains on IG DEMO and `live_money_execution` remains
`false`.

V6.12 replaces the V6.11 all-or-nothing 9/9 entry gate with a mandatory core plus
optional confluence. The purpose is to create more valid forward-test trades
without removing market direction, structure, session, broker, or account-risk
protections.

## Mandatory technical core

A BUY signal requires all of the following on completed candles:

1. H4 bullish directional structure.
2. H1 bullish directional structure.
3. M15 bullish BOS, CHoCH, CISD, or MSS displacement.
4. A pair-relevant weekday trading session is active.

A SELL signal requires the exact inverse.

The M15 structure detector no longer requires a liquidity sweep first. A
sweep-anchored break is still preferred when present, but a genuine recent M15
structure break can qualify independently.

## Optional confluence: minimum 2 of 5

The following five confirmations are scored but are no longer individually
mandatory:

1. H4 external trendline aligned and intact.
2. Premium/discount location.
3. Liquidity sweep/reclaim.
4. Order-block, fair-value-gap, or break-zone retest.
5. Closed-candlestick confirmation.

Candlesticks are still analyzed. Supported patterns include engulfing,
hammer/shooting-star rejection, piercing/dark-cloud, morning/evening star,
marubozu/displacement, and inside-bar breakout.

## Confidence and trade grades

- Market-structure confluence minimum: 60%.
- Quant confidence minimum: 20%.
- Directional/model-AI confidence minimum: 30%.
- Overall setup confidence minimum: 30%.
- B-grade DEMO trade: at least 2/5 optional confirmations, at least 30% setup
  confidence, and at least 0.3R available target room.
- A-grade DEMO trade: at least 3/5 optional confirmations, at least 30% setup
  confidence, and at least 0.5R available target room.
- Preferred target remains 2R or better when market structure/liquidity allows.

FAST remains available as a diagnostic/ranking measure for V6.12 FX, but it is
not an extra technical-signal veto above the agreed 20%/30%/30% minimums.

## News and IG execution safety

News clearance and IG dealing checks no longer decide whether the technical
setup exists. They remain hard pre-execution protections. A valid setup can be
recorded for forward learning while an actual DEMO order is blocked when:

- a configured high-impact news blackout is active;
- the IG market is not tradeable;
- a fresh bid/offer is unavailable;
- spread exceeds the configured FX limit;
- minimum deal size/increment cannot be used within the risk budget;
- duplicate market/setup or exposure limits block another position; or
- account/risk evidence is incomplete.

This distinction keeps the requested entry rules relaxed without allowing the
engine to submit invalid or unsafe broker orders.

## Structural risk and targets

V6.12 can derive a structural stop from a sweep/retest invalidation when
available, or from a recent confirmed swing when the optional sweep/retest is
absent. The minimum eligible target room is 0.3R; A grade requires at least 0.5R;
the preferred target remains 2R. Position sizing still uses the configured
account-risk percentage and never rounds an unsafe broker size upward.

Gold is not changed by this V6.12 FX policy and retains its existing XAUUSD
liquidity/structure risk rules.

## Timeframes and sessions

- H4: primary directional bias, premium/discount, external line.
- H1: mandatory directional confirmation.
- M15: structure break, optional sweep/retest/candle, entry timing.
- Forming M15 candles and incomplete H1/H4 aggregates are ignored.
- London: 08:00-17:00 `Europe/London` for EUR/GBP/CHF.
- New York: 08:00-17:00 `America/New_York` for USD/CAD.
- Tokyo: 09:00-18:00 `Asia/Tokyo` for JPY.
- Sydney: 08:00-17:00 `Australia/Sydney` for AUD/NZD.

IANA timezones remain DST-aware and South African timestamps remain available in
runtime diagnostics.

## Forward measures

V6.12 is intended to be judged on genuine IG DEMO forward evidence. Track at
minimum:

- win rate;
- expectancy in R;
- profit factor;
- maximum drawdown;
- realized R-return;
- performance by A/B grade;
- performance by setup-confidence bucket; and
- rejection/blocker frequencies, including news and broker execution guards.

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
