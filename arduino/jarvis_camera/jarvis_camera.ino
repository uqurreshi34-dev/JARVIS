// JARVIS camera: an ESP32-CAM (AI Thinker) that takes a picture only when asked.
//
// It reports to JARVIS over HTTPS, checking JARVIS's certificate against
// JARVIS's own certificate authority (setCACert) -- never setInsecure().
//
//   - "online" when it joins the wifi, and every 15 seconds after that, so
//     the house hologram draws its cone out of the window;
//   - every 3 seconds it asks whether a picture is wanted (POST /camera/wanted);
//   - only when one is does it take a picture and send it (POST /camera).
//
// Nothing is captured unless you ask: "show me the street". JARVIS keeps
// the newest picture in memory and never writes it to disk.
//
// Name it after its room, with "cam" on the end (SENSOR_NAME "my room cam"
// in jarvis_secrets.h), and the house plan places it by that name, looking
// out through that room's window.
//
// The camera module (OV2640 or OV3660, printed on its ribbon) is found by
// itself; either works with no change here.
//
// Before uploading:
//   1. python tools/esp32_setup.py        (writes jarvis_config.h and
//                                           jarvis_secrets.h here too)
//   2. Arduino IDE: esp32 board package 3.1 or later. Board "AI Thinker
//      ESP32-CAM". No library to add: the camera driver is part of the
//      board package. Or PlatformIO: pio run -e cam
//   3. Power it from 5V that can give half an amp: a weak supply makes it
//      restart the moment the wifi starts ("Brownout detector").

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <NetworkClientSecure.h>
#include "esp_camera.h"
#include <esp_task_wdt.h>

#include "jarvis_config.h"
#include "jarvis_secrets.h"

// AI Thinker ESP32-CAM: the camera's pins, fixed by the board.
#define PWDN_GPIO_NUM 32
#define RESET_GPIO_NUM -1
#define XCLK_GPIO_NUM 0
#define SIOD_GPIO_NUM 26
#define SIOC_GPIO_NUM 27
#define Y9_GPIO_NUM 35
#define Y8_GPIO_NUM 34
#define Y7_GPIO_NUM 39
#define Y6_GPIO_NUM 36
#define Y5_GPIO_NUM 21
#define Y4_GPIO_NUM 19
#define Y3_GPIO_NUM 18
#define Y2_GPIO_NUM 5
#define VSYNC_GPIO_NUM 25
#define HREF_GPIO_NUM 23
#define PCLK_GPIO_NUM 22

// The bright white LED beside the lens. Kept off: a camera looking out of
// a window at night would only see its own reflection.
#define FLASH_LED_PIN 4

static const unsigned long HEARTBEAT_EVERY_MS = 15UL * 1000UL;
static const unsigned long ASK_EVERY_MS = 3UL * 1000UL;
static const unsigned long WIFI_RETRY_MS = 10UL * 1000UL;

// How long a TLS handshake with JARVIS may take: well under a second, where
// the library's own limit is two minutes of silence.
static const unsigned long HANDSHAKE_SECONDS = 10;

// The board heals itself, as unplugging it does: nothing reaching JARVIS
// for this long and it restarts, saying why first. And should the loop
// ever hang, the chip's watchdog restarts it.
static const unsigned long RESTART_AFTER_MS = 3UL * 60UL * 1000UL;
static const uint32_t WATCHDOG_MS = 60UL * 1000UL;

// A wifi that has not come back in this long is started again from scratch;
// calling WiFi.begin() over a connection still being retried can wedge it.
static const unsigned long WIFI_RESTART_MS = 30UL * 1000UL;

NetworkClientSecure secure;

unsigned long lastHeartbeat = 0;
unsigned long lastAsk = 0;
unsigned long lastDelivered = 0;
unsigned long wifiBegun = 0;
unsigned long wifiLost = 0;
bool saidOnline = false;
bool cameraReady = false;

String quoted(const char *text) {
  return String("\"") + text + "\"";
}

