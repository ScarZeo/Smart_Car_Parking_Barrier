#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <SPI.h>
#include <MFRC522.h>
#include <Wire.h>
#include <LiquidCrystal_I2C.h>
#include <ESP32Servo.h>

// ================= KONFIGURASI JARINGAN =================
const char* WIFI_SSID  = "Adull";
const char* WIFI_PASS  = "123240221";
const char* SERVER_URL = "http://192.168.137.1:5000";
const char* API_KEY    = "ganti-api-key-rahasia";
const char* DEVICE_ID  = "GATE01";

// ================= PIN ESP32 =================
#define SS_PIN    5
#define RST_PIN   27

// Ultrasonic A (masuk)
#define TRIG_A    13
#define ECHO_A    14

// Ultrasonic B (keluar)
#define TRIG_B    32
#define ECHO_B    33

// Output / input
#define SERVO_PIN 26
#define BUZZER    25
#define BUTTON    4

// ================= PENGATURAN =================
const int BATAS_JARAK = 10;
const int SUDUT_TUTUP = 0;
const int SUDUT_BUKA  = 90;

const unsigned long TIMEOUT_PROSES = 30000;
const unsigned long CLEAR_DELAY = 1000;
const unsigned long MANUAL_AUTO_CLOSE = 5000;
const unsigned long POLL_INTERVAL = 2000;

// Tambahan: sensor kosong sebelum RFID -> kembali ke IDLE
const unsigned long AUTH_CLEAR_DELAY = 1500;

// ================= OBJEK =================
MFRC522 rfid(SS_PIN, RST_PIN);
LiquidCrystal_I2C lcd(0x27, 16, 2);
Servo palang;

// ================= STATUS SISTEM =================
enum State {
  IDLE,
  ENTRY_AUTH,
  ENTRY_CROSS,
  EXIT_AUTH,
  EXIT_CROSS
};

State state = IDLE;

String uidInput = "";
String currentUid = "";
String pendingEvent = "";

bool kendaraanLewat = false;
bool triggerCleared = false;
bool tailgateAlerted = false;

bool manualMode = false;
bool manualCross = false;

// Tambahan: admin membuka palang dan menunggu kendaraan
bool manualMenungguSensor = false;

// Timer sensor kosong saat menunggu RFID
unsigned long waktuSensorKosong = 0;
bool sensorKosongTiming = false;

int slotsLeft = -1;
int maxSlots = 50;

unsigned long waktuProses = 0;
unsigned long waktuManual = 0;
unsigned long waktuPoll = 0;
unsigned long tLastCross = 0;
unsigned long tWifi = 0;
unsigned long lcdHoldUntil = 0;

bool tombolSebelumnya = HIGH;

int hitA = 0;
int hitB = 0;

String lcdCache1 = "";
String lcdCache2 = "";

// ================= SENSOR =================
float bacaJarak(int trig, int echo) {
  digitalWrite(trig, LOW);
  delayMicroseconds(2);

  digitalWrite(trig, HIGH);
  delayMicroseconds(10);

  digitalWrite(trig, LOW);

  long durasi = pulseIn(echo, HIGH, 30000);

  if (durasi == 0) return 999;

  return durasi * 0.034 / 2;
}

bool sensorAktif(int trig, int echo) {
  return bacaJarak(trig, echo) < BATAS_JARAK;
}

// ================= AKTUATOR =================
void bukaPalang() {
  palang.write(SUDUT_BUKA);
}

void tutupPalang() {
  palang.write(SUDUT_TUTUP);
}

void bunyi(int jumlah, int durasi) {
  for (int i = 0; i < jumlah; i++) {
    tone(BUZZER, 2000);
    delay(durasi);

    noTone(BUZZER);
    delay(150);
  }
}

// ================= LCD =================
String pad16(String s) {
  while (s.length() < 16) s += ' ';
  return s.substring(0, 16);
}

