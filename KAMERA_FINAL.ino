
#include <WiFi.h>
#include "esp_camera.h"
#include "esp_http_server.h"
#include "esp_timer.h"
#include <esp_arduino_version.h>
#include <string.h>

// ================= KONFIGURASI =================
const char* WIFI_SSID = "Adull";
const char* WIFI_PASS = "123240221";
const char* CAM_KEY   = "ganti-api-key-rahasia";

#define USE_STATIC_IP 0

IPAddress STATIC_IP(10, 133, 176, 142);
IPAddress GATEWAY(10, 133, 176, 1);
IPAddress SUBNET(255, 255, 255, 0);
IPAddress DNS1(8, 8, 8, 8);

// ---------- Profil PREVIEW ----------
#define STREAM_FRAME_SIZE  FRAMESIZE_VGA
#define STREAM_QUALITY     15
#define STREAM_MAX_FPS     8

// ---------- Profil FOTO OCR ----------
#define CAPTURE_FRAME_SIZE FRAMESIZE_XGA
#define CAPTURE_QUALITY    10
#define CAPTURE_SETTLE_MS  300
#define DISCARD_FRAMES     3

// ---------- Orientasi ----------
#define FLIP_VERTICAL      0
#define FLIP_HORIZONTAL    0

// ---------- Penyetelan sensor ----------
#define CAM_BRIGHTNESS     2
#define CAM_CONTRAST       2
#define CAM_SATURATION     0
#define CAM_SHARPNESS      2
#define CAM_AE_LEVEL       2
#define CAM_GAIN_CEILING   GAINCEILING_8X

// ---------- FLASH PWM ----------
#define FLASH_LED_GPIO     4
#define FLASH_PWM_FREQ     3000
#define FLASH_PWM_RES      8
#define FLASH_PWM_CHANNEL  7

#define FLASH_BRIGHTNESS   40
#define FLASH_SETTLE_MS    100

// ================= PIN AI-THINKER =================
#define PWDN_GPIO_NUM   32
#define RESET_GPIO_NUM  -1
#define XCLK_GPIO_NUM    0
#define SIOD_GPIO_NUM   26
#define SIOC_GPIO_NUM   27
#define Y9_GPIO_NUM     35
#define Y8_GPIO_NUM     34
#define Y7_GPIO_NUM     39
#define Y6_GPIO_NUM     36
#define Y5_GPIO_NUM     21
#define Y4_GPIO_NUM     19
#define Y3_GPIO_NUM     18
#define Y2_GPIO_NUM      5
#define VSYNC_GPIO_NUM  25
#define HREF_GPIO_NUM   23
#define PCLK_GPIO_NUM   22

#define PART_BOUNDARY "123456789000000000000987654321"

static const char* STREAM_CT =
  "multipart/x-mixed-replace;boundary=" PART_BOUNDARY;

static const char* STREAM_BOUNDARY =
  "\r\n--" PART_BOUNDARY "\r\n";

static const char* STREAM_PART =
  "Content-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n";

httpd_handle_t httpMain = NULL;
httpd_handle_t httpStream = NULL;

SemaphoreHandle_t captureMutex = NULL;

// Status flash manual
volatile bool manualFlashOn = false;

// ================= FLASH PWM =================

bool initFlashPWM() {

#if ESP_ARDUINO_VERSION_MAJOR >= 3

  bool ok = ledcAttachChannel(
    FLASH_LED_GPIO,
    FLASH_PWM_FREQ,
    FLASH_PWM_RES,
    FLASH_PWM_CHANNEL
  );

  if (!ok) {
    Serial.println("Gagal menginisialisasi PWM flash!");
    return false;
  }

#else

  double freq = ledcSetup(
    FLASH_PWM_CHANNEL,
    FLASH_PWM_FREQ,
    FLASH_PWM_RES
  );

  if (freq == 0) {
    Serial.println("Gagal menginisialisasi PWM flash!");
    return false;
  }

  ledcAttachPin(FLASH_LED_GPIO, FLASH_PWM_CHANNEL);

#endif

  Serial.println("PWM flash berhasil diinisialisasi.");
  return true;
}