// Post to JARVIS. Returns the HTTP status, or a negative HTTPClient error;
// [answer], when given, receives JARVIS's reply.
int post(const char *path, const char *type, const uint8_t *body, size_t length, String *answer) {
  HTTPClient https;
  String url = String("https://") + JARVIS_HOST + ":" + JARVIS_PORT + path;

  if (!https.begin(secure, url)) {
    Serial.println("[jarvis] could not start the request");
    return -1;
  }

  https.setTimeout(8000);
  https.addHeader("Content-Type", type);
  https.addHeader("X-Jarvis-Token", JARVIS_TOKEN);
  https.addHeader("X-Jarvis-Camera", SENSOR_NAME);

  int code = https.POST(const_cast<uint8_t *>(body), length);

  if (code == HTTP_CODE_OK) {
    lastDelivered = millis();

    if (answer) {
      *answer = https.getString();
    }
  } else if (code == 403) {
    Serial.println("[jarvis] refused: the token in jarvis_secrets.h is not JARVIS's");
  } else if (code < 0) {
    Serial.println("[jarvis] no connection: " + HTTPClient::errorToString(code) +
                   " (JARVIS running? same wifi? run tools/esp32_setup.py again?)");
  } else {
    Serial.println("[jarvis] JARVIS answered " + String(code));
  }

  https.end();
  return code;
}

int postJson(const char *path, const String &json, String *answer) {
  return post(path, "application/json", (const uint8_t *)json.c_str(), json.length(), answer);
}

int reportOnline() {
  String json = String("{\"name\":") + quoted(SENSOR_NAME) + ",\"event\":\"online\"}";
  String answer;
  int code = postJson("/sensor", json, &answer);

  if (code == HTTP_CODE_OK) {
    Serial.println("[jarvis] online -> " + answer);
  }

  return code;
}

bool pictureWanted() {
  String answer;
  String json = String("{\"name\":") + quoted(SENSOR_NAME) + "}";

  if (postJson("/camera/wanted", json, &answer) != HTTP_CODE_OK) {
    return false;
  }

  answer.replace(" ", "");
  return answer.indexOf("\"snap\":true") >= 0;
}

void sendPicture() {
  // The driver keeps the newest frame (CAMERA_GRAB_LATEST), so this is a
  // picture of now, not of whenever the last one was taken.
  camera_fb_t *frame = esp_camera_fb_get();

  if (!frame) {
    Serial.println("[jarvis] the camera gave no picture");
    return;
  }

  Serial.printf("[jarvis] sending a picture, %u bytes\n", (unsigned)frame->len);
  int code = post("/camera", "image/jpeg", frame->buf, frame->len, nullptr);
  esp_camera_fb_return(frame);

  if (code == HTTP_CODE_OK) {
    Serial.println("[jarvis] picture delivered");
  }
}

bool startCamera() {
  camera_config_t config = {};
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
  config.grab_mode = CAMERA_GRAB_LATEST;

  // VGA (640 x 480) from the board's own extra memory; a smaller picture if
  // that memory is missing, rather than no picture at all.
  if (psramFound()) {
    config.frame_size = FRAMESIZE_VGA;
    config.jpeg_quality = 12;
    config.fb_count = 2;
    config.fb_location = CAMERA_FB_IN_PSRAM;
  } else {
    config.frame_size = FRAMESIZE_QVGA;
    config.jpeg_quality = 14;
    config.fb_count = 1;
    config.fb_location = CAMERA_FB_IN_DRAM;
  }

  esp_err_t problem = esp_camera_init(&config);

  if (problem != ESP_OK) {
    Serial.printf("[jarvis] the camera did not start (0x%x): is its ribbon pushed in and latched?\n", problem);
    return false;
  }

  sensor_t *sensor = esp_camera_sensor_get();

  if (sensor && sensor->id.PID == OV3660_PID) {
    // The OV3660 comes up upside down and washed out on this board.
    sensor->set_vflip(sensor, 1);
    sensor->set_brightness(sensor, 1);
    sensor->set_saturation(sensor, -2);
    Serial.println("[jarvis] camera: OV3660");
  } else if (sensor && sensor->id.PID == OV2640_PID) {
    Serial.println("[jarvis] camera: OV2640");
  } else if (sensor) {
    Serial.printf("[jarvis] camera: sensor 0x%x\n", sensor->id.PID);
  }

  return true;
}