void tampilLCD(String a, String b, unsigned long hold = 0) {
  a = pad16(a);
  b = pad16(b);

  if (a != lcdCache1) {
    lcd.setCursor(0, 0);
    lcd.print(a);
    lcdCache1 = a;
  }

  if (b != lcdCache2) {
    lcd.setCursor(0, 1);
    lcd.print(b);
    lcdCache2 = b;
  }

  if (hold) {
    lcdHoldUntil = millis() + hold;
  }
}

void updateLCD() {
  if (millis() < lcdHoldUntil) return;

  if (state == ENTRY_AUTH) {
    tampilLCD("KENDARAAN MASUK", "TAP KARTU RFID");
  }
  else if (state == ENTRY_CROSS) {
    tampilLCD("AKSES DITERIMA", "SILAKAN MASUK");
  }
  else if (state == EXIT_AUTH) {
    tampilLCD("KENDARAAN KELUAR", "TAP RFID DULU");
  }
  else if (state == EXIT_CROSS) {
    tampilLCD("AKSES DITERIMA", "SILAKAN KELUAR");
  }
  else if (manualMode && manualMenungguSensor) {
    tampilLCD("ADMIN ACCESS", "MENUNGGU MOBIL");
  }
  else if (manualMode) {
    tampilLCD("ADMIN ACCESS", "PALANG TERBUKA");
  }
  else {
    String l1 = (slotsLeft >= 0)
      ? ("SLOT: " + String(slotsLeft) + "/" + String(maxSlots))
      : "SLOT: ?";

    String l2 = (WiFi.status() == WL_CONNECTED)
      ? "STATUS: READY"
      : "WIFI OFFLINE";

    tampilLCD(l1, l2);
  }
}

// ================= HTTP =================
bool postJson(
  const char* path,
  const String& body,
  JsonDocument& res,
  uint16_t timeoutMs
) {
  if (WiFi.status() != WL_CONNECTED) return false;

  HTTPClient http;

  http.begin(String(SERVER_URL) + path);
  http.setTimeout(timeoutMs);

  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-API-KEY", API_KEY);

  int code = http.POST(body);

  bool ok = false;

  if (code == 200) {
    ok = !deserializeJson(res, http.getString());
  }

  http.end();

  return ok;
}

void updateSlots(JsonDocument& r) {
  if (r["slots_left"].is<int>()) {
    slotsLeft = r["slots_left"];
  }

  if (r["max_slots"].is<int>()) {
    maxSlots = r["max_slots"];
  }
}

// ================= AKSES SERVER =================
bool requestAccess(
  const char* mode,
  const String& uid,
  bool& allow,
  String& msg
) {
  JsonDocument req;

  req["uid"] = uid;
  req["device"] = DEVICE_ID;
  req["mode"] = mode;

  String body;
  serializeJson(req, body);

  JsonDocument res;

  // Server perlu mengambil foto dan menjalankan OCR (bisa beberapa detik di CPU)
  if (!postJson("/api/access", body, res, 10000)) {
    return false;
  }

  allow = res["allow"] | false;
  msg = String((const char*)(res["message"] | ""));

  updateSlots(res);

  return true;
}

// ================= KONFIRMASI KENDARAAN =================
void sendConfirm(
  const char* mode,
  const String& uid,
  bool passed,
  bool manual
) {
  JsonDocument req;

  req["device"] = DEVICE_ID;
  req["mode"] = mode;
  req["uid"] = uid;
  req["passed"] = passed;
  req["manual"] = manual;

  String body;
  serializeJson(req, body);

  JsonDocument res;

  if (postJson("/api/confirm", body, res, 4000)) {
    updateSlots(res);
  }
}

// ================= EVENT SERVER =================
void sendEvent(const String& type) {
  JsonDocument req;

  req["device"] = DEVICE_ID;
  req["type"] = type;

  String body;
  serializeJson(req, body);

  JsonDocument res;

  postJson("/api/alert", body, res, 1500);
}

void sendPendingEvent() {
  if (pendingEvent.length()) {
    sendEvent(pendingEvent);
    pendingEvent = "";
  }
}

