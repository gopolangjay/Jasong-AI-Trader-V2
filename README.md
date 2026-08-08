# Jasong AI Trader V3

A complete Android-ready AI-assisted paper-trading application.

## Included

- Flutter Android mobile app
- FastAPI backend
- Explainable rule + machine-learning signal engine
- EMA / RSI / MACD / Bollinger / ATR / volatility features
- BUY / SELL / WAIT decisions
- Conservative / Balanced / Aggressive risk profiles
- No Martingale
- Daily-loss circuit breaker
- Consecutive-loss circuit breaker
- Backtesting
- Paper trade journal
- SQLite persistence
- Docker backend image
- GitHub Actions APK build
- GitHub Actions backend validation

## Live broker execution

Live IQ Option execution is intentionally not included. The app does not store broker credentials or use unofficial broker endpoints.

## Local backend

```bash
cd backend
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

## Android build

Install Flutter + Android Studio, then:

```bash
cd mobile
flutter create . --platforms=android
flutter pub get
flutter build apk --release
```

APK output:

`mobile/build/app/outputs/flutter-apk/app-release.apk`

## GitHub cloud build

Push this repository to GitHub. The workflow `.github/workflows/build-apk.yml` automatically creates the Android shell, installs dependencies and uploads the release APK as a workflow artifact.

For a hosted backend, set repository variable:

`API_BASE_URL=https://your-backend.example.com`

Then re-run the APK build.

## Risk statement

This project does not guarantee returns, including 30% daily profit. The system is intended for research, backtesting and paper trading.
