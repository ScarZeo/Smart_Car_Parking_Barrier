## SMART CAR PARKING BARRIER

## Anggota Kelompok

| No | Nama Lengkap | NIM |
| :-: | :--- | :--- |
| 1 | **Muammar Haqqi W** | `1232402024` |
| 2 | **Rio Adhi Permana** | `123240206` |
| 3 | **Rizqy Wildan Azka** | `123240219` |
| 4 | **Adiel Khairullah** | `123240221` |

---

## 📋 Gambaran Proyek

**Smart Car Parking Barrier** adalah prototipe sistem kendali gerbang parkir pintar yang dirancang untuk menggantikan sistem parkir konvensional menjadi sistem otomatis yang lebih aman dan terintegrasi.

Sistem ini terbagi menjadi tiga komponen utama sesuai dengan struktur proyek:
1. **Unit Kontrol Utama (`DEVKIT1`)**: Menggunakan **ESP32 DevKit V1** sebagai pengendali utama yang terhubung ke modul RFID RC522, layar LCD, dua sensor ultrasonik, motor servo (palang), buzzer, dan *push button*.
2. **Unit Kamera (`KAMERA_FINAL`)**: Menggunakan **ESP32-CAM** untuk mengambil foto kendaraan secara otomatis saat terjadi akses masuk, penolakan akses, maupun peringatan keamanan (*alert*).
3. **Unit Server & Database (`SERVER`)**: Menggunakan aplikasi server berbasis **Python** (`Smart_Parking_Barrier.py`) dan basis data **SQL** (`smart_parking.sql`) untuk memverifikasi ID kartu RFID, menyimpan riwayat aktivitas parkir, serta mengarsipkan foto hasil tangkapan kamera pada direktori `photos/`.

### Komponen Perangkat Keras
* **ESP32 DevKit V1** (Kontroler Utama & Konektivitas Wi-Fi)
* **ESP32-CAM** (Pengambilan Gambar Kendaraan)
* **Modul RFID RC522** (Pembacaan Kartu/Tag Akses)
* **LCD 16x2** (Penampil Status Akses & Informasi Sistem)
* **2x Sensor Ultrasonik** (Deteksi Kehadiran & Posisi Kendaraan di Area Palang)
* **Motor Servo** (Aktuator Pembuka dan Penutup Palang Parkir)
* **Buzzer** (Indikator Suara Status & Alarm Peringatan)
* **Push Button** (Kontrol Manual / Akses Darurat)
* **Breadboard & Kabel Jumper**

---

## Tujuan Proyek

1. **Otomatisasi Akses Parkir**: Mempercepat proses keluar-masuk kendaraan melalui autentikasi kartu RFID tanpa memerlukan operator penuh waktu.
2. **Meningkatkan Keamanan Area Parkir**: Mencegah akses kendaraan tidak terdaftar dengan verifikasi terpusat dan sistem peringatan dini (*alert*) jika terdeteksi anomali.
3. **Dokumentasi Visual Otomatis**: Merekam bukti foto setiap kendaraan yang berhasil masuk (`MASUK`), ditolak (`DITOLAK`), maupun yang memicu peringatan (`ALERT`) secara otomatis melalui ESP32-CAM.
4. **Pencatatan Data Terstruktur**: Menyimpan seluruh log aktivitas akses kendaraan dan manajemen kartu RFID ke dalam basis data SQL agar mudah dipantau dan diaudit.

---

## Alur Sistem