void setFlashBrightness(uint8_t brightness) {

#if ESP_ARDUINO_VERSION_MAJOR >= 3

  ledcWrite(FLASH_LED_GPIO, brightness);

#else

  ledcWrite(FLASH_PWM_CHANNEL, brightness);

#endif

}

// ================= AUTENTIKASI =================

static bool authorized(httpd_req_t* req) {

  char val[64];

  if (
    httpd_req_get_hdr_value_str(
      req,
      "X-API-KEY",
      val,
      sizeof(val)
    ) == ESP_OK &&
    strcmp(val, CAM_KEY) == 0
  ) {
    return true;
  }

  size_t ql = httpd_req_get_url_query_len(req);

  if (ql > 0 && ql < 128) {

    char q[130];

    if (
      httpd_req_get_url_query_str(req, q, sizeof(q)) == ESP_OK &&
      httpd_query_key_value(q, "key", val, sizeof(val)) == ESP_OK &&
      strcmp(val, CAM_KEY) == 0
    ) {
      return true;
    }
  }

  return false;
}

static esp_err_t denyReq(httpd_req_t* req) {

  httpd_resp_send_err(
    req,
    HTTPD_401_UNAUTHORIZED,
    "unauthorized"
  );

  return ESP_FAIL;
}

// ================= PROFIL KAMERA =================

static void setProfile(framesize_t size, int quality) {

  sensor_t* s = esp_camera_sensor_get();

  if (!s) return;

  s->set_framesize(s, size);
  s->set_quality(s, quality);
}

static framesize_t captureSize() {

  return psramFound()
    ? CAPTURE_FRAME_SIZE
    : FRAMESIZE_VGA;
}

// ================= HANDLER CAPTURE =================

static esp_err_t captureHandler(httpd_req_t* req) {

  if (!authorized(req)) {
    return denyReq(req);
  }

  bool useFlash = false;

  char q[130];
  char v[8];

  if (
    httpd_req_get_url_query_str(req, q, sizeof(q)) == ESP_OK &&
    httpd_query_key_value(q, "flash", v, sizeof(v)) == ESP_OK
  ) {
    useFlash = (v[0] == '1');
  }

  if (
    xSemaphoreTake(
      captureMutex,
      pdMS_TO_TICKS(5000)
    ) != pdTRUE
  ) {
    httpd_resp_send_err(
      req,
      HTTPD_500_INTERNAL_SERVER_ERROR,
      "busy"
    );

    return ESP_FAIL;
  }

  bool previousFlashState = manualFlashOn;

  setProfile(captureSize(), CAPTURE_QUALITY);

  delay(CAPTURE_SETTLE_MS);

  if (useFlash && !previousFlashState) {
    setFlashBrightness(FLASH_BRIGHTNESS);
    delay(FLASH_SETTLE_MS);
  }

  for (int i = 0; i < DISCARD_FRAMES; i++) {

    camera_fb_t* t = esp_camera_fb_get();

    if (t) {
      esp_camera_fb_return(t);
    }
  }

  camera_fb_t* fb = esp_camera_fb_get();

  if (useFlash && !previousFlashState) {
    setFlashBrightness(0);
  }

  esp_err_t res = ESP_FAIL;

  if (!fb) {

    Serial.println("Capture gagal");

    httpd_resp_send_500(req);

  } else {

    httpd_resp_set_type(req, "image/jpeg");

    httpd_resp_set_hdr(
      req,
      "Content-Disposition",
      "inline; filename=capture.jpg"
    );

    httpd_resp_set_hdr(
      req,
      "Cache-Control",
      "no-store"
    );

    res = httpd_resp_send(
      req,
      (const char*)fb->buf,
      fb->len
    );

    esp_camera_fb_return(fb);
  }

  setProfile(STREAM_FRAME_SIZE, STREAM_QUALITY);

  xSemaphoreGive(captureMutex);

  return res;
}

// ================= HANDLER FLASH ON/OFF =================