// ================= RFID =================
void bacaRFID() {
  if (!rfid.PICC_IsNewCardPresent()) return;
  if (!rfid.PICC_ReadCardSerial()) return;

  uidInput = "";

  for (byte i = 0; i < rfid.uid.size; i++) {
    if (rfid.uid.uidByte[i] < 0x10) {
      uidInput += "0";
    }

    uidInput += String(rfid.uid.uidByte[i], HEX);
  }

  uidInput.toUpperCase();

  Serial.print("UID RFID: ");
  Serial.println(uidInput);

  rfid.PICC_HaltA();
  rfid.PCD_StopCrypto1();
}

// ================= RESET SESSION =================
void resetSession() {
  state = IDLE;

  kendaraanLewat = false;
  triggerCleared = false;
  tailgateAlerted = false;

  manualMode = false;
  manualCross = false;
  manualMenungguSensor = false;

  currentUid = "";
  uidInput = "";

  sensorKosongTiming = false;
  waktuSensorKosong = 0;

  hitA = 0;
  hitB = 0;
}

// ================= MULAI KENDARAAN MELINTAS =================
void startCross(bool entry) {
  state = entry ? ENTRY_CROSS : EXIT_CROSS;

  kendaraanLewat = false;
  triggerCleared = false;
  tailgateAlerted = false;

  manualMenungguSensor = false;

  sensorKosongTiming = false;
  waktuSensorKosong = 0;

  waktuProses = millis();
}

// ================= MULAI MASUK =================
void startEntry() {
  if (slotsLeft == 0) {
    static unsigned long lastFull = 0;

    if (millis() - lastFull > 5000) {
      lastFull = millis();

      tampilLCD(
        "PARKIR PENUH",
        "TIDAK BISA MASUK",
        2000
      );

      bunyi(1, 800);
    }

    return;
  }

  state = ENTRY_AUTH;

  uidInput = "";
  currentUid = "";

  sensorKosongTiming = false;
  waktuSensorKosong = 0;

  waktuProses = millis();

  Serial.println(
    "Kendaraan masuk terdeteksi. Tap kartu RFID."
  );
}

// ================= MULAI KELUAR =================
void startExit() {
  state = EXIT_AUTH;

  uidInput = "";
  currentUid = "";

  sensorKosongTiming = false;
  waktuSensorKosong = 0;

  waktuProses = millis();

  Serial.println(
    "Kendaraan keluar terdeteksi. Tap kartu RFID."
  );
}

// ================= SENSOR MENJAUH SAAT MENUNGGU RFID =================
void cekKendaraanMenjauh(bool sA, bool sB) {
  bool sedangAuth = (
    state == ENTRY_AUTH ||
    state == EXIT_AUTH
  );

  if (!sedangAuth) {
    sensorKosongTiming = false;
    return;
  }

  bool sensorMasihAktif = (
    (state == ENTRY_AUTH && sA) ||
    (state == EXIT_AUTH && sB)
  );

  if (sensorMasihAktif) {
    sensorKosongTiming = false;
    return;
  }

  if (!sensorKosongTiming) {
    sensorKosongTiming = true;
    waktuSensorKosong = millis();
  }

  if (
    sensorKosongTiming &&
    millis() - waktuSensorKosong >= AUTH_CLEAR_DELAY
  ) {
    Serial.println(
      "Kendaraan menjauh sebelum tap RFID."
    );

    state = IDLE;

    uidInput = "";
    currentUid = "";

    sensorKosongTiming = false;
    waktuSensorKosong = 0;

    hitA = 0;
    hitB = 0;

    tampilLCD(
      "KENDARAAN PERGI",
      "SLOT TERSEDIA",
      1000
    );
  }
}

