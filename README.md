# Jasong AI Trader V6.16

IG DEMO trading service with a FastAPI backend and Flutter Android client.
OpenAI now selects BUY, SELL or WAIT from validated completed M15/H1/H4 price
history. The active AI universe is the 28 liquid FX pairs, Gold and Bitcoin.

## Entry analysis and execution

- OpenAI selects up to three BUY/SELL entries per analysis, or WAIT, and explicit
  closes for positions it opened. It receives completed M15/H1/H4 candles and
  aliased open-position evidence; it has no broker tools or credential access.
- The default `JASONG_DEMO_EXECUTION_POLICY=DISCRETIONARY` implements the user's
  requested DEMO policy: no application account-percent sizing cap, daily entry
  cap, position/theme/EPIC capacity cap, geographic/news veto or quote/spread/
  attached-protection/dealing-distance preflight. Orders use a positive model
  contract size, or the existing configured IG default when size is null.
- Market orders omit attached stop and limit levels. Advisory model levels and
  holding horizons do not force exits. The model explicitly chooses closes;
  the fixed server risk/profit watchdog and native-order amendment are disabled.
- IG still applies its own market, margin, minimum-size and order rules. Rejections
  are recorded. The code cannot remove rules enforced by IG.
- Hard-coded IG DEMO, authenticated writes, exact instruments, valid completed
  candles, fresh context-bound model decisions and owned-position closes remain.
  Each decision is submitted once, durably, including across restarts. An uncertain
  fill is recovered from the broker by its reserved reference rather than resent.
- The old independent weekend and Compound entry lanes remain disabled in AI mode.
- One bounded analysis per M15 period is shared by all scanners. The existing
  OpenAI key, 96 requests per UTC day, 45-second timeout, no automatic API retries,
  4,000 output-token bound and credential redaction are retained.
- Set `JASONG_DEMO_EXECUTION_POLICY=PROTECTED` for the earlier capped/attached-order
  policy, or `JASONG_ANALYSIS_MODE=REGIME` for the previous rule-based strategy.

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
The existing client consumes live category rankings and portfolio state.
AI rationale is also exposed in compact Actions/MCP market responses.
Code tests do not establish profitability; evaluate returns using genuine
broker-settled DEMO results.
