# Jasong AI Trader V6.10

Jasong is an autonomous IG DEMO trading service with a FastAPI backend and a
Flutter mobile client. V6.10 executes one active strategy: a completed-candle,
multi-timeframe XAUUSD liquidity/market-structure setup during DST-aware London
and New York sessions.

## Active execution policy

- New autonomous entries: `GOLD` / XAUUSD only.
- Broker environment: IG DEMO only; live-money execution is disabled.
- Signal path: H4 structure and premium/discount, M15 liquidity sweep, BOS/CHoCH,
  order-block/FVG retest, closed-candle confirmation, and at least 2R room.
- Weekday session window: London or New York 08:00-17:00 local time, evaluated
  with IANA timezones and recorded in South African time.
- Risk: at most 1% of account balance per entry, structural 1R stop, minimum 2R
  target, no broker size round-up, one account-wide Gold position, and at most
  two entries per South African calendar day.
- The former 40-market entry strategies are retired. Their catalogue entries
  remain visible, and previously opened positions remain managed until exit.

See [the active strategy specification](backend/README_XAUUSD_ACTIVE.md) for the
exact entry, session, sizing, stop, target, and duplicate rules.

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