// ================= PROSES MASUK =================
void prosesEntry(const String& uid) {
  tampilLCD("MEMPROSES...", "CEK KARTU", 10000);

  bool allow = false;
  String msg;

  bool ok = requestAccess("entry", uid, allow, msg);

  lcdHoldUntil = 0;

  if (!ok) {
    tampilLCD("SERVER ERROR", "HUBUNGI ADMIN", 2500);
    bunyi(2, 300);
    return;
  }

  if (allow) {
    Serial.println("MASUK DITERIMA, UID: " + uid);

    tampilLCD(
      "AKSES DITERIMA",
      "SLOT SISA: " + String(slotsLeft),
      2000
    );

    bunyi(1, 150);

    bukaPalang();

    currentUid = uid;
    manualCross = false;

    startCross(true);
  }
  else {
    Serial.println("DITOLAK: " + msg);

    tampilLCD("AKSES DITOLAK", msg, 2500);

    bunyi(3, 300);
  }
}

// ================= PROSES KELUAR =================
void prosesExit(const String& uid) {
  tampilLCD("MEMPROSES...", "CEK KARTU", 10000);

  bool allow = false;
  String msg;

  bool ok = requestAccess("exit", uid, allow, msg);

  lcdHoldUntil = 0;

  if (!ok) {
    tampilLCD("SERVER ERROR", "HUBUNGI ADMIN", 2500);
    bunyi(2, 300);
    return;
  }

  if (allow) {
    Serial.println("KELUAR DITERIMA, UID: " + uid);

    tampilLCD(
      "AKSES DITERIMA",
      "SILAKAN KELUAR",
      2000
    );

    bunyi(1, 150);

    bukaPalang();

    currentUid = uid;
    manualCross = false;

    startCross(false);
  }
  else {
    Serial.println("DITOLAK: " + msg);

    tampilLCD("AKSES DITOLAK", msg, 2500);

    bunyi(3, 300);
  }
}

// ================= KONFIRMASI KENDARAAN LEWAT =================
// entry=true  : Sensor A -> Sensor B
// entry=false : Sensor B -> Sensor A

void handleCross(bool entry, bool sA, bool sB) {
  bool crossSensor = entry ? sB : sA;
  bool triggerSide = entry ? sA : sB;

  // Sensor tujuan mendeteksi kendaraan
  if (crossSensor) {
    kendaraanLewat = true;
    tLastCross = millis();
  }

  // Anti-tailgating
  if (kendaraanLewat && !triggerSide) {
    triggerCleared = true;
  }

  if (
    kendaraanLewat &&
    triggerCleared &&
    triggerSide &&
    !tailgateAlerted &&
    !manualCross
  ) {
    tailgateAlerted = true;

    pendingEvent = "tailgating";

    tampilLCD(
      "1 KARTU 1 MOBIL",
      "TUNGGU GILIRAN",
      2500
    );

    bunyi(3, 100);
  }

  // Sensor tujuan sudah tidak mendeteksi kendaraan
  if (
    kendaraanLewat &&
    !crossSensor &&
    millis() - tLastCross >= CLEAR_DELAY
  ) {
    tutupPalang();

    bunyi(1, 150);

    sendConfirm(
      entry ? "entry" : "exit",
      currentUid,
      true,
      manualCross
    );

    Serial.println(
      entry
        ? "KENDARAAN BERHASIL MASUK"
        : "KENDARAAN BERHASIL KELUAR"
    );

    resetSession();

    sendPendingEvent();
  }
}

// ================= ADMIN MANUAL =================
void adminOverride(
  const char* sumber,
  bool sA,
  bool sB
) {
  // Jika kendaraan sedang melintas, abaikan perintah baru
  if (
    state == ENTRY_CROSS ||
    state == EXIT_CROSS
  ) {
    return;
  }

  Serial.print("ADMIN: MANUAL OVERRIDE (");
  Serial.print(sumber);
  Serial.println(")");

  bunyi(1, 150);

  // Buka palang
  bukaPalang();

  manualMode = true;
  manualCross = true;

  waktuManual = millis();

  currentUid = "";
  uidInput = "";

  // Jika sebelumnya sedang menunggu RFID,
  // gunakan arah kendaraan sebelumnya
  if (state == ENTRY_AUTH) {
    startCross(true);
  }
  else if (state == EXIT_AUTH) {
    startCross(false);
  }

  // Jika tombol diklik ketika IDLE dan Sensor A aktif
  else if (state == IDLE && sA) {
    startCross(true);
  }

  // Jika tombol diklik ketika IDLE dan Sensor B aktif
  else if (state == IDLE && sB) {
    startCross(false);
  }

  // Jika tidak ada sensor yang aktif,
  // buka palang dan tunggu kendaraan datang
  else if (state == IDLE && !sA && !sB) {
    manualMenungguSensor = true;

    hitA = 0;
    hitB = 0;

    Serial.println(
      "ADMIN: MENUNGGU KENDARAAN"
    );
  }

  sendEvent(String("manual_") + sumber);
}