static esp_err_t flashHandler(httpd_req_t* req) {

  if (!authorized(req)) {
    return denyReq(req);
  }

  char q[130];
  char state[16];

  if (
    httpd_req_get_url_query_str(req, q, sizeof(q)) != ESP_OK ||
    httpd_query_key_value(q, "state", state, sizeof(state)) != ESP_OK
  ) {
    httpd_resp_send_err(
      req,
      HTTPD_400_BAD_REQUEST,
      "parameter state diperlukan: on/off"
    );

    return ESP_FAIL;
  }

  bool targetState;

  if (strcmp(state, "on") == 0) {
    targetState = true;
  }
  else if (strcmp(state, "off") == 0) {
    targetState = false;
  }
  else {
    httpd_resp_send_err(
      req,
      HTTPD_400_BAD_REQUEST,
      "state harus on atau off"
    );

    return ESP_FAIL;
  }

  if (
    xSemaphoreTake(
      captureMutex,
      pdMS_TO_TICKS(5000)
    ) != pdTRUE
  ) {
    httpd_resp_send_err(
      req,
      HTTPD_500_INTERNAL_SERVER_ERROR,
      "camera busy"
    );

    return ESP_FAIL;
  }

  manualFlashOn = targetState;

  setFlashBrightness(
    manualFlashOn ? FLASH_BRIGHTNESS : 0
  );

  xSemaphoreGive(captureMutex);

  char response[100];

  snprintf(
    response,
    sizeof(response),
    "{\"success\":true,\"flash\":\"%s\",\"brightness\":%d}",
    manualFlashOn ? "on" : "off",
    manualFlashOn ? FLASH_BRIGHTNESS : 0
  );

  httpd_resp_set_type(req, "application/json");

  return httpd_resp_send(
    req,
    response,
    HTTPD_RESP_USE_STRLEN
  );
}

// ================= HANDLER STREAM =================

static esp_err_t streamHandler(httpd_req_t* req) {

  if (!authorized(req)) {
    return denyReq(req);
  }

  esp_err_t res = httpd_resp_set_type(req, STREAM_CT);

  if (res != ESP_OK) return res;

  httpd_resp_set_hdr(
    req,
    "Access-Control-Allow-Origin",
    "*"
  );

  const int64_t frameUs = 1000000LL / STREAM_MAX_FPS;

  char part[64];

  while (true) {

    int64_t t0 = esp_timer_get_time();

    camera_fb_t* fb = esp_camera_fb_get();

    if (!fb) {
      res = ESP_FAIL;
      break;
    }

    size_t hlen = snprintf(
      part,
      sizeof(part),
      STREAM_PART,
      (unsigned)fb->len
    );

    res = httpd_resp_send_chunk(
      req,
      STREAM_BOUNDARY,
      strlen(STREAM_BOUNDARY)
    );

    if (res == ESP_OK) {
      res = httpd_resp_send_chunk(req, part, hlen);
    }

    if (res == ESP_OK) {
      res = httpd_resp_send_chunk(
        req,
        (const char*)fb->buf,
        fb->len
      );
    }

    esp_camera_fb_return(fb);

    if (res != ESP_OK) break;

    int64_t used = esp_timer_get_time() - t0;

    if (used < frameUs) {
      delay((frameUs - used) / 1000);
    }
  }

  return res;
}

// ================= HANDLER STATUS =================

static esp_err_t statusHandler(httpd_req_t* req) {

  if (!authorized(req)) {
    return denyReq(req);
  }

  char buf[250];

  snprintf(
    buf,
    sizeof(buf),
    "{\"ip\":\"%s\",\"rssi\":%d,\"psram\":%s,\"free_heap\":%u,\"uptime_s\":%lu,\"flash\":\"%s\"}",
    WiFi.localIP().toString().c_str(),
    WiFi.RSSI(),
    psramFound() ? "true" : "false",
    (unsigned)ESP.getFreeHeap(),
    (unsigned long)(millis() / 1000),
    manualFlashOn ? "on" : "off"
  );

  httpd_resp_set_type(req, "application/json");

  return httpd_resp_send(
    req,
    buf,
    HTTPD_RESP_USE_STRLEN
  );
}