bool joinWifi() {
  if (WiFi.status() == WL_CONNECTED) {
    wifiLost = 0;
    return true;
  }

  if (wifiLost == 0) {
    wifiLost = millis();
  }

  // Begun once and left to reconnect by itself; begun again from scratch
  // only once that has had WIFI_RESTART_MS and failed.
  bool fresh = wifiBegun == 0 ||
               (millis() - wifiBegun >= WIFI_RESTART_MS && millis() - wifiLost >= WIFI_RESTART_MS);

  if (!fresh) {
    unsigned long waited = millis();

    while (WiFi.status() != WL_CONNECTED && millis() - waited < WIFI_RETRY_MS) {
      delay(250);
    }

    return WiFi.status() == WL_CONNECTED;
  }

  Serial.print("[jarvis] joining wifi ");
  Serial.println(WIFI_SSID);

  if (wifiBegun != 0) {
    WiFi.disconnect(true);
    delay(200);
  }

  wifiBegun = millis();
  wifiLost = millis();
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  unsigned long started = millis();

  while (WiFi.status() != WL_CONNECTED && millis() - started < WIFI_RETRY_MS) {
    delay(250);
  }

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[jarvis] wifi not joined yet; trying again shortly");
    return false;
  }

  Serial.print("[jarvis] on the wifi as ");
  Serial.println(WiFi.localIP().toString());
  return true;
}

void setup() {
  Serial.begin(115200);
  delay(200);

  pinMode(FLASH_LED_PIN, OUTPUT);
  digitalWrite(FLASH_LED_PIN, LOW);

  Serial.print("[jarvis] ESP32-CAM as ");
  Serial.println(SENSOR_NAME);

  cameraReady = startCamera();

  // JARVIS's own authority: the board accepts JARVIS and nothing else.
  secure.setCACert(JARVIS_CA);
  secure.setHandshakeTimeout(HANDSHAKE_SECONDS);

  esp_task_wdt_config_t watchdog = {};
  watchdog.timeout_ms = WATCHDOG_MS;
  watchdog.idle_core_mask = 0;
  watchdog.trigger_panic = true;
  if (esp_task_wdt_reconfigure(&watchdog) == ESP_ERR_INVALID_STATE) {
    esp_task_wdt_init(&watchdog);   // a build with the watchdog off: start it
  }

  enableLoopWDT();

  lastDelivered = millis();
  WiFi.setAutoReconnect(true);
  joinWifi();
}

void loop() {
  if (millis() - lastDelivered >= RESTART_AFTER_MS) {
    Serial.printf("[jarvis] nothing has reached JARVIS for %lu minutes: restarting, as unplugging would\n",
                  RESTART_AFTER_MS / 60000UL);
    delay(200);
    ESP.restart();
  }

  if (!joinWifi()) {
    delay(1000);
    return;
  }

  unsigned long now = millis();

  // Online only with a working camera: a camera that cannot see should
  // not be drawn on the plan as if it could.
  if (cameraReady && (!saidOnline || now - lastHeartbeat >= HEARTBEAT_EVERY_MS)) {
    if (reportOnline() == HTTP_CODE_OK) {
      saidOnline = true;
    }

    lastHeartbeat = now;
  }

  if (cameraReady && saidOnline && now - lastAsk >= ASK_EVERY_MS) {
    lastAsk = now;

    if (pictureWanted()) {
      sendPicture();
    }
  }

  if (!cameraReady) {
    // Say why every so often, rather than sitting silent.
    static unsigned long lastComplaint = 0;

    if (now - lastComplaint >= 10000UL) {
      Serial.println("[jarvis] no camera: check the ribbon, then press reset");
      lastComplaint = now;
    }
  }

  delay(50);
}
