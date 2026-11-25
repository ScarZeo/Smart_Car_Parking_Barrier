"""
SMART PARKING BARRIER - Server + Dashboard (Python)
====================================================
Pasangan untuk sketch Arduino/ESP32 `smart_parking_barrier.ino`.

Program ini menjalankan DUA hal sekaligus:
  1. Server HTTP (Flask) di port 5000 yang dipanggil ESP32:
       POST /api/access   -> cek kartu RFID boleh masuk/keluar atau tidak
       POST /api/confirm  -> konfirmasi kendaraan benar-benar lewat (hitung slot)
       POST /api/alert    -> laporan kejadian (tailgating, override manual)
       GET  /api/command  -> ESP32 mengambil perintah "buka palang" dari PC
  2. Dashboard (Tkinter): slot parkir, status ESP32, data kartu, log, override.
  3. Kamera ESP32-CAM: setiap izin/tolak/alert di gerbang, server mengambil foto dari
       GET <CAM_URL>/capture dan menautkannya ke baris log (kolom "Foto").
  4. OCR plat nomor (EasyOCR): gerbang hanya dibuka bila KARTU sah DAN PLAT terbaca dari foto.
       Kamera mati / plat tak terbaca -> akses ditolak. Hanya admin (override) yang bisa membuka.

Cara pakai:
  pip install flask pymysql
  1. Jalankan MySQL/MariaDB.
  2. (Opsional) import skema manual:  mysql -u root -p < smart_parking.sql
     Bila tidak diimport, program membuat database + tabel otomatis saat start.
  3. Atur koneksi lewat variabel lingkungan PARKIR_DB_HOST / _PORT / _USER / _PASS / _NAME
     atau ubah bagian "DATABASE MYSQL" di bawah.
  python Smart_Parking_Barrier.py

Lalu di sketch Arduino sesuaikan:
  SERVER_URL = "http://<IP-KOMPUTER-INI>:5000"
  API_KEY    = sama dengan API_KEY di bawah
"""

import csv
import difflib
import logging
import os
import re
import io
import socket
import threading
import time
import tkinter as tk
import urllib.request
import webbrowser
from datetime import datetime
from tkinter import filedialog, messagebox, simpledialog, ttk

import pymysql
import pymysql.cursors
from flask import Flask, jsonify, request

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# ============================ KONFIGURASI ============================
HOST = "0.0.0.0"
PORT = 5000
API_KEY = os.environ.get("PARKIR_API_KEY", "ganti-api-key-rahasia")  # harus sama dengan ESP32

# --- DATABASE MYSQL ---
DB_HOST = os.environ.get("PARKIR_DB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("PARKIR_DB_PORT", "3306"))
DB_USER = os.environ.get("PARKIR_DB_USER", "root")
DB_PASS = os.environ.get("PARKIR_DB_PASS", "")
DB_NAME = os.environ.get("PARKIR_DB_NAME", "smart_parking")
DEFAULT_MAX_SLOTS = 50
ONLINE_TIMEOUT = 15      # detik; ESP32 dianggap offline jika tidak ada kontak selama ini
REFRESH_MS = 1000        # interval refresh tampilan

# --- ESP32-CAM ---
CAM_URL = os.environ.get("PARKIR_CAM_URL", "http://192.168.137.222")   # IP ESP32-CAM; "" = kamera nonaktif
CAM_KEY = os.environ.get("PARKIR_CAM_KEY", API_KEY)                  # samakan dengan CAM_KEY di sketch kamera
CAM_TIMEOUT = 4                                                      # detik
CAM_FLASH_ON = False                                                # status flash manual dashboard
CAM_FLASH_BUSY = False                                              # mencegah klik berulang saat request berjalan
PHOTO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "photos")
SIMPAN_FOTO_DISK = False                                             # foto SELALU disimpan di MySQL; True = salinan file juga ditulis ke PHOTO_DIR
CAPTURE_EVENTS = ("IZIN", "DITOLAK", "ALERT", "OVERRIDE")            # kejadian yang otomatis difoto
AUTO_FLASH = True                                                    # foto ulang dengan flash bila gambar gelap
DARK_THRESHOLD = 40                                                  # rata-rata kecerahan 0-255; di bawah ini = gelap
CAM_STREAM_PORT = 81                                                 # port stream MJPEG ESP32-CAM (sama dengan pratinjau web)
LIVE_MODE = "foto"                                                   # "foto": /capture berkala (port 80, stabil, tidak bentrok dengan tab web)
                                                                     # "stream": MJPEG port 81 (mulus, tapi ESP32 hanya melayani 1 penonton)
LIVE_INTERVAL = 0.3                                                  # detik jeda antar foto pada mode "foto"
LIVE_HOLD = 5                                                     # detik foto kejadian/hasil tes ditahan sebelum kembali ke live

# --- OCR PLAT & ATURAN AKSES ---
OCR_ENABLED = True            # muat EasyOCR saat server start
OCR_GPU = False               # True bila PC punya GPU NVIDIA + CUDA
OCR_LEBAR = 960               # foto diubah ke lebar ini sebelum dibaca (px)
OCR_MIN_CONF = 0.30           # keyakinan minimum OCR (0-1)
PLAT_WAJIB_MASUK = True       # masuk: kartu SAH + plat terbaca, keduanya wajib
PLAT_WAJIB_KELUAR = False     # keluar: False = cukup kartu SAH (tanpa baca plat); True = kartu + plat wajib
FOTO_KELUAR = False           # False = kejadian keluar (izin/ditolak) tidak difoto sama sekali; True = tetap difoto untuk arsip
PLAT_TOLAK_GANDA = True       # True: plat yang masih tercatat di dalam (kartu lain) tidak boleh masuk lagi dengan kartu berbeda
PLAT_HARUS_COCOK = False     # biarkan False: 1 kartu boleh dipakai plat berbeda. True = plat harus mirip dengan plat terakhir kartu
PLAT_COCOK_MIN = 0.70         # kemiripan minimum (0-1) bila PLAT_HARUS_COCOK


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def fmt_duration(sec, detik=False):
    """Detik -> teks singkat: '2 jam 15 menit', '15 menit', '1 hari 3 jam'."""
    sec = max(0, int(sec))
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, sc = divmod(rem, 60)
    if d:
        return f"{d} hari {h} jam"
    if h:
        return f"{h} jam {m} menit"
    if detik and m:
        return f"{m} menit {sc} detik"
    if detik and not m:
        return f"{sc} detik"
    return f"{m} menit"