// ================= HANDLER INDEX =================

static esp_err_t indexHandler(httpd_req_t* req) {

  if (!authorized(req)) {
    return denyReq(req);
  }

  String html =
    "<!doctype html><html><head>"
    "<meta charset=utf-8>"
    "<meta name=viewport content='width=device-width,initial-scale=1'>"
    "<title>Kamera Gerbang</title>"
    "<style>"
    "body{font-family:sans-serif;background:#111;color:#eee;text-align:center}"
    "img{max-width:100%;border:2px solid #444}"
    "a{color:#6cf}"
    "</style></head><body>"
    "<h3>Kamera Gerbang Parkir</h3>"
    "<img id=v><br>"
    "<p>"
    "<a id=snap target=_blank>Foto OCR (tajam)</a> | "
    "<a id=snapf target=_blank>Foto + flash</a> | "
    "<a id=st target=_blank>Status</a>"
    "</p>"
    "<script>var k='" + String(CAM_KEY) + "';"
    "document.getElementById('v').src='http://'+location.hostname+':81/stream?key='+k;"
    "document.getElementById('snap').href='/capture?key='+k;"
    "document.getElementById('snapf').href='/capture?flash=1&key='+k;"
    "document.getElementById('st').href='/status?key='+k;"
    "</script></body></html>";

  httpd_resp_set_type(req, "text/html");

  return httpd_resp_send(
    req,
    html.c_str(),
    HTTPD_RESP_USE_STRLEN
  );
}

// ================= HTTP SERVER =================

void startServers() {

  httpd_config_t config = HTTPD_DEFAULT_CONFIG();

  config.server_port = 80;

  httpd_uri_t uriIndex = {
    "/", HTTP_GET, indexHandler, NULL
  };

  httpd_uri_t uriCapture = {
    "/capture", HTTP_GET, captureHandler, NULL
  };

  httpd_uri_t uriStatus = {
    "/status", HTTP_GET, statusHandler, NULL
  };

  httpd_uri_t uriFlash = {
    "/flash", HTTP_GET, flashHandler, NULL
  };

  if (httpd_start(&httpMain, &config) == ESP_OK) {

    httpd_register_uri_handler(httpMain, &uriIndex);
    httpd_register_uri_handler(httpMain, &uriCapture);
    httpd_register_uri_handler(httpMain, &uriStatus);
    httpd_register_uri_handler(httpMain, &uriFlash);

    Serial.println("HTTP server port 80 aktif.");
  }

  config.server_port = 81;
  config.ctrl_port += 1;

  httpd_uri_t uriStream = {
    "/stream", HTTP_GET, streamHandler, NULL
  };

  if (httpd_start(&httpStream, &config) == ESP_OK) {

    httpd_register_uri_handler(httpStream, &uriStream);

    Serial.println("Stream server port 81 aktif.");
  }
}

// ================= SENSOR SETTINGS =================

void applySensorSettings() {

  sensor_t* s = esp_camera_sensor_get();

  if (!s) return;

  s->set_vflip(s, FLIP_VERTICAL);
  s->set_hmirror(s, FLIP_HORIZONTAL);

  s->set_brightness(s, CAM_BRIGHTNESS);
  s->set_contrast(s, CAM_CONTRAST);
  s->set_saturation(s, CAM_SATURATION);

  s->set_whitebal(s, 1);
  s->set_awb_gain(s, 1);
  s->set_wb_mode(s, 0);

  s->set_exposure_ctrl(s, 1);
  s->set_aec2(s, 1);
  s->set_ae_level(s, CAM_AE_LEVEL);

  s->set_gain_ctrl(s, 1);
  s->set_gainceiling(s, CAM_GAIN_CEILING);

  s->set_bpc(s, 1);
  s->set_wpc(s, 1);
  s->set_raw_gma(s, 1);
  s->set_lenc(s, 1);
  s->set_dcw(s, 1);

  if (s->set_sharpness) {
    s->set_sharpness(s, CAM_SHARPNESS);
  }

  if (s->set_denoise) {
    s->set_denoise(s, 1);
  }
}

