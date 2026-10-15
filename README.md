# Jasong AI Trader V6.14

IG DEMO trading service with a FastAPI backend and Flutter Android client.
V6.14 routes the 28 liquid FX pairs and Gold between trend-following and
range-reversal strategies. The V6.13 weekend crypto engine remains independent.

## Current strategies

| Condition | Entry | Timeframe | Protection |
| --- | --- | --- | --- |
| Strong trend: H1 ADX >25 | EMA20/50 cross, then first confirmed EMA20 pullback | H1; H4 conflict veto when enough history exists | Swing/ATR stop; target 2R before spread costs |
| Range or qualifying exhaustion | Confirmed price-pivot RSI14 divergence at Bollinger/support-resistance edge, then strong close | M15 or H1 | Pivot/ATR stop; target existing range mean with >=1.2R after costs |
| Transition, stale/incomplete confirmation or panic volatility | Wait | — | No new entry |

The range threshold is H1 ADX <20. Qualifying exhaustion below 25 requires falling
ADX and small EMA separation. Rule completion measures checks, not win probability.
See the [detailed policy](backend/README_REGIME_ADAPTIVE.md).

## Execution

- IG DEMO only; pair-relevant DST-aware sessions and configured news guards apply.
- Volatility widens ATR/structure stops and reduces size within the same cash budget.
- Both category spread caps and spread/ATR liquidity checks apply. Missing volume
  stays explicitly unknown; observed weak volume blocks entries.
- New orders submit a broker-side stop and target together. Unsafe minimum sizes,
  dealing distances, stale/invalid quotes and adverse fill risk reject or close
  an entry rather than widening its risk budget.
- Duplicate setups, exposure, portfolio capacity and daily limits remain enforced.
  Existing positions and earlier strategy evidence continue to be managed.
- New regime entries use the account-risk-sized category portfolio. They are
  excluded from the older allocation-sized Compound order lane, which lacks
  these attached-stop guarantees. Forward evidence still measures PRIME status.

Earlier [FX](backend/README_FOREX_ACTIVE.md) and
[Gold](backend/README_XAUUSD_ACTIVE.md) guides document retained legacy strategies.

## FinancialData.Net / fdnpy

Set `FDN_API_KEY` on the server to enable the optional
[official fdnpy SDK](https://github.com/financialdatanet/fdnpy) analysis adapter.
IG remains the broker and execution-quote source. Forex minute data requires the
provider's Premium subscription. No credential is committed.

The adapter supports FX, crypto and share minute endpoints; validates UTC OHLCV;
and retains only complete M15 bars. Commodity/index daily prices never substitute
for intraday candles. Unconfigured, unsupported, quota-limited or failed requests
fall back to existing analysis sources. Timeouts, page/request budgets, caching
and redacted errors bound SDK behavior. See [configuration](backend/.env.example).

## Run and validate

```bash
cd backend
python -m venv .venv
# Activate the environment for your operating system.
pip install -r requirements.txt
python run_server.py
```

From the repository root:

```bash
python -m unittest discover -s tests -p 'test*.py' -v
python backend/clean_core_smoke.py
# Optional broader discovery (includes the function-style weekend tests):
pip install 'pytest>=8,<10'
python -m pytest -q tests -o 'python_files=test*.py'
```

`/health`, `/strategy/regime-policy`, `/market-categories/status`,
`/category-portfolio/status` and `/market-categories/data-health` expose the
active policy, strategy IDs, protections and provider configuration.

## Android build

```bash
cd mobile
flutter create . --platforms=android
flutter pub get
flutter analyze
flutter build apk --release
```

APK output: `mobile/build/app/outputs/flutter-apk/app-release.apk`.
The V6.14 client shows regime, timeframe, ADX, volatility, rule status and blockers.
Code tests do not establish profitability; evaluate returns using genuine
broker-settled DEMO results.
