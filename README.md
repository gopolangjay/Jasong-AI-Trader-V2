# Jasong AI Trader V6.12

Jasong is an autonomous IG DEMO trading service with a FastAPI backend and a
Flutter mobile client. V6.12 keeps the existing XAUUSD liquidity/market-structure
setup and upgrades the 28-pair liquid forex path to Adaptive FX Session Momentum.

## Active execution policy

- New autonomous entries: `GOLD` plus all 28 combinations of EUR, GBP, USD,
  JPY, CHF, CAD, AUD, and NZD. Exotics remain analysis-only.
- Broker environment: IG DEMO only; live-money execution is disabled.
- FX mandatory core: aligned H4/H1 directional bias, an M15 BOS/CHoCH/CISD/MSS
  displacement, and an active pair-relevant session.
- FX optional confluence: minimum 2 of 5 from H4 trendline, premium/discount,
  liquidity sweep, OB/FVG/break-zone retest, and closed-candlestick confirmation.
- FX minimums: 60% market-structure confluence, 20% Quant, 30% directional AI,
  and 30% overall setup confidence.
- FX grades: A requires at least 3/5 optional confirmations and at least 0.5R;
  B requires at least 2/5 and at least 0.3R. The preferred target remains 2R+.
- Forex session windows follow pair geography: London for EUR/GBP/CHF, New York
  for USD/CAD, Tokyo for JPY, and Sydney for AUD/NZD. IANA timezones handle DST.
- News clearance and IG tradeability/quote/spread/size checks do not decide
  whether the technical FX signal exists, but remain hard DEMO execution guards.
- Position sizing remains account-risk based, never rounds an unsafe broker size
  upward, blocks duplicate exposure, and preserves daily/per-pair entry limits.
- Gold remains on its existing XAUUSD liquidity/structure policy, including its
  existing structural target requirements; the relaxed V6.12 thresholds are FX-only.
- The former EMA/ADX/range and non-FX/non-Gold entry strategies are retired.
  Catalogue entries remain visible, and existing positions remain managed.

See [the forex strategy specification](backend/README_FOREX_ACTIVE.md) and
[the Gold strategy specification](backend/README_XAUUSD_ACTIVE.md).

## Backend

```bash
cd backend
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python run_server.py
```

Run validation from the repository root:

```bash
python -m unittest discover -s tests -p 'test*.py' -v
python backend/clean_core_smoke.py
```

## Forward performance measures

Judge V6.12 using genuine IG DEMO forward results: win rate, expectancy, profit
factor, maximum drawdown, realized R-return, A/B-grade performance, confidence
buckets, and execution-blocker frequencies.

## Android build

Install Flutter and Android Studio, then:

```bash
cd mobile
flutter create . --platforms=android
flutter pub get
flutter build apk --release
```

The APK is written to `mobile/build/app/outputs/flutter-apk/app-release.apk`.

## Risk statement

No trading strategy guarantees returns. The active execution path remains on
IG DEMO. V6.12 deliberately separates technical-signal eligibility from broker
execution safety: an eligible setup may be recorded for forward learning while
an actual order is still blocked by news, market status, quote/spread, position
size, duplicate exposure, or account-risk controls.
