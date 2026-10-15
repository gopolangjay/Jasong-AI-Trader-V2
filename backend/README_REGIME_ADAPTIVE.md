# V6.14 regime-adaptive execution

The clean runtime explicitly selects the regime strategy, execution and forward
classes. V6.12 and the original engines remain available for legacy evidence and
rollback. The existing V6.13 weekend crypto path is unchanged.

## Trend follower

- Use completed H1 EMA20/50 and Wilder-smoothed ADX14. ADX must exceed 25,
  with EMA ordering and slope agreeing on direction.
- Require an actual crossover in the last 40 H1 bars. Alignment alone is
  insufficient; never enter on the crossover candle itself.
- Take the first post-cross directional candle touching EMA20 within 0.25 ATR,
  closing on the trend side within 0.6 ATR, with >=40% directional body and its
  close in the final 30% of its range.
- With 60 complete H4 bars, opposite H4 EMA ordering blocks the entry.
- Stop beyond the recent swing with an ATR buffer, at least 1.5 ATR. Target 2R
  before spread costs. The setup ID derives from the crossover event, preventing
  repeated entries for the same setup across scans.

## Reversal timer

- H1 ADX <20 identifies a range. Between 20 and 25, exhaustion requires three
  falling ADX intervals and EMA separation below 0.5 ATR. Other transitions wait.
- The M15/H1 entry timeframe must also have ADX below 25.
- Match RSI14 to the most recent two strict price pivots within 48 bars. Confirm
  with two bars on each side; the second pivot must be <=8 bars old and the
  pivots at least three bars apart.
- Bullish: lower price low by >0.02 ATR, RSI >=3 points higher, first RSI <=30,
  second RSI <45. Bearish: higher price high, RSI >=3 points lower, first >=70,
  second >55. RSI is sampled at the exact price-pivot timestamps.
- Require proximity to both existing support/resistance and the relevant
  Bollinger band (20 periods, two standard deviations), within 0.35 ATR.
- The first fresh strong confirming candle must have >=55% directional body and
  close in the final 30% of its range. Reject a close outside the existing range
  beyond a 0.15 ATR tolerance; a breakout cannot trigger a fade.
- Stop beyond the pivot with an ATR buffer, at least 0.5 ATR. Invalidation beyond
  1.5 ATR is too wide. Target the closer band/range mean; never invent a more
  distant target to force a favorable R. Require >=1.2R after the spread buffer.

## Shared controls

- Input is aligned M15 OHLCV with UTC bar-open timestamps. Every constituent bar
  is required to form H1/H4. Duplicate timestamps and invalid prices fail closed.
- Ignore forming/future candles. Base data must be <=990 seconds old and the
  confirmation <=300 seconds old. Check session/news using current time, again
  before execution. News uses the existing configured feed/window policy.
- ATR >3 times the prior 60-bar median blocks entries. Above 1.5 times, buffers
  and ATR floors widen by 25%; position sizing reduces quantity accordingly.
- Observed relative volume <0.5 blocks entries. Missing/zero volume remains
  unknown; tradeability, quotes and broker spread/ATR still must pass.
- Spread must pass its existing category cap and be <=0.15 ATR in relative-price
  units. Recheck it using fresh broker quotes at sizing and submission.
- Transfer relative risk distances across analysis/broker price scales. Add a
  spread buffer; reject target room below 1.2R after costs. Risk-sized quantities
  round downward. Broker minimum-size/distance failures reject the order.
- Send stop and target in the initial order. Track those exact protection levels
  after fill. Missing/invalid protection or cash risk above budget closes the trade.
- New IDs: `REGIME_EMA20_50_PULLBACK_V1`, `REGIME_RSI14_DIVERGENCE_V1`.
  Their broker-forward evidence is separate from retired strategy evidence.
- These entries use the risk-sized category portfolio. The older allocation-sized
  Compound lane is excluded for these IDs because its initial orders do not
  satisfy the attached-stop policy. Existing positions remain managed.
- Legacy ML/FAST fields remain diagnostic. Rule completion is not calibrated
  win probability, a historical win rate or a performance guarantee.

## Optional FDN analysis source

Pinned dependency: `fdnpy==0.6.0`. Set `FDN_API_KEY` in the server environment.
The official SDK documents forex minutes as Premium, crypto minutes as Standard,
and stock minutes as Standard. Provision the appropriate entitlement separately.

Use canonical FX/share symbols and explicit crypto mappings: BTCUSD, ETHUSD,
SOLUSD, XRPUSD, LTCUSD. Unsupported markets retain their original analysis sources.
Gold commodity/index daily data never substitutes for intraday candles.

Initially load seven calendar days, caching historical days independently and
complete M15 frames for five minutes. Defaults: six pages per day, 30 requests per
minute, 300 per UTC day, connection/read timeouts 3/8 seconds. Request/page exhaustion
rejects an incomplete run. Provider failure falls back to the existing sources;
stale fallback bars cannot create fresh regime signals. No key or keyed URL is
included in provider status/errors. Request budgets are per running process.

`/health` and `/market-categories/data-health` expose configuration, SDK availability,
last success/error, cooldown and request counts. Without a key the adapter is
inactive. Tests use fixtures and do not validate a paid provider connection.
