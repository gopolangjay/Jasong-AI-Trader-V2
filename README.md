# Jasong AI Trader V6.15

IG DEMO trading service with a FastAPI backend and Flutter Android client.
OpenAI now selects BUY, SELL or WAIT from validated completed M15/H1/H4 price
history. The active AI universe is the 28 liquid FX pairs, Gold and Bitcoin.

## Entry analysis and execution

- The OpenAI Responses API selects up to three proposals per analysis, with an
  observed invalidation stop, target and holding horizon. An empty list means WAIT.
- The prior EMA/ADX/RSI entry sequence, ML/FAST confidence floors, geographic
  session veto and fixed minimum reward/risk threshold do not gate AI entries.
- Every entry remains IG DEMO-only and requires fresh valid data/quotes, correct
  instrument identity, IG tradeability, category spread-cost limits and any
  explicitly configured high-impact-news guard.
- Account-risk sizing remains capped at 1% per trade. Existing daily, duplicate,
  capacity, account-wide EPIC and theme-exposure controls remain enforced.
- Initial orders include a broker stop and target. Invalid protection, excess
  filled risk or upward size changes close the order. Existing position management
  and broker-settled evidence continue across the upgrade.
- Bitcoin may be selected on weekdays when IG is tradeable. The old independent
  weekend entry worker is disabled in AI mode, preventing competing entry lanes.
- One bounded analysis batch per M15 period is shared by all market evaluations.
  Default: 96 paid requests per UTC day, 45-second timeout, no automatic retries,
  at most 4,000 output tokens, and no account identifiers or credentials in inputs.
  API failures, refusals, malformed decisions and missing credentials produce WAIT.

See the [OpenAI analyst guide](backend/README_OPENAI_ANALYST.md). The
[regime strategy](backend/README_REGIME_ADAPTIVE.md), earlier
[FX](backend/README_FOREX_ACTIVE.md) and [Gold](backend/README_XAUUSD_ACTIVE.md)
implementations remain available for rollback and existing-position evidence.

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
pip install -r backend/requirements-dev.txt
python -m pytest -q
python -m compileall -q backend
(cd backend && python clean_core_smoke.py)
```

`/health`, `/strategy/openai-analysis`, `/strategy/regime-policy`,
`/market-categories/status`, `/category-portfolio/status` and
`/market-categories/data-health` expose active policy, decisions, protections and
provider configuration. Reading status never starts a paid analysis or an order.

## Android build

```bash
cd mobile
flutter create . --platforms=android
flutter pub get
flutter analyze
flutter build apk --release
```

APK output: `mobile/build/app/outputs/flutter-apk/app-release.apk`.
The existing client consumes live category rankings and protected portfolio state.
AI rationale is also exposed in compact Actions/MCP market responses.
Code tests do not establish profitability; evaluate returns using genuine
broker-settled DEMO results.
