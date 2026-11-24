# MH4U Save Editor (APK via Chaquopy)

App Android = WebView + server `mh4u_server.py` yang jalan di dalam app (Chaquopy, Python 3.12).

## Cara build

**A. Tanpa PC (GitHub Actions)**
1. Buat repo GitHub, upload seluruh isi folder ini (termasuk folder `.github`).
2. Buka tab *Actions* -> *Build APK* -> tunggu selesai.
3. Unduh artifact `mh4u-editor-debug-apk` (zip berisi `app-debug.apk`), pasang di HP.

**B. Android Studio**
1. File -> Open -> pilih folder ini (JDK 17, Python 3.12 harus ada di PATH).
2. Build -> Build APK(s). Hasil: `app/build/outputs/apk/debug/app-debug.apk`.

## Pemakaian
- "Muat save": pilih file `user1` (pemilih file Android).
- "Unduh": pilih lokasi simpan; untuk menimpa save asli, simpan ke folder lalu salin ke folder save emulator.

## Catatan
- Kode server tidak diubah; `android_main.py` hanya menjalankannya & menambal tombol Unduh agar jalan di WebView.
- Ikon ada di `app/src/main/assets/icon/` (disalin ke penyimpanan app saat pertama dibuka).
- Versi: AGP 8.7.3, Gradle 8.9, Kotlin 2.0.21, Chaquopy 17.0.0, minSdk 24.
