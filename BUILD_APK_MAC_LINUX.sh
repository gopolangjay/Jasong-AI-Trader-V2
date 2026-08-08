#!/bin/sh
set -e
cd mobile
flutter create . --platforms=android
flutter pub get
flutter build apk --release
echo "APK: mobile/build/app/outputs/flutter-apk/app-release.apk"