// ================= INIT CAMERA =================

bool initCamera() {

  camera_config_t config;

  memset(&config, 0, sizeof(config));

  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;

  config.pin_d0 = Y2_GPIO_NUM;
  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;
  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;
  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;
  config.pin_d7 = Y9_GPIO_NUM;

  config.pin_xclk = XCLK_GPIO_NUM;
  config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM;
  config.pin_href = HREF_GPIO_NUM;

  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;

  config.pin_pwdn = PWDN_GPIO_NUM;
  config.pin_reset = RESET_GPIO_NUM;

  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;
  config.jpeg_quality = CAPTURE_QUALITY;

  if (psramFound()) {

    config.frame_size = CAPTURE_FRAME_SIZE;
    config.fb_count = 2;
    config.fb_location = CAMERA_FB_IN_PSRAM;
    config.grab_mode = CAMERA_GRAB_LATEST;

  } else {

    config.frame_size = FRAMESIZE_VGA;
    config.fb_count = 1;
    config.fb_location = CAMERA_FB_IN_DRAM;
    config.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
  }

  esp_err_t err = esp_camera_init(&config);

  if (err != ESP_OK) {

    Serial.printf(
      "Init kamera GAGAL: 0x%x\n",
      err
    );

    return false;
  }

  applySensorSettings();

  setProfile(
    STREAM_FRAME_SIZE,
    STREAM_QUALITY
  );

  return true;
}

// ================= WIFI =================

bool connectWiFi() {

  WiFi.mode(WIFI_STA);

  WiFi.setSleep(false);
  WiFi.setTxPower(WIFI_POWER_19_5dBm);
  WiFi.setAutoReconnect(true);

#if USE_STATIC_IP
  if (!WiFi.config(STATIC_IP, GATEWAY, SUBNET, DNS1)) {
    Serial.println("Konfigurasi IP statis gagal!");
  }
#endif

  WiFi.begin(WIFI_SSID, WIFI_PASS);

  Serial.print("Menghubungkan WiFi");

  unsigned long t = millis();

  while (
    WiFi.status() != WL_CONNECTED &&
    millis() - t < 20000
  ) {

    delay(400);
    Serial.print(".");
  }

  Serial.println();

  return WiFi.status() == WL_CONNECTED;
}

// ================= SETUP =================

void setup() {

  Serial.begin(115200);

  Serial.println("\nSMART PARKING ESP32-CAM");

  captureMutex = xSemaphoreCreateMutex();

  if (!captureMutex) {
    Serial.println("Gagal membuat capture mutex!");
    ESP.restart();
  }

  if (!initFlashPWM()) {
    Serial.println("PWM flash gagal. Restart...");
    delay(3000);
    ESP.restart();
  }

  setFlashBrightness(0);

  if (!initCamera()) {

    Serial.println(
      "Cek daya 5V/1A & kabel kamera. Restart dalam 5 detik..."
    );

    delay(5000);
    ESP.restart();
  }

  if (!connectWiFi()) {

    Serial.println(
      "WiFi gagal. Restart dalam 5 detik..."
    );

    delay(5000);
    ESP.restart();
  }

  startServers();

  Serial.println("Kamera siap!");

  Serial.println(
    "IP : " + WiFi.localIP().toString()
  );

  Serial.println(
    "Pratinjau : http://" +
    WiFi.localIP().toString() +
    "/?key=" + String(CAM_KEY)
  );

  Serial.println(
    "Isi CAM_URL di parking_server.py dengan: http://" +
    WiFi.localIP().toString()
  );

  Serial.printf(
    "Kecerahan flash: %d / 255\n",
    FLASH_BRIGHTNESS
  );
}

// ================= LOOP =================

void loop() {

  static unsigned long lastOk = millis();

  if (WiFi.status() == WL_CONNECTED) {

    lastOk = millis();

  } else {

    WiFi.reconnect();

    if (millis() - lastOk > 60000) {
      ESP.restart();
    }
  }

  delay(5000);
}