```text
[Kendaraan Tiba]
       │
       ▼
[Sensor Ultrasonik Mendeteksi Kendaraan]
       │
       ├──► (Tidak Tap Kartu / Pelanggaran) ──► Status: ALERT
       │                                        • Buzzer menyala (alarm)
       │                                        • ESP32-CAM memotret (..._ALERT_NA.jpg)
       │                                        • Log peringatan dikirim ke Server & SQL
       │
       ▼ (Tap Kartu RFID)
[ESP32 DevKit Membaca UID RFID & Mengirim ke Server Python]
       │
       ▼
[Server Memverifikasi UID pada Database SQL]
       │
       ├──► UID TIDAK TERDAFTAR ──────────────► Status: DITOLAK
       │                                        • LCD menampilkan "Akses Ditolak"
       │                                        • Buzzer berbunyi peringatan
       │                                        • Palang tetap tertutup
       │                                        • ESP32-CAM memotret (..._DITOLAK_<UID>.jpg)
       │
       └──► UID TERDAFTAR ────────────────────► Status: MASUK
                                                • LCD menampilkan "Akses Diterima"
                                                • Buzzer berbunyi konfirmasi
                                                • ESP32-CAM memotret (..._MASUK_<UID>.jpg)
                                                • Motor Servo membuka palang parkir
                                                • Sensor Ultrasonik mendeteksi kendaraan lewat
                                                • Motor Servo menutup palang otomatis
                                                • Log & foto tersimpan di Server & SQL
```

### Penjelasan Singkat Alur:
1. **Deteksi Kedatangan**: Sensor ultrasonik mendeteksi adanya kendaraan yang mendekat di depan palang parkir.
2. **Autentikasi RFID**: Pengemudi menempelkan kartu/tag RFID ke modul RC522. ESP32 DevKit membaca UID kartu dan mengirimkannya ke server Python melalui jaringan Wi-Fi.
3. **Validasi & Keputusan**:
   * **Akses Diterima (`MASUK`)**: Jika UID valid pada database `smart_parking.sql`, motor servo membuka palang, LCD menampilkan status masuk, dan ESP32-CAM menangkap foto dengan format `YYYYMMDD_HHMMSS_MASUK_<UID>.jpg`.
   * **Akses Ditolak (`DITOLAK`)**: Jika UID tidak terdaftar/tidak valid, palang tetap tertutup, buzzer berbunyi, LCD menampilkan penolakan, dan ESP32-CAM menangkap foto dengan format `YYYYMMDD_HHMMSS_DITOLAK_<UID>.jpg`.
   * **Peringatan Keamanan (`ALERT`)**: Jika terjadi kondisi tidak wajar (seperti upaya menerobos tanpa autentikasi), alarm buzzer aktif dan ESP32-CAM otomatis mengambil bukti foto dengan format `YYYYMMDD_HHMMSS_ALERT_NA.jpg`.
4. **Penutupan Otomatis & Kontrol Manual**: Setelah sensor ultrasonik mendeteksi kendaraan telah melewati palang dengan aman, motor servo menutup kembali secara otomatis. *Push button* juga disediakan pada panel kontrol untuk pengoperasian palang secara manual bila diperlukan.

---

## Fungsi Utama

1. **Autentikasi Kartu RFID Terpusat**
   Memvalidasi UID kartu kendaraan secara *real-time* terhadap basis data SQL melalui server Python.
2. **Kontrol Palang Otomatis & Aman**
   Menggerakkan motor servo secara otomatis saat akses disetujui serta memanfaatkan sensor ultrasonik untuk memastikan palang tidak menutup sebelum kendaraan benar-benar melintas.
3. **Pengambilan Gambar Otomatis (ESP32-CAM)**
   Mengambil dan mengirim foto kendaraan ke server secara otomatis dengan penamaan file terstruktur berdasarkan waktu, status kejadian (`MASUK`, `DITOLAK`, `ALERT`), dan UID kartu.
4. **Sistem Peringatan & Umpan Balik Lokal**
   Menyediakan informasi visual langsung di lokasi melalui **LCD** serta notifikasi audio/alarm melalui **Buzzer**.
5. **Kontrol Manual (*Override*)**
   Dilengkapi **Push Button** pada panel kontrol untuk membuka atau mengendalikan palang secara manual dalam situasi darurat atau kebutuhan khusus.
6. **Pencatatan Log & Manajemen Database**
   Menyimpan riwayat akses, data pengguna RFID, dan arsip foto kendaraan di sisi server (`Smart_Parking_Barrier.py` & `smart_parking.sql`).

---

