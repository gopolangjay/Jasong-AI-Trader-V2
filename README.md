# Jasong AI Trader V6.11

Jasong is an autonomous IG DEMO trading service with a FastAPI backend and a
Flutter mobile client. V6.11 executes two completed-candle strategy families:
the existing XAUUSD liquidity/market-structure setup and a new 28-pair liquid
forex liquidity/trendline setup with geography-aware sessions.

## Active execution policy

- New autonomous entries: `GOLD` plus all 28 combinations of EUR, GBP, USD,
  JPY, CHF, CAD, AUD, and NZD. Exotics remain analysis-only.
- Broker environment: IG DEMO only; live-money execution is disabled.
- Signal path: H4 structure/external trendline and premium/discount, M15
  liquidity sweep, BOS/CHoCH/CISD/MSS, order-block/FVG retest, closed-candlestick
  confirmation, and at least 2R room. The internal line adds entry confluence.
- Forex session windows follow pair geography: London for EUR/GBP/CHF, New York
  for USD/CAD, Tokyo for JPY, and Sydney for AUD/NZD. IANA timezones handle DST.
- Risk: at most 1% of account balance per entry, structural 1R stop, minimum 2R
  target, no broker size round-up, one account-wide position per market, a
  default four FX entries per SAST day and one entry per pair per SAST day.
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
IG DEMO and deliberately skips a trade whenever session, structure, quote,
position-size, or account-risk evidence is incomplete.
