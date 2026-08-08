@echo off
cd mobile
flutter create . --platforms=android
flutter pub get
flutter build apk --release
echo.
echo APK: mobile\build\app\outputs\flutter-apk\app-release.apk
pause