// ================= TOMBOL FISIK ADMIN =================
void cekTombolAdmin(bool sA, bool sB) {
  bool tombol = digitalRead(BUTTON);

  // Deteksi ketika tombol pertama kali ditekan
  if (tombol == LOW && tombolSebelumnya == HIGH) {

    Serial.println("TOMBOL FISIK DITEKAN");

    // Jalankan override tanpa membatasi kondisi sensor
    adminOverride("button", sA, sB);
  }

  tombolSebelumnya = tombol;
}

// ================= PERINTAH DASHBOARD PYTHON =================
void pollCommand(bool sA, bool sB) {
  if (WiFi.status() != WL_CONNECTED) return;

  HTTPClient http;

  http.begin(
    String(SERVER_URL) +
    "/api/command?device=" +
    DEVICE_ID
  );

  http.setTimeout(1500);

  http.addHeader("X-API-KEY", API_KEY);

  if (http.GET() == 200) {
    JsonDocument res;

    if (!deserializeJson(res, http.getString())) {
      updateSlots(res);

      String cmd = String(
        (const char*)(res["cmd"] | "none")
      );

      if (cmd == "open") {
        http.end();

        // Fungsi sama dengan tombol fisik
        adminOverride("pc", sA, sB);

        return;
      }
    }
  }

  http.end();
}

// ================= WIFI =================
void connectWiFi() {
  tampilLCD("MENGHUBUNGKAN", "WIFI...");

  WiFi.mode(WIFI_STA);

  WiFi.begin(WIFI_SSID, WIFI_PASS);

  unsigned long t = millis();

  while (
    WiFi.status() != WL_CONNECTED &&
    millis() - t < 15000
  ) {
    delay(300);
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println(
      "WiFi OK, IP: " +
      WiFi.localIP().toString()
    );

    tampilLCD(
      "WIFI TERHUBUNG",
      WiFi.localIP().toString(),
      1500
    );
  }
  else {
    tampilLCD(
      "WIFI GAGAL",
      "MODE OFFLINE",
      1500
    );
  }

  delay(1500);
}

// ================= SETUP =================
void setup() {
  Serial.begin(115200);

  SPI.begin();
  rfid.PCD_Init();

  Wire.begin(21, 22);

  lcd.init();
  lcd.backlight();

  pinMode(TRIG_A, OUTPUT);
  pinMode(ECHO_A, INPUT);

  pinMode(TRIG_B, OUTPUT);
  pinMode(ECHO_B, INPUT);

  pinMode(BUZZER, OUTPUT);
  pinMode(BUTTON, INPUT_PULLUP);

  palang.attach(SERVO_PIN, 500, 2400);

  tutupPalang();

  tampilLCD("SMART PARKING", "BOOTING...");

  connectWiFi();

  // Sinkronisasi slot dari server
  JsonDocument res;

  if (
    postJson(
      "/api/confirm",
      "{\"mode\":\"sync\"}",
      res,
      3000
    )
  ) {
    updateSlots(res);
  }

  Serial.println(
    "SMART PARKING BARRIER siap."
  );

  bunyi(1, 100);
}