def parking_duration(rec, detik=False):
    """Lama parkir dari baris parking_records. Hanya terisi bila kendaraan SUDAH keluar;
    selama masih di dalam (atau tanpa waktu masuk/keluar) hasilnya teks kosong."""
    fmt = "%Y-%m-%d %H:%M:%S"
    if not rec or not rec["entry_time"] or not rec["exit_time"]:
        return ""
    try:
        t0 = datetime.strptime(rec["entry_time"], fmt)
        return fmt_duration((datetime.strptime(rec["exit_time"], fmt) - t0).total_seconds(), detik)
    except ValueError:
        return ""


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def port_free(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


# ============================ OCR PLAT NOMOR ============================
# Format plat Indonesia: 1-2 huruf (wilayah) + 1-4 angka + 1-3 huruf, mis. "B 1234 XYZ".
TO_LETTER = {"0": "O", "1": "I", "2": "Z", "5": "S", "6": "G", "8": "B"}   # salah baca angka -> huruf
TO_DIGIT = {"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "Z": "2",
            "S": "5", "G": "6", "T": "7", "B": "8"}                         # salah baca huruf -> angka
ALLOW = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def parse_plate(raw):
    """Ubah teks mentah OCR jadi plat valid. Return (plat, jumlah_koreksi) atau None."""
    s = re.sub(r"[^A-Z0-9]", "", str(raw).upper())
    if not 5 <= len(s) <= 9:
        return None
    # Cegah teks sembarang "dikoreksi" jadi plat: harus ada huruf & angka asli dari OCR
    if sum(ch.isalpha() for ch in s) < 2 or sum(ch.isdigit() for ch in s) < 1:
        return None
    best = None
    for a in (1, 2):                 # huruf wilayah
        for b in (1, 2, 3, 4):       # angka
            c = len(s) - a - b       # huruf belakang
            if not 1 <= c <= 3:
                continue
            seg1, seg2, seg3 = s[:a], s[a:a + b], s[a + b:]
            cost, ok = 0, True
            out1 = out2 = out3 = ""
            for ch in seg1:
                if ch.isdigit():
                    if ch in TO_LETTER: out1 += TO_LETTER[ch]; cost += 1
                    else: ok = False; break
                else:
                    out1 += ch
            for ch in seg2 if ok else "":
                if ch.isalpha():
                    if ch in TO_DIGIT: out2 += TO_DIGIT[ch]; cost += 1
                    else: ok = False; break
                else:
                    out2 += ch
            for ch in seg3 if ok else "":
                if ch.isdigit():
                    if ch in TO_LETTER: out3 += TO_LETTER[ch]; cost += 1
                    else: ok = False; break
                else:
                    out3 += ch
            if ok and cost <= 2 and (best is None or cost < best[1]):
                best = (f"{out1} {out2} {out3}", cost)
    return best


def best_plate(dets):
    """dets: [(x, y, tinggi, teks, conf)]. Cari gabungan teks berurutan yang membentuk plat."""
    if not dets:
        return None, 0.0
    hs = sorted(d[2] for d in dets)
    med_h = max(hs[len(hs) // 2], 1)
    rows = []                                        # kelompokkan per baris berdasarkan posisi y
    for d in sorted(dets, key=lambda d: d[1]):
        for r in rows:
            if abs(r["y"] - d[1]) < 0.6 * med_h:
                r["items"].append(d)
                r["y"] = sum(i[1] for i in r["items"]) / len(r["items"])
                break
        else:
            rows.append({"y": d[1], "items": [d]})
    best = (None, 0.0, 99)
    for r in rows:
        items = sorted(r["items"], key=lambda d: d[0])
        for i in range(len(items)):
            for j in range(i, min(i + 4, len(items))):
                chunk = items[i:j + 1]
                conf = sum(c[4] for c in chunk) / len(chunk)
                if conf < OCR_MIN_CONF:
                    continue
                res = parse_plate("".join(c[3] for c in chunk))
                if not res:
                    continue
                plat, cost = res
                score = conf - 0.1 * cost
                if best[0] is None or score > best[1] - 0.1 * best[2]:
                    best = (plat, conf, cost)
    return best[0], best[1]


PLAT_EVENTS = ("IZIN MASUK", "MASUK", "IZIN KELUAR", "KELUAR")     # baris log yang platnya boleh dikoreksi admin


def format_plate(text):
    """Rapikan plat ketikan admin jadi 'AB 12 GR'. None bila bentuknya bukan plat."""
    s = re.sub(r"[^A-Z0-9]", "", str(text).upper())
    m = re.fullmatch(r"([A-Z]{1,2})(\d{1,4})([A-Z]{0,3})", s)
    return " ".join(p for p in m.groups() if p) if m else None


class PlateReader:
    """Pembungkus EasyOCR: dimuat di background supaya server tidak macet saat start."""

    def __init__(self):
        self.reader = None
        self.state = "belum"          # belum | memuat | siap | error
        self.error = ""
        self.lock = threading.Lock()

    def load_async(self):
        if self.state in ("memuat", "siap"):
            return
        self.state = "memuat"

        def work():
            try:
                import easyocr
                self.reader = easyocr.Reader(["en"], gpu=OCR_GPU, verbose=False)
                self.state = "siap"
                print("OCR plat siap.")
            except Exception as e:
                self.state, self.error = "error", str(e)
                print("OCR gagal dimuat:", e)

        threading.Thread(target=work, daemon=True).start()

    def status_text(self):
        return {"belum": "nonaktif", "memuat": "memuat model...", "siap": "siap",
                "error": "ERROR - " + self.error[:60]}[self.state]

    def read(self, jpeg):
        """Return (plat atau None, conf, teks_mentah[list])."""
        import cv2
        import numpy as np
        img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None, 0.0, []
        h, w = img.shape[:2]
        if w != OCR_LEBAR:
            scale = OCR_LEBAR / w
            img = cv2.resize(img, (OCR_LEBAR, int(h * scale)),
                             interpolation=cv2.INTER_CUBIC if scale > 1 else cv2.INTER_AREA)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
        with self.lock:
            res = self.reader.readtext(gray, allowlist=ALLOW, detail=1, paragraph=False)
        dets, raw = [], []
        for bbox, text, conf in res:
            ys = [pt[1] for pt in bbox]
            xs = [pt[0] for pt in bbox]
            dets.append((sum(xs) / 4, sum(ys) / 4, max(ys) - min(ys), str(text), float(conf)))
            raw.append(f"{text}({conf:.2f})")
        plat, conf = best_plate(dets)
        return plat, conf, raw


plate_reader = PlateReader()


def save_photo(data, tag, uid, plate=""):
    """Simpan foto kendaraan ke tabel vehicle_images (MySQL). Return nama file (kunci foto)."""
    tag = re.sub(r"[^0-9A-Za-z]", "", tag)[:12] or "EVT"
    stamp = f"{datetime.now():%Y%m%d_%H%M%S_%f}"[:19]          # sampai milidetik -> nama unik
    name = f"{stamp}_{tag}_{uid or 'NA'}.jpg"
    store.save_image(name, data, tag, uid, plate)
    if SIMPAN_FOTO_DISK:
        os.makedirs(PHOTO_DIR, exist_ok=True)
        with open(os.path.join(PHOTO_DIR, name), "wb") as f:
            f.write(data)
    return name


LAST_CAM_ERROR = ""          # pesan error terakhir dari fetch_snapshot (untuk label status live)


def fetch_snapshot(flash=False):
    """Ambil 1 foto JPEG dari ESP32-CAM. Return bytes, atau None bila gagal."""
    global LAST_CAM_ERROR
    if not CAM_URL:
        return None
    url = CAM_URL.rstrip("/") + "/capture" + ("?flash=1" if flash else "")
    req = urllib.request.Request(url, headers={"X-API-KEY": CAM_KEY})
    try:
        with urllib.request.urlopen(req, timeout=CAM_TIMEOUT + (2 if flash else 0)) as r:
            data = r.read()
        if data[:2] != b"\xff\xd8":                            # validasi header JPEG
            LAST_CAM_ERROR = "respons bukan JPEG"
            return None
        LAST_CAM_ERROR = ""
        return data
    except Exception as e:
        LAST_CAM_ERROR = f"{type(e).__name__}: {e}"
        print("Kamera tidak bisa diakses:", e)
        return None



def set_cam_flash(enabled):
    """Kirim perintah ON/OFF flash manual ke ESP32-CAM."""
    global CAM_FLASH_ON

    if not CAM_URL:
        return False, "CAM_URL belum diisi."

    state = "on" if enabled else "off"
    url = CAM_URL.rstrip("/") + f"/flash?state={state}"

    req = urllib.request.Request(
        url,
        headers={"X-API-KEY": CAM_KEY},
        method="GET",
    )

    try:
        with urllib.request.urlopen(req, timeout=CAM_TIMEOUT) as response:
            result = response.read().decode("utf-8", errors="ignore")

        CAM_FLASH_ON = bool(enabled)
        return True, result

    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def brightness(jpeg):
    """Rata-rata kecerahan foto (0-255)."""
    import cv2
    import numpy as np
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_GRAYSCALE)
    return float(img.mean()) if img is not None else 255.0


def fetch_snapshot_auto():
    """Ambil foto dengan flash selalu menyala."""
    print("Mengambil foto dengan flash menyala...")
    data = fetch_snapshot(flash=True)
    if data:
        print("Foto berhasil diambil dengan flash.")
    else:
        print("Gagal mengambil foto dengan flash.")
    return data


def stream_url():
    """URL stream MJPEG kamera (port 81), diturunkan dari CAM_URL."""
    from urllib.parse import urlparse
    host = urlparse(CAM_URL).hostname or ""
    return f"http://{host}:{CAM_STREAM_PORT}/stream"


def read_mjpeg(resp, stop):
    """Generator frame JPEG dari respons stream multipart; berhenti bila stop() True."""
    while not stop():
        line = resp.readline()
        if not line:
            return                                    # koneksi ditutup kamera
        if line.lower().startswith(b"content-length:"):
            n = int(line.split(b":", 1)[1])
            while True:                               # lewati header sampai baris kosong
                l2 = resp.readline()
                if not l2:
                    return
                if not l2.strip():
                    break
            data = resp.read(n)
            if len(data) == n and data[:2] == b"\xff\xd8":
                yield data


# ============================ SKEMA MYSQL ============================
# Sama persis dengan isi file smart_parking.sql (untuk import manual).
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS cards (
        uid         VARCHAR(32)  NOT NULL,
        name        VARCHAR(100) NOT NULL DEFAULT '',
        plate       VARCHAR(20)  NOT NULL DEFAULT '',
        active      TINYINT(1)   NOT NULL DEFAULT 1,
        `inside`    TINYINT(1)   NOT NULL DEFAULT 0,
        last_seen   DATETIME     NULL,
        created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (uid),
        KEY idx_cards_plate (plate)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS vehicle_images (
        id          INT UNSIGNED NOT NULL AUTO_INCREMENT,
        filename    VARCHAR(120) NOT NULL,
        uid         VARCHAR(32)  NULL,
        event       VARCHAR(30)  NOT NULL DEFAULT '',
        plate       VARCHAR(20)  NOT NULL DEFAULT '',
        mime_type   VARCHAR(30)  NOT NULL DEFAULT 'image/jpeg',
        size_bytes  INT UNSIGNED NOT NULL DEFAULT 0,
        image       LONGBLOB     NOT NULL,
        captured_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (id),
        UNIQUE KEY uq_image_filename (filename),
        KEY idx_image_uid (uid),
        KEY idx_image_plate (plate),
        KEY idx_image_time (captured_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS access_logs (
        id      BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        ts      DATETIME     NOT NULL,
        device  VARCHAR(40)  NOT NULL DEFAULT '',
        event   VARCHAR(30)  NOT NULL,
        uid     VARCHAR(32)  NOT NULL DEFAULT '',
        plate   VARCHAR(20)  NOT NULL DEFAULT '',
        detail  TEXT         NULL,
        photo   VARCHAR(120) NULL,
        PRIMARY KEY (id),
        KEY idx_logs_ts (ts),
        KEY idx_logs_event (event),
        KEY idx_logs_uid (uid),
        KEY idx_logs_plate (plate),
        KEY idx_logs_photo (photo),
        CONSTRAINT fk_logs_photo FOREIGN KEY (photo) REFERENCES vehicle_images (filename)
            ON DELETE SET NULL ON UPDATE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS parking_records (
        id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        uid           VARCHAR(32)  NULL,
        card_name     VARCHAR(100) NOT NULL DEFAULT '',
        plate         VARCHAR(20)  NOT NULL DEFAULT '',
        is_guest      TINYINT(1)   NOT NULL DEFAULT 0,
        entry_time    DATETIME     NULL,
        entry_device  VARCHAR(40)  NULL,
        entry_photo   VARCHAR(120) NULL,
        exit_time     DATETIME     NULL,
        exit_device   VARCHAR(40)  NULL,
        exit_photo    VARCHAR(120) NULL,
        status        ENUM('DI DALAM','SELESAI','DIRESET') NOT NULL DEFAULT 'DI DALAM',
        PRIMARY KEY (id),
        KEY idx_rec_uid (uid),
        KEY idx_rec_plate (plate),
        KEY idx_rec_status (status),
        KEY idx_rec_entry (entry_time),
        KEY idx_rec_exit (exit_time),
        CONSTRAINT fk_rec_entry_photo FOREIGN KEY (entry_photo) REFERENCES vehicle_images (filename)
            ON DELETE SET NULL ON UPDATE CASCADE,
        CONSTRAINT fk_rec_exit_photo FOREIGN KEY (exit_photo) REFERENCES vehicle_images (filename)
            ON DELETE SET NULL ON UPDATE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS settings (
        setting_key   VARCHAR(50)  NOT NULL,
        setting_value VARCHAR(255) NOT NULL DEFAULT '',
        PRIMARY KEY (setting_key)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]


# ============================ DATA / LOGIKA ============================
class Store:
    """Penyimpanan MySQL + logika bisnis parkir (thread-safe)."""

    def __init__(self):
        self.lock = threading.RLock()
        # status runtime (tidak disimpan ke DB)
        self.last_seen = 0.0
        self.last_device = "-"
        self.pending_cmd = None
        self.last_unknown_uid = ""
        self.last_alert = ("", 0.0)
        self._connect()
        self._init_db()

    # ---------- util DB ----------
    def _connect(self):
        # buat database bila belum ada
        boot = pymysql.connect(host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASS, charset="utf8mb4")
        try:
            with boot.cursor() as cur:
                cur.execute(f"CREATE DATABASE IF NOT EXISTS `{DB_NAME}` "
                            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
            boot.commit()
        finally:
            boot.close()
        self.db = pymysql.connect(host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASS,
                                  database=DB_NAME, charset="utf8mb4",
                                  cursorclass=pymysql.cursors.DictCursor, autocommit=True)

    def _init_db(self):
        with self.lock:
            with self.db.cursor() as cur:
                for stmt in SCHEMA:
                    cur.execute(stmt)
                cur.execute("INSERT IGNORE INTO settings(setting_key, setting_value) VALUES('max_slots', %s)",
                            (str(DEFAULT_MAX_SLOTS),))
                cur.execute("INSERT IGNORE INTO settings(setting_key, setting_value) VALUES('guests', '0')")

    @staticmethod
    def _clean(row):
        """DATETIME -> teks 'YYYY-MM-DD HH:MM:SS' agar tampilan dashboard tidak berubah."""
        for k, v in row.items():
            if isinstance(v, datetime):
                row[k] = v.strftime("%Y-%m-%d %H:%M:%S")
        return row

    def _q(self, sql, args=()):
        with self.lock:
            self.db.ping(reconnect=True)
            with self.db.cursor() as cur:
                cur.execute(sql, args)
                return [self._clean(r) for r in cur.fetchall()]

    def _x(self, sql, args=()):
        """Jalankan INSERT/UPDATE/DELETE. Return lastrowid."""
        with self.lock:
            self.db.ping(reconnect=True)
            with self.db.cursor() as cur:
                cur.execute(sql, args)
                return cur.lastrowid

    # ---------- pengaturan & slot ----------
    def _get(self, key, default="0"):
        r = self._q("SELECT setting_value FROM settings WHERE setting_key=%s", (key,))
        return r[0]["setting_value"] if r else default

    def _set(self, key, value):
        self._x("INSERT INTO settings(setting_key, setting_value) VALUES(%s,%s) "
                "ON DUPLICATE KEY UPDATE setting_value=VALUES(setting_value)", (key, str(value)))

    def max_slots(self):
        return int(self._get("max_slots", DEFAULT_MAX_SLOTS))

    def set_max_slots(self, n):
        self._set("max_slots", int(n))

    def guests(self):
        """Kendaraan masuk lewat override (tanpa kartu)."""
        return int(self._get("guests", 0))

    def inside_count(self):
        return self._q("SELECT COUNT(*) AS c FROM cards WHERE `inside`=1")[0]["c"]

    def occupied(self):
        return self.inside_count() + self.guests()

    def slot_info(self):
        mx, occ = self.max_slots(), self.occupied()
        return {"slots_left": max(0, mx - occ), "max_slots": mx}

    # ---------- kartu ----------
    @staticmethod
    def norm_uid(uid):
        return re.sub(r"[^0-9A-Fa-f]", "", uid or "").upper()

    def card(self, uid):
        r = self._q("SELECT * FROM cards WHERE uid=%s", (uid,))
        return r[0] if r else None

    def cards(self):
        return self._q("SELECT * FROM cards ORDER BY name")

    def add_card(self, uid, name, plate):
        self._x("INSERT INTO cards(uid,name,plate) VALUES(%s,%s,%s)", (uid, name, plate))
        self.log("KARTU", uid, f"Kartu ditambahkan: {name} ({plate})", "PC")

    def update_card(self, uid, name, plate):
        self._x("UPDATE cards SET name=%s, plate=%s WHERE uid=%s", (name, plate, uid))

    def delete_card(self, uid):
        self._x("DELETE FROM cards WHERE uid=%s", (uid,))
        self.log("KARTU", uid, "Kartu dihapus", "PC")

    def set_active(self, uid, active):
        self._x("UPDATE cards SET active=%s WHERE uid=%s", (1 if active else 0, uid))
        self.log("KARTU", uid, "Kartu diaktifkan" if active else "Kartu diblokir", "PC")

    def set_inside(self, uid, inside, manual=False):
        self._x("UPDATE cards SET `inside`=%s WHERE uid=%s", (1 if inside else 0, uid))
        if manual and not inside:      # admin menandai "di luar" -> catatan parkir yang masih terbuka ditutup sebagai DIRESET
            self._x("UPDATE parking_records SET status='DIRESET' WHERE uid=%s AND status='DI DALAM'", (uid,))

    def reset_occupancy(self):
        self._x("UPDATE cards SET `inside`=0")
        self._x("UPDATE parking_records SET status='DIRESET' WHERE status='DI DALAM'")
        self._set("guests", 0)
        self.log("RESET", "", "Status parkir direset (semua kartu di luar)", "PC")

    # ---------- log ----------
    def log(self, event, uid="", detail="", device=None, photo=None, plate="", capture=True):
        dev = device or self.last_device
        log_id = self._x(
            "INSERT INTO access_logs(ts,device,event,uid,detail,photo,plate) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (now_str(), dev, event, uid or "", detail or "", photo or None, plate or ""),
        )
        if photo and plate:                      # foto kendaraan ikut mencatat plat yang terbaca
            self._x("UPDATE vehicle_images SET plate=%s WHERE filename=%s", (plate, photo))
        # Kejadian keluar tidak difoto bila FOTO_KELUAR=False (keluar hanya memakai kartu)
        is_exit = event == "IZIN KELUAR" or (event == "DITOLAK" and detail.startswith("KELUAR"))
        if is_exit and not FOTO_KELUAR:
            capture = False
        # Foto otomatis untuk kejadian di gerbang bila belum ada foto (bukan aksi dari dashboard PC)
        if capture and not photo and CAM_URL and dev != "PC" and event.startswith(CAPTURE_EVENTS):
            self._capture_async(log_id, event, uid)
        return log_id

    def _capture_async(self, log_id, event, uid):
        """Ambil foto di thread terpisah supaya respons ke ESP32 tidak tertunda."""
        def work():
            data = fetch_snapshot_auto()
            if not data:
                return
            name = save_photo(data, event, uid)
            self._x("UPDATE access_logs SET photo=%s WHERE id=%s", (name, log_id))

        threading.Thread(target=work, daemon=True).start()

    def verify_plate(self, uid, label):
        """Ambil foto lalu baca plat. Return (ok, plat, pesan_LCD, nama_foto, keterangan)."""
        if not CAM_URL:
            return False, "", "KAMERA OFF", None, "CAM_URL kosong - kamera nonaktif"
        if plate_reader.state == "memuat":
            return False, "", "OCR BELUM SIAP", None, "OCR sedang memuat model, coba beberapa saat lagi"
        if plate_reader.state != "siap":
            return False, "", "OCR ERROR", None, "OCR tidak aktif: " + (plate_reader.error or "OCR_ENABLED=False")
        data = fetch_snapshot_auto()
        if not data:
            return False, "", "KAMERA ERROR", None, "Kamera tidak merespons / tidak aktif"
        name = save_photo(data, label, uid)
        try:
            plat, conf, raw = plate_reader.read(data)
        except Exception as e:
            return False, "", "OCR ERROR", name, f"OCR gagal: {e}"
        if not plat:
            return False, "", "PLAT TDK TERBACA", name, "Plat tidak terbaca. Teks: " + (", ".join(raw)[:120] or "-")
        return True, plat, "OK", name, f"Plat {plat} (yakin {conf:.2f})"

    def card_inside_with_plate(self, plate, exclude_uid):
        """Kartu LAIN yang tercatat sedang di dalam dengan plat yang sama (spasi/tanda baca diabaikan)."""
        norm = re.sub(r"[^A-Z0-9]", "", str(plate).upper())
        if not norm:
            return None
        for r in self._q("SELECT uid, name, plate FROM cards WHERE `inside`=1 AND uid<>%s", (exclude_uid,)):
            if re.sub(r"[^A-Z0-9]", "", (r["plate"] or "").upper()) == norm:
                return r
        return None

    def log_row(self, log_id):
        r = self._q("SELECT * FROM access_logs WHERE id=%s", (log_id,))
        return r[0] if r else None

    def set_visit_plate(self, log_id, new_plate):
        """Koreksi plat satu kunjungan: IZIN MASUK + MASUK + KELUAR milik kartu itu
        (log, foto, dan catatan parking_records ikut diperbarui).
        Bila kunjungan tsb yang terbaru, plat di kartu ikut diperbarui. Return jumlah baris log yang diubah."""
        row = self.log_row(log_id)
        if not row or not row["uid"] or row["event"] not in PLAT_EVENTS:
            return 0
        uid = row["uid"]
        marks = ",".join(["%s"] * len(PLAT_EVENTS))
        with self.lock:
            self.db.ping(reconnect=True)
            self.db.begin()
            try:
                with self.db.cursor() as cur:
                    cur.execute("SELECT id, ts, plate, detail FROM access_logs WHERE uid=%s AND event='IZIN MASUK' "
                                "AND id<=%s ORDER BY id DESC LIMIT 1", (uid, log_id))
                    s = cur.fetchone()
                    start = s["id"] if s else log_id
                    cur.execute("SELECT id, ts FROM access_logs WHERE uid=%s AND event='IZIN MASUK' AND id>%s "
                                "ORDER BY id LIMIT 1", (uid, start))
                    nxt = cur.fetchone()
                    src = s if s else row
                    old = (src["plate"] or "").strip()
                    if not old:                                      # log lama tanpa kolom plat: ambil dari "(...)"
                        m = re.search(r"\(([^)]*)\)", src["detail"] or "")
                        old = m.group(1).strip() if m else ""
                    sql = f"SELECT id, detail, photo FROM access_logs WHERE uid=%s AND id>=%s AND event IN ({marks})"
                    args = [uid, start, *PLAT_EVENTS]
                    if nxt:
                        sql += " AND id<%s"
                        args.append(nxt["id"])
                    cur.execute(sql, args)
                    rows = cur.fetchall()
                    for r in rows:
                        detail = r["detail"] or ""
                        if old:
                            detail = detail.replace(f"({old})", f"({new_plate})")
                        cur.execute("UPDATE access_logs SET plate=%s, detail=%s WHERE id=%s", (new_plate, detail, r["id"]))
                        if r["photo"]:
                            cur.execute("UPDATE vehicle_images SET plate=%s WHERE filename=%s", (new_plate, r["photo"]))
                    # catatan masuk/keluar pada rentang kunjungan yang sama
                    sql2 = ("UPDATE parking_records SET plate=%s WHERE uid=%s "
                            "AND COALESCE(entry_time, exit_time) >= %s")
                    args2 = [new_plate, uid, (s["ts"] if s else row["ts"])]
                    if nxt:
                        sql2 += " AND COALESCE(entry_time, exit_time) < %s"
                        args2.append(nxt["ts"])
                    cur.execute(sql2, args2)
                    if not nxt:                                      # kunjungan terbaru -> plat kartu ikut berubah
                        cur.execute("UPDATE cards SET plate=%s WHERE uid=%s", (new_plate, uid))
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        return len(rows)

    # ---------- gambar kendaraan ----------
    def save_image(self, filename, data, tag, uid, plate=""):
        self._x("INSERT INTO vehicle_images(filename,uid,event,plate,size_bytes,image,captured_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (filename, uid or None, tag, plate or "", len(data), data, now_str()))

    def image_bytes(self, filename):
        r = self._q("SELECT image FROM vehicle_images WHERE filename=%s", (filename,))
        return r[0]["image"] if r else None

    def photo_of(self, log_id):
        r = self._q("SELECT photo FROM access_logs WHERE id=%s", (log_id,))
        return (r[0]["photo"] or "") if r else ""

    def latest_photo(self):
        r = self._q("SELECT photo FROM access_logs WHERE photo IS NOT NULL AND photo<>'' ORDER BY id DESC LIMIT 1")
        return r[0]["photo"] if r else ""

    def logs(self, limit=500, event_prefix=""):
        if event_prefix:
            return self._q("SELECT * FROM access_logs WHERE event LIKE %s ORDER BY id DESC LIMIT %s",
                           (event_prefix + "%", limit))
        return self._q("SELECT * FROM access_logs ORDER BY id DESC LIMIT %s", (limit,))

    def last_entry_photo(self):
        """Nama foto dari izin MASUK terakhir (untuk panel foto di tab Data Kartu)."""
        r = self._q("SELECT photo FROM access_logs WHERE event='IZIN MASUK' AND photo IS NOT NULL "
                    "AND photo<>'' ORDER BY id DESC LIMIT 1")
        return r[0]["photo"] if r else ""

    def last_durations(self):
        """{uid: lama parkir} untuk kunjungan TERAKHIR tiap kartu, hanya bila sudah selesai (sudah keluar)."""
        rows = self._q("SELECT uid, entry_time, exit_time, status FROM parking_records "
                       "WHERE id IN (SELECT MAX(id) FROM parking_records WHERE uid IS NOT NULL GROUP BY uid) "
                       "AND status='SELESAI'")
        return {r["uid"]: parking_duration(r) for r in rows}

    def parking_records(self, limit=500, status=""):
        if status:
            return self._q("SELECT * FROM parking_records WHERE status=%s ORDER BY id DESC LIMIT %s",
                           (status, limit))
        return self._q("SELECT * FROM parking_records ORDER BY id DESC LIMIT %s", (limit,))

    # ---------- logika API ----------
    def touch(self, device):
        self.last_seen = time.time()
        if device:
            self.last_device = device

    def check_access(self, uid, mode, device):
        """Memeriksa akses dan mendaftarkan kartu baru secara otomatis."""

        uid = self.norm_uid(uid)
        label = "MASUK" if mode == "entry" else "KELUAR"

        # Validasi UID
        if not uid:
            self.log("DITOLAK", "", f"{label}: UID kosong", device)
            return False, "UID TIDAK VALID"

        # Cari kartu di database
        c = self.card(uid)

        # Jika kartu belum terdaftar, daftarkan otomatis
        if c is None:
            nama_otomatis = f"Pengguna {uid[-4:]}"
            plat_otomatis = "BELUM DIISI"

            try:
                self._x(
                    """
                    INSERT IGNORE INTO cards
                    (uid, name, plate, active, `inside`, last_seen)
                    VALUES (%s, %s, %s, 1, 0, %s)
                    """,
                    (uid, nama_otomatis, plat_otomatis, now_str())
                )

                self.log(
                    "KARTU OTOMATIS",
                    uid,
                    f"Kartu baru otomatis terdaftar: {nama_otomatis}",
                    device
                )

            except pymysql.MySQLError as e:
                self.log(
                    "ERROR",
                    uid,
                    f"Gagal mendaftarkan kartu: {e}",
                    device
                )
                return False, "ERROR DATABASE"

            # Ambil kembali data kartu yang baru didaftarkan
            c = self.card(uid)

        # Jika masih tidak ditemukan
        if c is None:
            return False, "KARTU INVALID"

        # Periksa apakah kartu diblokir
        if not c["active"]:
            self.log(
                "DITOLAK",
                uid,
                f"{label}: kartu diblokir ({c['name']})",
                device
            )
            return False, "KARTU DIBLOKIR"

        # Pemeriksaan kendaraan masuk
        if mode == "entry":

            # Kartu tidak boleh masuk jika masih tercatat di dalam
            if c["inside"]:
                self.log(
                    "DITOLAK",
                    uid,
                    f"MASUK: {c['name']} sudah berada di dalam",
                    device
                )
                return False, "SUDAH DI DALAM"

            # Periksa kapasitas parkir
            if self.slot_info()["slots_left"] <= 0:
                self.log(
                    "DITOLAK",
                    uid,
                    "MASUK: parkir penuh",
                    device
                )
                return False, "PARKIR PENUH"

        # Pemeriksaan kendaraan keluar
        else:

            # Kartu harus tercatat sedang berada di dalam
            if not c["inside"]:
                self.log(
                    "DITOLAK",
                    uid,
                    f"KELUAR: {c['name']} belum tercatat masuk",
                    device
                )
                return False, "BELUM MASUK"

        # ---- KARTU SAH. Sekarang verifikasi PLAT dari kamera (keduanya wajib) ----
        plate, photo, plat_lama = "", None, ""
        if PLAT_WAJIB_MASUK if mode == "entry" else PLAT_WAJIB_KELUAR:
            ok, plate, msg, photo, info = self.verify_plate(uid, label)
            if not ok:
                self.log("DITOLAK", uid, f"{label}: {info} (kartu {c['name']})", device,
                         photo=photo, capture=False)
                return False, msg

            # Plat yang sama masih di dalam dengan kartu lain -> tolak (1 plat = 1 kendaraan di dalam)
            if mode == "entry" and PLAT_TOLAK_GANDA:
                lain = self.card_inside_with_plate(plate, uid)
                if lain is not None:
                    self.log("DITOLAK", uid,
                             f"MASUK: plat {plate} masih di dalam dengan kartu {lain['name']} ({lain['uid']}) "
                             f"- kartu {c['name']} ditolak",
                             device, photo=photo, plate=plate, capture=False)
                    return False, "PLAT SUDAH MASUK"

            terdaftar =(c["plate"] or "").strip().upper()
            if terdaftar and terdaftar != "BELUM DIISI":
                a = re.sub(r"[^A-Z0-9]", "", terdaftar)
                b = re.sub(r"[^A-Z0-9]", "", plate)
                sim = difflib.SequenceMatcher(None, a, b).ratio()
                if PLAT_HARUS_COCOK and sim < PLAT_COCOK_MIN:
                    self.log("DITOLAK", uid,
                             f"{label}: plat {plate} tidak cocok dengan plat kartu {terdaftar} "
                             f"(mirip {sim:.2f})", device, photo=photo, plate=plate, capture=False)
                    return False, "PLAT TIDAK COCOK"

            # 1 kartu boleh dipakai banyak plat: plat di kartu selalu mengikuti plat terakhir yang terbaca
            if plate != terdaftar:
                if terdaftar and terdaftar != "BELUM DIISI":
                    plat_lama = terdaftar
                self._x("UPDATE cards SET plate=%s WHERE uid=%s", (plate, uid))
                c = self.card(uid)

        # Perbarui waktu akses
        self._x(
            "UPDATE cards SET last_seen=%s WHERE uid=%s",
            (now_str(), uid)
        )

        # Catat pemberian izin
        self.log(
            "IZIN " + label,
            uid,
            f"{c['name']} ({plate or c['plate']}) - palang dibuka",
            device,
            photo=photo,
            plate=plate,
            capture=not photo,
        )

        return True, "OK"

    def confirm(self, mode, uid, passed, manual, device):
        """Kendaraan selesai melintas (atau timeout). Di sinilah slot dihitung."""
        if mode == "sync":
            return
        uid = self.norm_uid(uid)
        label = "MASUK" if mode == "entry" else "KELUAR"
        entry = mode == "entry"

        if not passed:
            self.log("BATAL", uid, f"{label}: palang terbuka tetapi kendaraan tidak lewat", device)
            return

        c = self.card(uid) if uid else None
        if c is not None:
            self.set_inside(uid, entry)
            plat = (c["plate"] or "").strip()
            plat = "" if plat == "BELUM DIISI" else plat
            self.log(label, uid, f"{c['name']} ({c['plate']})", device, plate=plat)
            self._record_pass(entry, uid, c["name"], plat, device)
        else:
            # lewat override admin (tanpa kartu): dihitung sebagai kendaraan tamu
            g = self.guests()
            g = g + 1 if entry else max(0, g - 1)
            self._set("guests", g)
            self.log(label, "", "Override admin (tanpa kartu)", device)
            self._record_pass(entry, None, "", "", device)

    def _record_pass(self, entry, uid, name, plate, device):
        """Catat kendaraan masuk/keluar ke tabel parking_records (uid=None -> tamu/override)."""
        ts, dev = now_str(), device or self.last_device
        label = "MASUK" if entry else "KELUAR"
        photo = None
        if uid:                                     # foto diambil dari log IZIN terakhir kartu ini
            r = self._q("SELECT photo FROM access_logs WHERE uid=%s AND event=%s ORDER BY id DESC LIMIT 1",
                        (uid, "IZIN " + label))
            photo = (r[0]["photo"] if r else None) or None
        with self.lock:
            if entry:
                if uid:                             # sisa catatan lama yang belum ditutup -> DIRESET
                    self._x("UPDATE parking_records SET status='DIRESET' WHERE uid=%s AND status='DI DALAM'", (uid,))
                self._x("INSERT INTO parking_records(uid,card_name,plate,is_guest,entry_time,entry_device,entry_photo,status) "
                        "VALUES(%s,%s,%s,%s,%s,%s,%s,'DI DALAM')",
                        (uid, name, plate, 0 if uid else 1, ts, dev, photo))
                return
            if uid:
                r = self._q("SELECT id FROM parking_records WHERE uid=%s AND status='DI DALAM' "
                            "ORDER BY id DESC LIMIT 1", (uid,))
            else:
                r = self._q("SELECT id FROM parking_records WHERE is_guest=1 AND status='DI DALAM' "
                            "ORDER BY id LIMIT 1")
            if r:
                self._x("UPDATE parking_records SET exit_time=%s, exit_device=%s, exit_photo=%s, status='SELESAI', "
                        "plate=IF(%s<>'', %s, plate) WHERE id=%s", (ts, dev, photo, plate, plate, r[0]["id"]))
            else:                                   # keluar tanpa catatan masuk (mis. posisi diubah manual)
                self._x("INSERT INTO parking_records(uid,card_name,plate,is_guest,exit_time,exit_device,exit_photo,status) "
                        "VALUES(%s,%s,%s,%s,%s,%s,%s,'SELESAI')",
                        (uid, name, plate, 0 if uid else 1, ts, dev, photo))

    def alert(self, kind, device):
        texts = {
            "tailgating": "TAILGATING! 1 kartu dipakai lebih dari 1 mobil",
            "manual_button": "Override admin lewat tombol di gerbang",
            "manual_pc": "Override admin lewat PC",
        }
        text = texts.get(kind, f"Kejadian: {kind}")
        if kind == "tailgating":
            self.last_alert = (text, time.time())
            self.log("ALERT", "", text, device)
        else:
            self.log("OVERRIDE", "", text, device)

    def send_command(self, cmd):
        self.pending_cmd = cmd

    def pop_command(self):
        cmd, self.pending_cmd = self.pending_cmd, None
        return cmd


try:
    store = Store()
except pymysql.MySQLError as _e:
    _r = tk.Tk()
    _r.withdraw()
    messagebox.showerror("MySQL tidak terhubung",
                         f"Tidak bisa terhubung ke MySQL di {DB_HOST}:{DB_PORT} (user '{DB_USER}').\n\n{_e}\n\n"
                         "Pastikan MySQL/MariaDB berjalan, lalu cek PARKIR_DB_HOST / _PORT / _USER / _PASS / _NAME.")
    raise SystemExit(1)

# ============================ SERVER FLASK ============================
app = Flask(__name__)
logging.getLogger("werkzeug").setLevel(logging.ERROR)


@app.before_request
def _auth():
    if not request.path.startswith("/api/"):
        return None
    if request.headers.get("X-API-KEY") != API_KEY:
        return jsonify(error="unauthorized"), 401
    body = request.get_json(silent=True) or {}
    store.touch(request.args.get("device") or body.get("device"))
    return None


@app.get("/")
def index():
    return "Smart Parking Barrier server aktif."


@app.post("/api/access")
def api_access():
    d = request.get_json(silent=True) or {}
    allow, msg = store.check_access(d.get("uid", ""), d.get("mode", "entry"), d.get("device"))
    return jsonify(allow=allow, message=msg, **store.slot_info())


@app.post("/api/confirm")
def api_confirm():
    d = request.get_json(silent=True) or {}
    store.confirm(d.get("mode", ""), d.get("uid", ""), bool(d.get("passed", False)),
                  bool(d.get("manual", False)), d.get("device"))
    return jsonify(ok=True, **store.slot_info())


@app.post("/api/alert")
def api_alert():
    d = request.get_json(silent=True) or {}
    store.alert(d.get("type", "unknown"), d.get("device"))
    return jsonify(ok=True)


@app.get("/api/command")
def api_command():
    return jsonify(cmd=store.pop_command() or "none", **store.slot_info())


def run_server():
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)


# ============================ DIALOG KARTU ============================
class CardDialog(tk.Toplevel):
    def __init__(self, parent, title, uid="", name="", plate="", uid_editable=True):
        super().__init__(parent)
        self.title(title)
        self.resizable(False, False)
        self.transient(parent)
        self.result = None

        frm = ttk.Frame(self, padding=16)
        frm.pack(fill="both", expand=True)
        self.v_uid, self.v_name, self.v_plate = tk.StringVar(value=uid), tk.StringVar(value=name), tk.StringVar(value=plate)

        for i, (lbl, var) in enumerate([("UID Kartu (hex)", self.v_uid), ("Nama Pemilik", self.v_name), ("Plat Nomor", self.v_plate)]):
            ttk.Label(frm, text=lbl).grid(row=i, column=0, sticky="w", pady=6, padx=(0, 12))
            e = ttk.Entry(frm, textvariable=var, width=32, state="normal" if (uid_editable or i > 0) else "readonly")
            e.grid(row=i, column=1, pady=6)
            if i == 0 and uid_editable:
                e.focus_set()
            elif i == 1 and not uid_editable:
                e.focus_set()

        btns = ttk.Frame(frm)
        btns.grid(row=3, column=0, columnspan=2, pady=(12, 0), sticky="e")
        ttk.Button(btns, text="Batal", command=self.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(btns, text="Simpan", command=self._ok).pack(side="right")
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()

    def _ok(self):
        uid = Store.norm_uid(self.v_uid.get())
        name, plate = self.v_name.get().strip(), self.v_plate.get().strip().upper()
        if not uid:
            messagebox.showwarning("Data kurang", "UID kartu wajib diisi (karakter 0-9 dan A-F).", parent=self)
            return
        if not name:
            messagebox.showwarning("Data kurang", "Nama pemilik wajib diisi.", parent=self)
            return
        self.result = (uid, name, plate)
        self.destroy()


# ============================ DASHBOARD ============================
GREEN, ORANGE, RED, BLUE, GREY = "#1e9e55", "#e08a00", "#d33a3a", "#2b6cb0", "#777777"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Smart Parking Barrier - Dashboard")
        self.geometry("1020x680")
        self.minsize(900, 600)
        self.ip = local_ip()
        self._sig = {}
        self._img_ref = None
        self._shown_photo = ""
        self._last_photo_name = None
        self._last_img_ref = None
        self._snap_busy = False
        self._snap_result = None
        self._snap_ready = False
        self._ocr_ready = False
        self._ocr_result = None
        self._ocr_last = "-"
        self._ocr_busy = False
        self._live = False
        self._live_gen = 0
        self._live_frame = None
        self._live_last_gate = 0.0
        self._live_status = "Live: mati"
        self._live_count = 0

        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=26)
        style.configure("Big.TButton", font=("Segoe UI", 12, "bold"), padding=10)

        # ---- header ----
        head = tk.Frame(self, bg="#14213d")
        head.pack(fill="x")
        tk.Label(head, text="SMART PARKING BARRIER", bg="#14213d", fg="white",
                 font=("Segoe UI", 16, "bold")).pack(side="left", padx=16, pady=10)
        self.lbl_dev = tk.Label(head, text="", bg="#14213d", font=("Segoe UI", 11, "bold"))
        self.lbl_dev.pack(side="right", padx=16)
        tk.Label(head, text=f"Server: http://{self.ip}:{PORT}", bg="#14213d", fg="#cbd5e1",
                 font=("Segoe UI", 10)).pack(side="right", padx=8)

        # ---- tabs ----
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=8, pady=8)
        self.tab_dash, self.tab_cards = ttk.Frame(self.nb), ttk.Frame(self.nb)
        self.tab_logs, self.tab_set = ttk.Frame(self.nb), ttk.Frame(self.nb)
        self.tab_rec = ttk.Frame(self.nb)
        for t, n in [(self.tab_dash, "Dashboard"), (self.tab_cards, "Data Kartu"),
                     (self.tab_rec, "Catatan Parkir"),
                     (self.tab_logs, "Log Aktivitas"), (self.tab_set, "Pengaturan")]:
            self.nb.add(t, text=n)

        self._build_dashboard()
        self._build_cards()
        self._build_records()
        self._build_logs()
        self._build_settings()
        self.refresh()
        if CAM_URL and HAS_PIL:
            self.after(1500, self.toggle_live)      # live view otomatis menyala saat dashboard dibuka

    # ---------------- Dashboard ----------------
    def _build_dashboard(self):
        top = ttk.Frame(self.tab_dash)
        top.pack(fill="x", padx=8, pady=8)

        # panel slot
        box = ttk.LabelFrame(top, text=" Slot Parkir ", padding=14)
        box.pack(side="left", fill="both", expand=True, padx=(0, 8))
        self.lbl_slot = tk.Label(box, text="--", font=("Segoe UI", 54, "bold"))
        self.lbl_slot.pack()
        tk.Label(box, text="SLOT TERSEDIA", font=("Segoe UI", 11), fg=GREY).pack()
        self.bar = ttk.Progressbar(box, maximum=100, length=360)
        self.bar.pack(pady=10, fill="x")
        self.lbl_detail = tk.Label(box, text="", font=("Segoe UI", 10), fg=GREY)
        self.lbl_detail.pack()
        self.lbl_state = tk.Label(box, text="", font=("Segoe UI", 12, "bold"))
        self.lbl_state.pack(pady=(6, 0))

        # panel kontrol
        ctl = ttk.LabelFrame(top, text=" Kontrol Palang ", padding=14)
        ctl.pack(side="left", fill="both", expand=True)
        ttk.Button(ctl, text="BUKA PALANG (OVERRIDE)", style="Big.TButton",
                   command=self.override).pack(fill="x")
        self.lbl_cmd = tk.Label(ctl, text="", font=("Segoe UI", 10), fg=ORANGE, wraplength=360, justify="left")
        self.lbl_cmd.pack(anchor="w", pady=(8, 12))
        tk.Label(ctl, text="Peringatan terakhir", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.lbl_alert = tk.Label(ctl, text="", font=("Segoe UI", 11, "bold"), wraplength=360,
                                  justify="left", padx=10, pady=10)
        self.lbl_alert.pack(fill="x", pady=4)
        self.lbl_unknown = tk.Label(ctl, text="", font=("Segoe UI", 10), fg=GREY, wraplength=360, justify="left")
        self.lbl_unknown.pack(anchor="w", pady=(6, 0))

        # panel kamera
        cam = ttk.LabelFrame(top, text=" Kamera Gerbang ", padding=8)
        cam.pack(side="left", fill="y", padx=(8, 0))
        self.lbl_img = tk.Label(cam, text="(belum ada foto)", width=40, height=13, bg="#e5e7eb", fg=GREY)
        self.lbl_img.pack()
        row = ttk.Frame(cam)
        row.pack(fill="x", pady=(6, 0))
        self.btn_snap = ttk.Button(row, text="Ambil Foto", command=self.snap_now)
        self.btn_snap.pack(side="left")
        self.btn_flash = ttk.Button(row, text="Flash: OFF", command=self.toggle_cam_flash)
        self.btn_flash.pack(side="left", padx=6)
        self.btn_live = ttk.Button(row, text="Live: OFF", command=self.toggle_live)
        self.btn_live.pack(side="left", padx=6)
        ttk.Button(row, text="Browser", command=self.open_live).pack(side="left")
        self.btn_ocr = ttk.Button(row, text="Tes Baca Plat", command=self.test_ocr)
        self.btn_ocr.pack(side="left", padx=6)
        self.lbl_ocr = tk.Label(cam, text="OCR: -", fg=GREY, wraplength=330, justify="left")
        self.lbl_ocr.pack(anchor="w", pady=(4, 0))
        self.lbl_live = tk.Label(cam, text="Live: mati", fg=GREY, wraplength=330, justify="left")
        self.lbl_live.pack(anchor="w")
        if not CAM_URL:
            tk.Label(cam, text="CAM_URL kosong - kamera nonaktif", fg=GREY).pack()
        elif not HAS_PIL:
            tk.Label(cam, text="pip install pillow untuk pratinjau", fg=RED).pack()

        # event terbaru
        ev = ttk.LabelFrame(self.tab_dash, text=" Aktivitas Terbaru ", padding=6)
        ev.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        bar_ev = ttk.Frame(ev)
        bar_ev.pack(fill="x", pady=(0, 4))
        ttk.Button(bar_ev, text="Koreksi Plat (baris terpilih)",
                   command=lambda: self.edit_plate_log(self.tree_recent)).pack(side="left")
        tk.Label(bar_ev, text="atau klik dua kali pada baris IZIN MASUK / MASUK / KELUAR",
                 fg=GREY).pack(side="left", padx=8)
        self.tree_recent = self._make_log_tree(ev, height=10)

    def override(self):
        if not messagebox.askyesno(
            "Override",
            "Kirim perintah buka palang secara manual?\n\n"
            "ESP32 akan memeriksa sensor masuk dan keluar terlebih dahulu."
        ):
            return

        store.send_command("open")

        store.log(
            "OVERRIDE",
            "",
            "Perintah buka palang dikirim dari Dashboard PC",
            "PC"
        )

        self.refresh()

    # ---------------- Kamera ----------------
    def _show_image(self, img):
        if not HAS_PIL:
            return
        img.thumbnail((320, 240))
        self._img_ref = ImageTk.PhotoImage(img)
        self.lbl_img.config(image=self._img_ref, text="", width=img.width, height=img.height)

    def _show_photo_file(self, name):
        """Tampilkan foto dari database (tabel vehicle_images). Return True bila berhasil."""
        if not (HAS_PIL and name):
            return False
        try:
            data = store.image_bytes(name)
            if not data:
                raise OSError("foto tidak ada di database")
            with Image.open(io.BytesIO(data)) as im:
                self._show_image(im.convert("RGB"))
            self._shown_photo = name
            return True
        except (OSError, pymysql.MySQLError):
            self._shown_photo = name          # foto hilang/rusak: jangan dicoba terus tiap detik
            return False

    def toggle_cam_flash(self):
        """Nyalakan/matikan flash ESP32-CAM dari dashboard tanpa memblokir GUI."""
        global CAM_FLASH_ON, CAM_FLASH_BUSY

        if not CAM_URL:
            messagebox.showinfo("Flash Kamera", "CAM_URL belum diisi. Kamera sedang nonaktif.")
            return

        if CAM_FLASH_BUSY:
            return

        CAM_FLASH_BUSY = True
        self.btn_flash.config(state="disabled")
        target = not CAM_FLASH_ON

        def work():
            success, result = set_cam_flash(target)

            def finish():
                global CAM_FLASH_BUSY
                CAM_FLASH_BUSY = False

                # Jaga-jaga jika jendela dashboard sudah ditutup.
                try:
                    self.btn_flash.config(state="normal")
                except tk.TclError:
                    return

                if success:
                    self.btn_flash.config(
                        text="Flash: ON" if target else "Flash: OFF"
                    )
                    print("ESP32-CAM flash:", "ON" if target else "OFF", result)
                else:
                    messagebox.showerror(
                        "Flash Kamera",
                        "Gagal mengontrol flash ESP32-CAM.\\n\\n"
                        + str(result)
                        + "\\n\\nPeriksa IP kamera, CAM_KEY, dan pastikan firmware "
                          "ESP32-CAM sudah memiliki endpoint /flash."
                    )

            try:
                self.after(0, finish)
            except tk.TclError:
                CAM_FLASH_BUSY = False

        threading.Thread(target=work, daemon=True).start()

    def snap_now(self):
        if not CAM_URL:
            messagebox.showinfo("Kamera", "CAM_URL belum diisi di parking_server.py")
            return
        if self._snap_busy:
            return
        self._snap_busy = True
        self.btn_snap.state(["disabled"])

        def work():
            self._snap_result = fetch_snapshot(flash=True)
            self._snap_ready = True

        threading.Thread(target=work, daemon=True).start()

    def test_ocr(self):
        if self._ocr_busy:
            return
        if plate_reader.state != "siap":
            messagebox.showinfo("OCR", f"OCR belum siap: {plate_reader.status_text()}")
            return
        self._ocr_busy = True
        self.btn_ocr.state(["disabled"])

        def work():
            data = fetch_snapshot_auto()
            if not data:
                self._ocr_result = ("Kamera tidak merespons", None)
            else:
                try:
                    plat, conf, raw = plate_reader.read(data)
                    txt = (f"Plat: {plat} (yakin {conf:.2f})" if plat
                           else "Plat TIDAK terbaca. Teks: " + (", ".join(raw)[:100] or "-"))
                    self._ocr_result = (txt, data)
                except Exception as e:
                    self._ocr_result = (f"OCR gagal: {e}", data)
            self._ocr_ready = True

        threading.Thread(target=work, daemon=True).start()

    def open_live(self):
        if CAM_URL:
            webbrowser.open(f"{CAM_URL.rstrip('/')}/?key={CAM_KEY}")

    def toggle_live(self):
        """Live view di dashboard. LIVE_MODE 'foto' = /capture berkala; 'stream' = MJPEG port 81."""
        if not CAM_URL:
            messagebox.showinfo("Kamera", "CAM_URL belum diisi di parking_server.py")
            return
        self._live = not self._live
        self._live_gen += 1
        gen = self._live_gen
        self.btn_live.config(text="Live: ON" if self._live else "Live: OFF")
        if not self._live:
            self._live_status = "Live: mati"
            return

        def alive():
            return self._live and gen == self._live_gen

        def loop_foto():
            """Ambil /capture berulang (port 80). Tidak memakai slot stream port 81."""
            self._live_status = "Live: menghubungkan..."
            while alive():
                t0 = time.time()
                d = fetch_snapshot()
                if d:
                    self._live_frame = d
                    self._live_count += 1
                    self._live_status = f"Live: ON (foto berkala, {time.time() - t0:.1f} dtk/foto)"
                else:
                    self._live_status = f"Live: kamera tidak merespons ({LAST_CAM_ERROR[:70] or 'tanpa data'}) - cek CAM_URL"
                    time.sleep(1.5)
                time.sleep(LIVE_INTERVAL)

        def loop_stream():
            while alive():
                try:
                    self._live_status = "Live: menghubungkan..."
                    req = urllib.request.Request(stream_url(), headers={"X-API-KEY": CAM_KEY})
                    with urllib.request.urlopen(req, timeout=CAM_TIMEOUT) as r:
                        for frame in read_mjpeg(r, lambda: not alive()):
                            self._live_frame = frame          # simpan frame terbaru saja
                            self._live_count += 1
                            self._live_status = "Live: ON"
                except Exception as e:
                    self._live_status = (f"Live terputus ({type(e).__name__}: {str(e)[:50]}). "
                                         "Stream hanya 1 penonton: tutup tab pratinjau web, atau pakai LIVE_MODE='foto'")
                    print("Live view:", e)
                if alive():
                    time.sleep(1.5)

        loop = loop_stream if LIVE_MODE == "stream" else loop_foto
        threading.Thread(target=loop, daemon=True).start()
        self._tick_live(gen)

    def _tick_live(self, gen):
        if not self._live or gen != self._live_gen:
            return
        d, self._live_frame = self._live_frame, None
        # foto kejadian gerbang / hasil tes ditahan LIVE_HOLD detik, baru kembali ke live
        if d and HAS_PIL and time.time() - self._live_last_gate > LIVE_HOLD:
            import io
            try:
                self._show_image(Image.open(io.BytesIO(d)).convert("RGB"))
            except OSError:
                pass
        self.lbl_live.config(text=self._live_status)
        self.after(200, lambda: self._tick_live(gen))

    def edit_plate_log(self, tree):
        """Admin mengoreksi plat hasil OCR pada baris log terpilih (satu kunjungan sekaligus)."""
        sel = tree.selection()
        if not sel:
            messagebox.showinfo("Pilih baris", "Pilih dulu baris IZIN MASUK / MASUK / KELUAR yang platnya mau dikoreksi.")
            return
        try:
            row = store.log_row(int(sel[0]))
        except ValueError:
            return
        if not row or not row["uid"] or row["event"] not in PLAT_EVENTS:
            messagebox.showinfo("Koreksi plat", "Baris ini tidak punya plat yang bisa dikoreksi.\n"
                                "Pilih baris IZIN MASUK, MASUK, atau KELUAR milik sebuah kartu.")
            return
        awal = (row["plate"] or "").strip()
        if not awal:
            m = re.search(r"\(([^)]*)\)", row["detail"] or "")
            awal = m.group(1).strip() if m else ""
        teks = simpledialog.askstring("Koreksi Plat", f"Plat nomor yang benar untuk kartu {row['uid']}:\n"
                                      "(contoh: AB 12 GR)", initialvalue=awal, parent=self)
        if teks is None:
            return
        baru = format_plate(teks)
        if not baru:
            messagebox.showwarning("Format salah", "Plat harus berupa huruf wilayah, angka, lalu huruf, "
                                   "misalnya AB 12 GR atau B 1234 XYZ.")
            return
        n = store.set_visit_plate(int(sel[0]), baru)
        store.log("KOREKSI", row["uid"], f"Plat dikoreksi admin: {awal or '-'} -> {baru}", "PC")
        for k in ("recent", "logs", "cards"):                  # paksa tabel digambar ulang pada siklus berikut
            self._sig.pop(k, None)
        messagebox.showinfo("Koreksi plat", f"Plat menjadi {baru}.\n{n} baris log pada kunjungan ini diperbarui.")

    def _on_log_select(self, event):
        sel = event.widget.selection()
        if not sel:
            return
        try:
            name = store.photo_of(int(sel[0]))
        except ValueError:
            return
        if name:
            self._show_photo_file(name)
        else:
            self._img_ref = None
            self.lbl_img.config(image="", text="(kejadian ini tanpa foto)", width=40, height=13)

    def _refresh_camera(self):
        # hasil "Ambil Foto" dari thread -> ditampilkan di thread GUI
        if self._snap_ready:
            self._snap_ready = False
            self._snap_busy = False
            self.btn_snap.state(["!disabled"])
            if self._snap_result:
                if HAS_PIL:
                    import io
                    self._show_image(Image.open(io.BytesIO(self._snap_result)).convert("RGB"))
                    self._live_last_gate = time.time()          # tahan agar tidak langsung ditimpa live
            else:
                messagebox.showwarning("Kamera", "Gagal mengambil foto.\nCek CAM_URL / CAM_KEY / WiFi kamera.")
        if self._ocr_ready:
            self._ocr_ready = False
            self._ocr_busy = False
            self.btn_ocr.state(["!disabled"])
            txt, data = self._ocr_result
            self._ocr_last = txt
            if data and HAS_PIL:
                import io
                self._show_image(Image.open(io.BytesIO(data)).convert("RGB"))
                self._live_last_gate = time.time()
        self.lbl_ocr.config(text=f"OCR: {plate_reader.status_text()}   |   Tes: {self._ocr_last}")
        # foto terbaru dari gerbang -> tampil otomatis
        latest = store.latest_photo()
        if latest and latest != self._shown_photo:
            if self._show_photo_file(latest):
                self._live_last_gate = time.time()      # hanya bila foto benar-benar tampil

    # ---------------- Kartu ----------------
    def _build_cards(self):
        bar = ttk.Frame(self.tab_cards)
        bar.pack(fill="x", padx=8, pady=8)
        for text, cmd in [("+ Tambah", self.card_add), ("Edit", self.card_edit), ("Hapus", self.card_delete),
                          ("Blokir / Aktifkan", self.card_toggle_active),
                          ("Ubah Posisi (Dalam/Luar)", self.card_toggle_inside),
                          ("Daftarkan UID Terakhir Ditolak", self.card_add_unknown)]:
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=(0, 6))

        # foto kendaraan terakhir masuk (hanya foto)
        last = ttk.LabelFrame(self.tab_cards, text=" Foto Kendaraan Terakhir Masuk ", padding=8)
        last.pack(side="bottom", fill="x", padx=8, pady=(0, 8))
        self.lbl_last_img = tk.Label(last, text="(belum ada foto)", width=28, height=9, bg="#e5e7eb", fg=GREY)
        self.lbl_last_img.pack()

        cols = ("uid", "name", "plate", "status", "pos", "dur", "seen")
        self.tree_cards = ttk.Treeview(self.tab_cards, columns=cols, show="headings", selectmode="browse")
        for c, t, w in [("uid", "UID", 130), ("name", "Nama", 200), ("plate", "Plat", 110),
                        ("status", "Status", 90), ("pos", "Posisi", 90), ("dur", "Lama Parkir", 130),
                        ("seen", "Terakhir Diakses", 170)]:
            self.tree_cards.heading(c, text=t)
            self.tree_cards.column(c, width=w, anchor="w")
        self.tree_cards.tag_configure("blocked", foreground=RED)
        self.tree_cards.tag_configure("inside", foreground=GREEN)
        self.tree_cards.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.tree_cards.bind("<Double-1>", lambda e: self.card_edit())

    def _sel_uid(self):
        sel = self.tree_cards.selection()
        if not sel:
            messagebox.showinfo("Pilih kartu", "Pilih salah satu kartu di tabel dulu.")
            return None
        return sel[0]

    def _card_form(self, title, **kw):
        dlg = CardDialog(self, title, **kw)
        self.wait_window(dlg)
        return dlg.result

    def card_add(self, uid=""):
        res = self._card_form("Tambah Kartu", uid=uid)
        if res:
            try:
                store.add_card(*res)
            except pymysql.err.IntegrityError:
                messagebox.showerror("Gagal", "UID tersebut sudah terdaftar.")
            self.refresh()

    def card_add_unknown(self):
        if not store.last_unknown_uid:
            messagebox.showinfo("Belum ada", "Belum ada kartu tak dikenal yang ditolak.\n"
                                "Tap kartu baru di gerbang, lalu coba lagi.")
            return
        self.card_add(store.last_unknown_uid)

    def card_edit(self):
        uid = self._sel_uid()
        if not uid:
            return
        c = store.card(uid)
        res = self._card_form("Edit Kartu", uid=uid, name=c["name"], plate=c["plate"], uid_editable=False)
        if res:
            store.update_card(uid, res[1], res[2])
            self.refresh()

    def card_delete(self):
        uid = self._sel_uid()
        if uid and messagebox.askyesno("Hapus", f"Hapus kartu {uid}?"):
            store.delete_card(uid)
            self.refresh()

    def card_toggle_active(self):
        uid = self._sel_uid()
        if uid:
            store.set_active(uid, not store.card(uid)["active"])
            self.refresh()

    def card_toggle_inside(self):
        uid = self._sel_uid()
        if uid:
            c = store.card(uid)
            store.set_inside(uid, not c["inside"], manual=True)
            store.log("KOREKSI", uid, "Posisi diubah manual: " + ("di luar" if c["inside"] else "di dalam"), "PC")
            self.refresh()

    # ---------------- Kendaraan terakhir ----------------
    def _load_last_photo(self, name):
        self._last_img_ref = None
        if not HAS_PIL:
            self.lbl_last_img.config(image="", text="(pillow belum terpasang)", width=28, height=9)
            return
        try:
            data = store.image_bytes(name)
            with Image.open(io.BytesIO(data)) as raw:
                img = raw.convert("RGB")
            img.thumbnail((240, 180))
            self._last_img_ref = ImageTk.PhotoImage(img)
            self.lbl_last_img.config(image=self._last_img_ref, text="", width=img.width, height=img.height)
        except (OSError, TypeError, pymysql.MySQLError):
            self.lbl_last_img.config(image="", text="(foto tidak tersedia)", width=28, height=9)

    def _refresh_last_vehicle(self):
        name = store.last_entry_photo()
        if name and name != self._last_photo_name:      # foto hanya dimuat ulang bila berganti
            self._last_photo_name = name
            self._load_last_photo(name)

    # ---------------- Catatan Parkir ----------------
    def _build_records(self):
        bar = ttk.Frame(self.tab_rec)
        bar.pack(fill="x", padx=8, pady=8)
        ttk.Label(bar, text="Status:").pack(side="left")
        self.rec_filter = tk.StringVar(value="Semua")
        cb = ttk.Combobox(bar, textvariable=self.rec_filter, state="readonly", width=12,
                          values=["Semua", "DI DALAM", "SELESAI", "DIRESET"])
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        ttk.Button(bar, text="Ekspor CSV", command=self.export_records_csv).pack(side="right")
        tk.Label(bar, text="Lama parkir baru muncul setelah kendaraan keluar",
                 fg=GREY).pack(side="left", padx=12)
        holder = ttk.Frame(self.tab_rec)
        holder.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        cols = ("plate", "name", "uid", "entry", "exit", "dur", "status")
        self.tree_rec = ttk.Treeview(holder, columns=cols, show="headings")
        for c, t, w in [("plate", "Plat", 100), ("name", "Nama", 150), ("uid", "UID", 110),
                        ("entry", "Waktu Masuk", 150), ("exit", "Waktu Keluar", 150),
                        ("dur", "Lama Parkir", 150), ("status", "Status", 80)]:
            self.tree_rec.heading(c, text=t)
            self.tree_rec.column(c, width=w, anchor="w")
        self.tree_rec.tag_configure("inside", foreground=GREEN)
        self.tree_rec.tag_configure("reset", foreground=GREY)
        sb = ttk.Scrollbar(holder, orient="vertical", command=self.tree_rec.yview)
        self.tree_rec.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree_rec.pack(fill="both", expand=True)

    def _rec_status(self):
        v = self.rec_filter.get()
        return "" if v == "Semua" else v

    @staticmethod
    def _rec_row(r):
        dur = parking_duration(r)
        plate = (r["plate"] or "").strip()
        plate = "-" if plate in ("", "BELUM DIISI") else plate
        keluar = r["exit_time"] or ("masih di dalam" if r["status"] == "DI DALAM" else "-")
        tag = ("inside",) if r["status"] == "DI DALAM" else (("reset",) if r["status"] == "DIRESET" else ())
        return (str(r["id"]), (plate, r["card_name"] or ("(Tamu / override)" if r["is_guest"] else "-"),
                               r["uid"] or "-", r["entry_time"] or "-", keluar,
                               dur, r["status"]), tag)

    def export_records_csv(self):
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")],
                                            initialfile="catatan_parkir.csv")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["Plat", "Nama", "UID", "Waktu Masuk", "Waktu Keluar", "Lama Parkir", "Status", "Tamu"])
            for r in store.parking_records(100000, self._rec_status()):
                dur = parking_duration(r)
                w.writerow([r["plate"] or "", r["card_name"] or "", r["uid"] or "", r["entry_time"] or "",
                            r["exit_time"] or "", dur, r["status"], "ya" if r["is_guest"] else ""])
        messagebox.showinfo("Selesai", f"Catatan parkir disimpan ke:\n{path}")

    # ---------------- Log ----------------
    def _make_log_tree(self, parent, height=15):
        cols = ("ts", "device", "event", "uid", "plate", "detail", "photo")
        tree = ttk.Treeview(parent, columns=cols, show="headings", height=height)
        tree.bind("<<TreeviewSelect>>", self._on_log_select)
        tree.bind("<Double-1>", lambda e, t=tree: self.edit_plate_log(t) if t.identify_row(e.y) else None)
        for c, t, w in [("ts", "Waktu", 150), ("device", "Perangkat", 80), ("event", "Kejadian", 100),
                        ("uid", "UID", 110), ("plate", "Plat", 90), ("detail", "Keterangan", 360),
                        ("photo", "Foto", 50)]:
            tree.heading(c, text=t)
            tree.column(c, width=w, anchor="w")
        for tag, color in [("MASUK", GREEN), ("KELUAR", BLUE), ("DITOLAK", RED), ("ALERT", ORANGE),
                           ("OVERRIDE", ORANGE), ("BATAL", GREY)]:
            tree.tag_configure(tag, foreground=color)
        tree.pack(fill="both", expand=True)
        return tree

    def _build_logs(self):
        bar = ttk.Frame(self.tab_logs)
        bar.pack(fill="x", padx=8, pady=8)
        ttk.Label(bar, text="Filter:").pack(side="left")
        self.filter_var = tk.StringVar(value="Semua")
        cb = ttk.Combobox(bar, textvariable=self.filter_var, state="readonly", width=14,
                          values=["Semua", "IZIN", "MASUK", "KELUAR", "DITOLAK", "BATAL", "ALERT", "OVERRIDE", "KARTU"])
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        ttk.Button(bar, text="Koreksi Plat (baris terpilih)",
                   command=lambda: self.edit_plate_log(self.tree_logs)).pack(side="left", padx=12)
        ttk.Button(bar, text="Ekspor CSV", command=self.export_csv).pack(side="right")
        holder = ttk.Frame(self.tab_logs)
        holder.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.tree_logs = self._make_log_tree(holder)

    def _log_prefix(self):
        v = self.filter_var.get()
        return "" if v == "Semua" else v

    def export_csv(self):
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")],
                                            initialfile="log_parkir.csv")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["Waktu", "Perangkat", "Kejadian", "UID", "Plat", "Keterangan", "Foto"])
            for r in store.logs(100000, self._log_prefix()):
                w.writerow([r["ts"], r["device"], r["event"], r["uid"], r["plate"] or "", r["detail"], r["photo"] or ""])
        messagebox.showinfo("Selesai", f"Log disimpan ke:\n{path}")

    # ---------------- Pengaturan ----------------
    def _build_settings(self):
        f = ttk.Frame(self.tab_set, padding=20)
        f.pack(fill="both", expand=True)

        ttk.Label(f, text="Kapasitas parkir (jumlah slot)").grid(row=0, column=0, sticky="w", pady=6)
        self.spin = ttk.Spinbox(f, from_=1, to=9999, width=8)
        self.spin.set(store.max_slots())
        self.spin.grid(row=0, column=1, padx=10)
        ttk.Button(f, text="Simpan", command=self.save_slots).grid(row=0, column=2)

        ttk.Button(f, text="Reset status parkir (semua kendaraan dianggap di luar)",
                   command=self.reset_occ).grid(row=1, column=0, columnspan=3, sticky="w", pady=14)

        info = (
            "Pengaturan yang harus sama di sketch Arduino:\n\n"
            f'  const char* SERVER_URL = "http://{self.ip}:{PORT}";\n'
            f'  const char* API_KEY    = "{API_KEY}";\n'
            '  const char* DEVICE_ID  = "GATE01";\n\n'
            f"Kamera: CAM_URL = {CAM_URL or '(nonaktif)'}  (harus sama dengan IP ESP32-CAM)\n\n"
            f"Akses: plat wajib (masuk={PLAT_WAJIB_MASUK}, keluar={PLAT_WAJIB_KELUAR}), kartu selalu wajib; hanya admin\n"
            "(override) yang bisa membuka palang tanpa plat terbaca.\n\n"
            "Pastikan komputer dan ESP32 berada di jaringan WiFi yang sama, dan firewall\n"
            f"mengizinkan koneksi masuk ke port {PORT}.\n\n"
            "Catatan alur:\n"
            "  - Slot hanya berubah setelah ESP32 mengonfirmasi kendaraan benar-benar lewat.\n"
            "  - Override (tombol gerbang / tombol PC) dihitung sebagai kendaraan 'tamu'.\n"
            "  - Jika data tidak sinkron, ubah posisi kartu di tab Data Kartu atau gunakan Reset."
        )
        tk.Label(f, text=info, justify="left", anchor="w", font=("Consolas", 10), bg="#f3f4f6",
                 padx=14, pady=12).grid(row=2, column=0, columnspan=3, sticky="we", pady=10)

    def save_slots(self):
        try:
            n = int(self.spin.get())
            if n < 1:
                raise ValueError
        except ValueError:
            messagebox.showwarning("Tidak valid", "Masukkan angka bulat minimal 1.")
            return
        store.set_max_slots(n)
        self.refresh()

    def reset_occ(self):
        if messagebox.askyesno("Reset", "Semua kartu akan dianggap berada di luar dan hitungan tamu menjadi 0.\nLanjutkan?"):
            store.reset_occupancy()
            self.refresh()

    # ---------------- Refresh ----------------
    def _fill(self, key, tree, rows, tag_fn=None):
        """Isi ulang tabel hanya jika datanya berubah (hindari kedip)."""
        sig = [tuple(r) for _, r, _ in rows]
        if self._sig.get(key) == sig:
            return
        self._sig[key] = sig
        sel = tree.selection()
        tree.delete(*tree.get_children())
        for iid, vals, tags in rows:
            tree.insert("", "end", iid=iid, values=vals, tags=tags)
        if sel and tree.exists(sel[0]):
            tree.selection_set(sel[0])

    @staticmethod
    def _log_rows(rs):
        known = {"MASUK", "KELUAR", "DITOLAK", "ALERT", "OVERRIDE", "BATAL"}
        return [(str(r["id"]), (r["ts"], r["device"], r["event"], r["uid"], r["plate"] or "", r["detail"],
                                "ada" if r["photo"] else "-"),
                 (r["event"],) if r["event"] in known else ()) for r in rs]

    def refresh(self):
        try:
            self._refresh()
        except Exception as e:  # jangan sampai loop refresh mati
            print("Refresh error:", e)
        self.after(REFRESH_MS, self.refresh)

    def _refresh(self):
        # ---- status ESP32 ----
        age = time.time() - store.last_seen
        if store.last_seen and age < ONLINE_TIMEOUT:
            self.lbl_dev.config(text=f"● {store.last_device} ONLINE", fg="#4ade80")
        elif store.last_seen:
            self.lbl_dev.config(text=f"● {store.last_device} OFFLINE ({int(age)} dtk)", fg="#f87171")
        else:
            self.lbl_dev.config(text="● Menunggu koneksi ESP32...", fg="#fbbf24")

        # ---- slot ----
        info = store.slot_info()
        mx, left, occ = info["max_slots"], info["slots_left"], store.occupied()
        color = RED if left == 0 else (ORANGE if left <= max(1, mx // 10) else GREEN)
        self.lbl_slot.config(text=f"{left}", fg=color)
        self.bar["value"] = min(100, occ * 100 / mx) if mx else 0
        self.lbl_detail.config(text=f"Terisi {occ} dari {mx}   |   Kartu di dalam: {store.inside_count()}   |   Tamu (override): {store.guests()}")
        self.lbl_state.config(text="PARKIR PENUH" if left == 0 else "TERSEDIA", fg=color)

        self._refresh_camera()
        self._refresh_last_vehicle()

        # ---- kontrol ----
        self.lbl_cmd.config(text="Perintah buka palang menunggu diambil ESP32..." if store.pending_cmd else "")
        text, ts = store.last_alert
        if text and time.time() - ts < 30:
            self.lbl_alert.config(text=text, bg="#fde2e2", fg=RED)
        else:
            self.lbl_alert.config(text=text and f"(sebelumnya) {text}" or "Tidak ada peringatan", bg="#f3f4f6", fg=GREY)
        self.lbl_unknown.config(
            text=f"UID tak dikenal terakhir: {store.last_unknown_uid}  (daftarkan di tab Data Kartu)"
            if store.last_unknown_uid else "")

        # ---- tabel ----
        self._fill("recent", self.tree_recent, self._log_rows(store.logs(15)))
        self._fill("logs", self.tree_logs, self._log_rows(store.logs(500, self._log_prefix())))
        rows = []
        durs = store.last_durations()
        for c in store.cards():
            tags = ("blocked",) if not c["active"] else (("inside",) if c["inside"] else ())
            rows.append((c["uid"], (c["uid"], c["name"], c["plate"],
                                    "Aktif" if c["active"] else "Diblokir",
                                    "Di Dalam" if c["inside"] else "Di Luar",
                                    "" if c["inside"] else durs.get(c["uid"], ""),
                                    c["last_seen"] or "-"), tags))
        self._fill("cards", self.tree_cards, rows)
        self._fill("records", self.tree_rec, [self._rec_row(r) for r in store.parking_records(500, self._rec_status())])


def main():
    if not port_free(PORT):
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Port dipakai", f"Port {PORT} sudah digunakan program lain.\n"
                             "Tutup program tersebut atau ubah PORT di bagian atas file.")
        return
    if OCR_ENABLED:
        plate_reader.load_async()          # model dimuat di background (pertama kali perlu internet)
    threading.Thread(target=run_server, daemon=True).start()
    App().mainloop()


if __name__ == "__main__":
    main()