// ================= LOOP =================
void loop() {
  // Baca kedua sensor
  bool sA = sensorAktif(TRIG_A, ECHO_A);
  bool sB = sensorAktif(TRIG_B, ECHO_B);

  // RFID dan tombol fisik
  bacaRFID();

  cekTombolAdmin(sA, sB);

  // ---------- DASHBOARD PYTHON ----------
  // Polling dilakukan pada semua kondisi.
  // Jika sedang melintas, adminOverride akan mengabaikan
  // perintah tersebut, sama seperti tombol fisik.
  if (millis() - waktuPoll >= POLL_INTERVAL) {
    waktuPoll = millis();

    pollCommand(sA, sB);
  }

  // ---------- MENUNGGU KENDARAAN SETELAH ADMIN MEMBUKA ----------
  if (
    state == IDLE &&
    manualMode &&
    manualMenungguSensor
  ) {
    hitA = sA ? hitA + 1 : 0;
    hitB = sB ? hitB + 1 : 0;

    // Sensor A mendeteksi kendaraan masuk
    if (hitA >= 3) {
      hitA = 0;
      hitB = 0;

      Serial.println(
        "ADMIN: KENDARAAN TERDETEKSI SENSOR A"
      );

      startCross(true);
    }

    // Sensor B mendeteksi kendaraan keluar
    else if (hitB >= 3) {
      hitA = 0;
      hitB = 0;

      Serial.println(
        "ADMIN: KENDARAAN TERDETEKSI SENSOR B"
      );

      startCross(false);
    }
  }

  // ---------- IDLE NORMAL ----------
  else if (state == IDLE) {
    hitA = sA ? hitA + 1 : 0;
    hitB = sB ? hitB + 1 : 0;

    if (!manualMode) {
      if (hitA >= 3) {
        hitA = 0;
        startEntry();
      }
      else if (hitB >= 3) {
        hitB = 0;
        startExit();
      }
    }
  }

  // ---------- SENSOR MENJAUH SAAT MENUNGGU RFID ----------
  cekKendaraanMenjauh(sA, sB);

  // ---------- AUTENTIKASI MASUK ----------
  if (
    state == ENTRY_AUTH &&
    uidInput.length() > 0
  ) {
    String uid = uidInput;

    uidInput = "";

    if (!sA) {
      tampilLCD(
        "MOBIL TIDAK ADA",
        "DEKATI GERBANG",
        2000
      );

      bunyi(2, 150);
    }
    else {
      prosesEntry(uid);
    }
  }

  // ---------- AUTENTIKASI KELUAR ----------
  if (
    state == EXIT_AUTH &&
    uidInput.length() > 0
  ) {
    String uid = uidInput;

    uidInput = "";

    prosesExit(uid);
  }

  // ---------- KONFIRMASI LEWAT ----------
  if (state == ENTRY_CROSS) {
    handleCross(true, sA, sB);
  }

  if (state == EXIT_CROSS) {
    handleCross(false, sA, sB);
  }

  // ---------- TIMEOUT PROSES ----------
  // Override manual tidak menggunakan timeout 30 detik.
  // Palang menunggu sensor tujuan mendeteksi dan kemudian kosong.
  bool manualSedangMelintas = (
    manualCross &&
    (
      state == ENTRY_CROSS ||
      state == EXIT_CROSS
    )
  );

  if (
    state != IDLE &&
    millis() - waktuProses > TIMEOUT_PROSES &&
    !manualSedangMelintas
  ) {
    bool blocking = (
      (state == ENTRY_CROSS && sB) ||
      (state == EXIT_CROSS && sA)
    );

    if (blocking) {
      waktuProses = millis();
    }
    else {
      Serial.println("PROSES TIMEOUT");

      tutupPalang();

      if (
        state == ENTRY_CROSS ||
        state == EXIT_CROSS
      ) {
        bool entry = (state == ENTRY_CROSS);

        sendConfirm(
          entry ? "entry" : "exit",
          currentUid,
          kendaraanLewat,
          manualCross
        );
      }

      resetSession();

      tampilLCD(
        "PROSES TIMEOUT",
        "SILAKAN ULANGI",
        2000
      );

      bunyi(2, 300);
    }
  }

  // ---------- WIFI RECONNECT ----------
  if (
    WiFi.status() != WL_CONNECTED &&
    millis() - tWifi > 10000
  ) {
    tWifi = millis();

    WiFi.reconnect();
  }

  updateLCD();
}