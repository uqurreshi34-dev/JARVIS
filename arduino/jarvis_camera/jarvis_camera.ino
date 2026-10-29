// JARVIS camera: an ESP32-CAM (AI Thinker), an ESP32-S3-CAM, or DFRobot's
// ESP32-S3 AI Camera (DFR1154), that takes a picture only when asked. The same
// sketch for all three: the S3's camera pins and name are chosen below by the
// chip it is built for (PlatformIO env "s3cam"), and DFRobot's by its own env
// ("aicam").
//
// It reports to JARVIS over HTTPS, checking JARVIS's certificate against
// JARVIS's own certificate authority (setCACert) -- never setInsecure().
//
//   - "online" when it joins the wifi, and every 15 seconds after that, so
//     the house hologram draws its cone out of the window;
//   - every second it asks whether a picture is wanted (POST /camera/wanted),
//     which also tells JARVIS it is there;
//   - only when one is does it take a picture and send it (POST /camera);
//   - while its picture is on screen, JARVIS asks for live view: it opens
//     one encrypted connection (POST /camera/live) and sends frame after
//     frame down it until JARVIS ends the view. One TLS handshake for the
//     whole view, so it runs at the camera's pace rather than one picture
//     a second, and nothing is broadcast on the wifi.
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
#include <esp_log.h>
#include <esp_task_wdt.h>

#include "jarvis_config.h"
#include "jarvis_secrets.h"

#if defined(JARVIS_BOARD_DFR1154)
#include "ESP_I2S.h"
#include "esp_heap_caps.h"
#endif

#ifdef JARVIS_DETECT
// On-board detection (PlatformIO env "s3detect"): YOLO26n, compiled to C by
// FEMTO-ML into detect/ by tools/femto_build.py. It runs on the other core,
// beside everything above: pictures and live view carry on as before.
#include "img_converters.h"
#include "jpeg_decoder.h"
#include "esp_heap_caps.h"
#include "model.h"
#include "labels.h"

// The face finder: YuNet (OpenCV's, MIT), compiled by FEMTO-ML with its names prefixed face_
// into faces/ by tools/femto_build.py, beside YOLO26n. Without faces/, none of it is built.
#if __has_include("face_model.h")
#include "face_model.h"
#define JARVIS_FACES 1
#endif
#endif

// One thing detection found. Up here, and whatever the build, as Wanted is:
// the Arduino build declares every function above the sketch's own code --
// those inside #ifdef JARVIS_DETECT too -- and some of them take one.
struct Seen {
  int label;
  float score;
  float x1, y1, x2, y2;
};

// The S3 board is a second camera, with its own name: jarvis_secrets.h names
// the first ("my room cam"), and this one is called by what it watches. Say
// "show me the house cam", or "show me all the cameras" for both at once.
#ifdef JARVIS_CAMERA_NAME_HOUSE
#undef SENSOR_NAME
#define SENSOR_NAME "house cam"
#endif

// DFRobot's ESP32-S3 AI Camera is a third, the toy's eyes: "show me the toy cam".
#ifdef JARVIS_CAMERA_NAME_TOY
#undef SENSOR_NAME
#define SENSOR_NAME "toy cam"
#endif

#if defined(JARVIS_BOARD_DFR1154)
// DFRobot ESP32-S3 AI Camera (DFR1154, V1.1): the camera's pins, as DFRobot's own examples
// (DFR1154_Examples, CameraWebServer) wire them -- not the S3-EYE's below. No power-down or reset pin.
// Its infrared lights (47) and LED (3) are left off here: a camera looking out of a window at
// night would only see its own reflection. Its microphone and speaker are the toy's talk (startTalk,
// below); its Gravity port (43, 44) is not used by this sketch.
#define PWDN_GPIO_NUM -1
#define RESET_GPIO_NUM -1
#define XCLK_GPIO_NUM 5
#define SIOD_GPIO_NUM 8
#define SIOC_GPIO_NUM 9
#define Y9_GPIO_NUM 4
#define Y8_GPIO_NUM 6
#define Y7_GPIO_NUM 7
#define Y6_GPIO_NUM 14
#define Y5_GPIO_NUM 17
#define Y4_GPIO_NUM 21
#define Y3_GPIO_NUM 18
#define Y2_GPIO_NUM 16
#define VSYNC_GPIO_NUM 1
#define HREF_GPIO_NUM 2
#define PCLK_GPIO_NUM 15
#define FLASH_LED_PIN -1
#define DFR1154_IR_PIN 47
#elif defined(CONFIG_IDF_TARGET_ESP32S3)
// ESP32-S3-CAM (Goouuu, ESP32-S3-N16R8): the camera's pins, as Espressif's
// ESP32-S3-EYE and the boards that copy it wire them. No power-down or reset
// pin, and no white flash LED (its RGB light, on 48, is left alone).
#define PWDN_GPIO_NUM -1
#define RESET_GPIO_NUM -1
#define XCLK_GPIO_NUM 15
#define SIOD_GPIO_NUM 4
#define SIOC_GPIO_NUM 5
#define Y9_GPIO_NUM 16
#define Y8_GPIO_NUM 17
#define Y7_GPIO_NUM 18
#define Y6_GPIO_NUM 12
#define Y5_GPIO_NUM 10
#define Y4_GPIO_NUM 8
#define Y3_GPIO_NUM 9
#define Y2_GPIO_NUM 11
#define VSYNC_GPIO_NUM 6
#define HREF_GPIO_NUM 7
#define PCLK_GPIO_NUM 13
#define FLASH_LED_PIN -1
#else
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
#endif

static const unsigned long HEARTBEAT_EVERY_MS = 15UL * 1000UL;
// Asking every second: a picture starts within a second of "show me", and JARVIS
// counts the camera as connected the moment it asks.
static const unsigned long ASK_EVERY_MS = 1UL * 1000UL;
// Each ask may wait at JARVIS this long for something to be wanted, answered
// the moment it is: every connection is a TLS handshake, and one a second took
// about 0.4 s of every detection look from the core it shares (20 October
// 2026). A JARVIS from before then answers at once, as it always did.
static const unsigned long ASK_WAIT_SECONDS = 10UL;
static const unsigned long POST_TIMEOUT_MS = 8000UL;
static const unsigned long WIFI_RETRY_MS = 10UL * 1000UL;

// Live view carries on past a frame the camera fails to give -- moved by
// hand it drops some -- and stops only after this many in a row. Each
// wait is the driver's own (up to four seconds), inside JARVIS's limit for
// a silent stream (cameras.LIVE_READ_TIMEOUT).
static const int LIVE_MISSES_ALLOWED = 2;

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

// What JARVIS wants of the camera: one picture, or its live view. Up here
// with the globals: the Arduino build declares every function above the
// sketch's own code, and pictureWanted() returns one.
struct Wanted {
  bool snap;
  bool live;
  bool look;   // JARVIS has been asked what this camera sees: report the next look as it is
  bool still;  // a picture larger than live view's, for a sighting (takeStill)
};

String quoted(const char *text) {
  return String("\"") + text + "\"";
}

// Post to JARVIS. Returns the HTTP status, or a negative HTTPClient error;
// [answer], when given, receives JARVIS's reply.
int post(const char *path, const char *type, const uint8_t *body, size_t length, String *answer,
         unsigned long timeoutMs) {
  HTTPClient https;
  String url = String("https://") + JARVIS_HOST + ":" + JARVIS_PORT + path;

  if (!https.begin(secure, url)) {
    Serial.println("[jarvis] could not start the request");
    return -1;
  }

  https.setTimeout(timeoutMs);
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

int postJson(const char *path, const String &json, String *answer, unsigned long timeoutMs) {
  return post(path, "application/json", (const uint8_t *)json.c_str(), json.length(), answer, timeoutMs);
}

int reportOnline() {
  String json = String("{\"name\":") + quoted(SENSOR_NAME) + ",\"event\":\"online\"}";
  String answer;
  int code = postJson("/sensor", json, &answer, POST_TIMEOUT_MS);

  if (code == HTTP_CODE_OK) {
    Serial.println("[jarvis] online -> " + answer);
  }

  return code;
}

Wanted pictureWanted() {
  String answer;
  String json = String("{\"name\":") + quoted(SENSOR_NAME) + ",\"wait\":" + String(ASK_WAIT_SECONDS) + "}";

  if (postJson("/camera/wanted", json, &answer, POST_TIMEOUT_MS + ASK_WAIT_SECONDS * 1000UL) != HTTP_CODE_OK) {
    return {false, false, false, false};
  }

  answer.replace(" ", "");
  return {answer.indexOf("\"snap\":true") >= 0, answer.indexOf("\"live\":true") >= 0,
          answer.indexOf("\"look\":true") >= 0, answer.indexOf("\"still\":true") >= 0};
}

// Live view's own connection, kept apart from the one the polls use.
NetworkClientSecure liveLink;

#ifdef JARVIS_DETECT
// Each look's boxes, for the live view: the detection task (core 0) leaves
// them here, and streamLive (core 1) sends them down the stream it already
// has open, between two frames -- so JARVIS can draw them over the picture
// ("highlight the bottle") without another connection, and so without the
// cost of another TLS handshake. Marked apart from a frame by the top bit of
// its length: {"objects": [what the board is sure of], "now": [this look]}.
static const uint32_t LIVE_MARKS_FLAG = 0x80000000UL;
static char liveMarks[2048];
static size_t liveMarksLength = 0;
static uint32_t liveMarksSeq = 0;
static portMUX_TYPE liveMarksLock = portMUX_INITIALIZER_UNLOCKED;
#endif

// Writes all of [length] bytes, in pieces the TLS layer takes; false if the
// connection went.
bool writeAll(NetworkClientSecure &link, const uint8_t *data, size_t length) {
  while (length > 0) {
    size_t sent = link.write(data, length);

    if (sent == 0) {
      return false;
    }

    data += sent;
    length -= sent;
  }

  return true;
}

// Live view: one POST whose body never ends until JARVIS ends it, each frame
// sent as one chunk -- its length in four bytes, then the JPEG. JARVIS
// answers, and closes the connection, when the picture is put away; a write
// that fails, or an answer waiting, ends it here.
void streamLive() {
  if (!liveLink.connect(JARVIS_HOST, JARVIS_PORT)) {
    Serial.println("[jarvis] live view: could not connect");
    return;
  }

  liveLink.print(String("POST /camera/live HTTP/1.1\r\n") +
                 "Host: " + JARVIS_HOST + ":" + JARVIS_PORT + "\r\n" +
                 "X-Jarvis-Token: " + JARVIS_TOKEN + "\r\n" +
                 "X-Jarvis-Camera: " + SENSOR_NAME + "\r\n" +
                 "Content-Type: application/octet-stream\r\n" +
                 "Transfer-Encoding: chunked\r\n" +
                 "Connection: close\r\n\r\n");

  unsigned long started = millis();
  unsigned long counted = started;
  unsigned long frames = 0;
  unsigned long sinceCount = 0;
  int missed = 0;
#ifdef JARVIS_DETECT
  uint32_t marksSent = liveMarksSeq;   // only looks taken from now on
  static char marks[sizeof(liveMarks)];
#endif

  while (liveLink.connected() && !liveLink.available()) {
    camera_fb_t *frame = esp_camera_fb_get();

    if (!frame) {
      missed++;
      Serial.printf("[jarvis] live view: the camera gave no picture (%d in a row)\n", missed);
      feedLoopWDT();

      if (missed > LIVE_MISSES_ALLOWED) {
        break;
      }

      continue;
    }

    missed = 0;

    uint8_t header[4] = {
      (uint8_t)(frame->len >> 24), (uint8_t)(frame->len >> 16),
      (uint8_t)(frame->len >> 8), (uint8_t)frame->len,
    };
    char size[12];
    snprintf(size, sizeof(size), "%X\r\n", (unsigned)(frame->len + sizeof(header)));

    bool sent = writeAll(liveLink, (const uint8_t *)size, strlen(size)) &&
                writeAll(liveLink, header, sizeof(header)) &&
                writeAll(liveLink, frame->buf, frame->len) &&
                writeAll(liveLink, (const uint8_t *)"\r\n", 2);
    esp_camera_fb_return(frame);

    if (!sent) {
      break;
    }

#ifdef JARVIS_DETECT
    // A new look's boxes, if one has finished since the last frame: one more
    // chunk, its length with the top bit set.
    size_t marksLength = 0;

    portENTER_CRITICAL(&liveMarksLock);
    if (liveMarksSeq != marksSent) {
      marksSent = liveMarksSeq;
      marksLength = liveMarksLength;
      memcpy(marks, liveMarks, marksLength);
    }
    portEXIT_CRITICAL(&liveMarksLock);

    if (marksLength) {
      uint32_t flagged = (uint32_t)marksLength | LIVE_MARKS_FLAG;
      uint8_t marksHeader[4] = {
        (uint8_t)(flagged >> 24), (uint8_t)(flagged >> 16), (uint8_t)(flagged >> 8), (uint8_t)flagged,
      };
      char marksSize[12];
      snprintf(marksSize, sizeof(marksSize), "%X\r\n", (unsigned)(marksLength + sizeof(marksHeader)));

      if (!(writeAll(liveLink, (const uint8_t *)marksSize, strlen(marksSize)) &&
            writeAll(liveLink, marksHeader, sizeof(marksHeader)) &&
            writeAll(liveLink, (const uint8_t *)marks, marksLength) &&
            writeAll(liveLink, (const uint8_t *)"\r\n", 2))) {
        break;
      }
    }
#endif

    frames++;
    sinceCount++;
    lastDelivered = millis();

    // The loop's watchdog is fed while streaming; JARVIS counts the frames
    // as this camera's "online", so no heartbeat is needed meanwhile.
    feedLoopWDT();

    if (millis() - counted >= 5000UL) {
      Serial.printf("[jarvis] live view: %.1f frames a second\n", sinceCount * 1000.0 / (millis() - counted));
      counted = millis();
      sinceCount = 0;
    }
  }

  writeAll(liveLink, (const uint8_t *)"0\r\n\r\n", 5);
  liveLink.stop();
  Serial.printf("[jarvis] live view ended: %lu frames in %lu s\n", frames, (millis() - started) / 1000UL);
}

// A still, larger than live view's VGA, when JARVIS asks (someone has come into view: actions/sightings.py).
// The camera's picture buffers are sized when it starts, width x height / 5 each, so it starts at the
// largest of these whose buffers leave STILL_PSRAM_SPARE of PSRAM free -- after the detection model has
// taken its own (reserveDetection) -- and is set back to VGA at once. None fits: no stills, VGA as before.
// The ESP32-S3 boards only; the ESP32-CAM's 4 MB is left as it was.
#if defined(CONFIG_IDF_TARGET_ESP32S3)
static const framesize_t STILL_SIZES[] = {FRAMESIZE_QXGA, FRAMESIZE_UXGA, FRAMESIZE_SXGA};
static const size_t STILL_PSRAM_SPARE = 512UL * 1024UL;
#endif
static const int LIVE_QUALITY = 12;         // JPEG quality for live view and pictures (lower is better)
static const int STILL_QUALITY = 10;        // and for a still
static const int STILL_SETTLE_FRAMES = 2;   // frames at the still's size let go while the sensor settles
static const unsigned long STILL_SETTLE_MS = 3000UL;   // and the longest a still may take

// A JPEG's width, from its frame header (SOF0 to SOF2), or 0.
int jpegWidth(const uint8_t *data, size_t length) {
  size_t i = 2;

  while (i + 9 < length) {
    if (data[i] != 0xFF) {
      i++;
      continue;
    }

    uint8_t marker = data[i + 1];

    if (marker == 0xC0 || marker == 0xC1 || marker == 0xC2) {
      return (data[i + 7] << 8) | data[i + 8];
    }

    if (marker == 0xFF || marker == 0x01 || (marker >= 0xD0 && marker <= 0xD8)) {
      i += (marker == 0xFF) ? 1 : 2;
      continue;
    }

    i += 2 + ((data[i + 2] << 8) | data[i + 3]);
  }

  return 0;
}
framesize_t stillSize = FRAMESIZE_INVALID;

// A still at stillSize for a sighting: the sensor set to it, a frame or two let go while it settles,
// one sent to POST /camera/still, and the sensor set back to VGA. Detection meanwhile skips the large
// frames it is given (too big for its buffer), as live view is not running: the loop is here.
void takeStill() {
  if (stillSize == FRAMESIZE_INVALID) {
    return;
  }

  sensor_t *sensor = esp_camera_sensor_get();
  unsigned long started = millis();

  if (!sensor || sensor->set_framesize(sensor, stillSize) != 0) {
    Serial.println("[jarvis] still: the camera would not change size");
    return;
  }

  sensor->set_quality(sensor, STILL_QUALITY);

  // Frames let go until STILL_SETTLE_FRAMES at the still's own size have gone by, then the next at that
  // size is the still. Letting any two go was not enough: they were frames already waiting at the old
  // size, and the still came in 143 ms, the first at the new size, half drawn (22 October 2026). Each
  // JPEG says its own size (jpegWidth), so an old one is told from a new one.
  int wanted = resolution[stillSize].width;
  int atSize = 0, letGo = 0;
  camera_fb_t *frame = nullptr;

  while (millis() - started < STILL_SETTLE_MS) {
    camera_fb_t *candidate = esp_camera_fb_get();

    if (!candidate) {
      continue;
    }

    if (jpegWidth(candidate->buf, candidate->len) == wanted && atSize++ >= STILL_SETTLE_FRAMES) {
      frame = candidate;
      break;
    }

    esp_camera_fb_return(candidate);
    letGo++;
  }

  if (frame) {
    Serial.printf("[jarvis] still: %u x %u, %u bytes in %lu ms (%d frames let go)\n", (unsigned)frame->width,
                  (unsigned)frame->height, (unsigned)frame->len, millis() - started, letGo);
    int code = post("/camera/still", "image/jpeg", frame->buf, frame->len, nullptr, POST_TIMEOUT_MS * 2);
    esp_camera_fb_return(frame);

    if (code == HTTP_CODE_OK) {
      lastDelivered = millis();
    }
  } else {
    Serial.printf("[jarvis] still: none at the still's size in %lu ms (%d frames let go)\n", STILL_SETTLE_MS, letGo);
  }

  sensor->set_framesize(sensor, FRAMESIZE_VGA);
  sensor->set_quality(sensor, LIVE_QUALITY);
  camera_fb_t *old = esp_camera_fb_get();

  if (old) {
    esp_camera_fb_return(old);
  }
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
  int code = post("/camera", "image/jpeg", frame->buf, frame->len, nullptr, POST_TIMEOUT_MS);
  esp_camera_fb_return(frame);

  if (code == HTTP_CODE_OK) {
    Serial.println("[jarvis] picture delivered");
  }
}

// The OV3660's highest gain, in sixteenths: 0x1F0 is 31x (see startCamera).
static const int OV3660_GAIN_CEILING = 0x1F0;
// The OV2640's highest gain (see startCamera); the camera driver sets 2x.
static const gainceiling_t OV2640_GAIN_CEILING = GAINCEILING_16X;


bool startCamera() {
  // The camera driver's own task warns, through printf, whenever a frame
  // arrives broken or there is no buffer free for it -- and printf needs
  // more stack than that task has. On 12 October 2026 it overflowed
  // (Guru Meditation, "Stack canary watchpoint triggered (cam_task)"),
  // decoded as cam_task -> esp_log -> vfprintf -> malloc, and the board
  // restarted in a loop. The frame is dropped by the driver either way;
  // only the message is silenced, for that task's tag alone.
  esp_log_level_set("cam_hal", ESP_LOG_NONE);

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
    config.jpeg_quality = LIVE_QUALITY;
#ifdef JARVIS_DETECT
    // One frame more: detection holds one while it decodes, and live view
    // still has two to swap between.
    config.fb_count = 3;
#else
    config.fb_count = 2;
#endif
    config.fb_location = CAMERA_FB_IN_PSRAM;
  } else {
    config.frame_size = FRAMESIZE_QVGA;
    config.jpeg_quality = 14;
    config.fb_count = 1;
    config.fb_location = CAMERA_FB_IN_DRAM;
  }

  esp_err_t problem = ESP_FAIL;

#if defined(CONFIG_IDF_TARGET_ESP32S3)
  if (psramFound()) {
    for (framesize_t size : STILL_SIZES) {
      config.frame_size = size;

      if (esp_camera_init(&config) != ESP_OK) {
        continue;
      }

      if (ESP.getFreePsram() >= STILL_PSRAM_SPARE) {
        // The size the camera really took: asked for more than its sensor's largest, the driver quietly
        // gives it its largest (the house cam's OV2640: 1600 x 1200, though asked for 2048 x 1536, 22
        // October 2026), and a still asked of it at the larger would be no picture at all.
        sensor_t *sized = esp_camera_sensor_get();
        framesize_t taken = sized->status.framesize;
        sized->set_framesize(sized, FRAMESIZE_VGA);
        problem = ESP_OK;

        if (taken > FRAMESIZE_VGA && taken < FRAMESIZE_INVALID) {
          stillSize = taken;
          Serial.printf("[jarvis] stills: %d x %d; %u bytes of PSRAM left\n", resolution[taken].width,
                        resolution[taken].height, (unsigned)ESP.getFreePsram());
        }

        break;
      }

      esp_camera_deinit();
    }

    config.frame_size = FRAMESIZE_VGA;
  }

  if (problem != ESP_OK)
#endif
  problem = esp_camera_init(&config);

  if (problem != ESP_OK) {
    Serial.printf("[jarvis] the camera did not start (0x%x): is its ribbon pushed in and latched?\n", problem);
#if defined(JARVIS_BOARD_DFR1154)
    Serial.println("[jarvis] (DFRobot AI Camera: is it the V1.1 board, flashed as aicam?)");
#elif defined(CONFIG_IDF_TARGET_ESP32S3)
    Serial.println("[jarvis] (ESP32-S3: if the ribbon is in, this board's camera pins differ from the S3-EYE's)");
#endif
    return false;
  }

  sensor_t *sensor = esp_camera_sensor_get();

  if (sensor && sensor->id.PID == OV3660_PID) {
    // The OV3660 comes up upside down and washed out on this board.
    sensor->set_vflip(sensor, 1);
    sensor->set_brightness(sensor, 1);
    sensor->set_saturation(sensor, -2);
    // Dark indoors: in a lamp-lit room the house cam's pictures averaged 15 to 23 of 255, most of each
    // under 20 (21 October 2026) -- the house cam turned out to be an OV2640 (below), but an OV3660 is
    // as short of the 40 to 48 its own exposure aims for in dim light, held back by an
    // exposure no longer than one frame and a gain of at most 15.5x. Night mode lets the exposure run
    // past a frame when it must (fewer frames a second in the dark, none fewer in daylight), and the
    // gain may go to 31x (0x1F0, in sixteenths; the driver's default 0xF8). A board without them
    // ignores both.
    sensor->set_aec2(sensor, 1);
    sensor->set_gainceiling(sensor, (gainceiling_t)OV3660_GAIN_CEILING);
    Serial.println("[jarvis] camera: OV3660");
  } else if (sensor && sensor->id.PID == OV2640_PID) {
    // Dark indoors, as the OV3660: the house cam is an OV2640 (22 October 2026), whose pictures in a
    // lamp-lit room averaged 15 to 23 of 255 -- and the camera driver caps an OV2640's gain at 2x as it
    // starts. Its gain may go to 16x, about the OV3660's own default, and its exposure is measured by its
    // DSP (aec2), which holds up better in dim light. In daylight its exposure keeps the gain low.
    sensor->set_aec2(sensor, 1);
    sensor->set_gainceiling(sensor, OV2640_GAIN_CEILING);
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
  // The radio kept awake and at full strength: left to doze between messages (the ESP32's default), the
  // ESP32-CAM missed about half of what reached it (20 October 2026: half its pings lost, the rest up to
  // 1.3 s late) and its connections to JARVIS timed out. A camera on USB has power to spare.
  WiFi.setSleep(false);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  WiFi.setTxPower(WIFI_POWER_19_5dBm);

  unsigned long started = millis();

  while (WiFi.status() != WL_CONNECTED && millis() - started < WIFI_RETRY_MS) {
    delay(250);
  }

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[jarvis] wifi not joined yet; trying again shortly");
    return false;
  }

  Serial.print("[jarvis] on the wifi as ");
  Serial.print(WiFi.localIP().toString());
  Serial.printf(", signal %d dBm (-70 or above is good, below -80 is poor)\n", (int)WiFi.RSSI());
  return true;
}

#ifdef JARVIS_DETECT
// ---- detection --------------------------------------------------------------
//
// Every few seconds: the newest frame, decoded and letterboxed to the model's
// 256 x 256 as Ultralytics trains it (grey bars, the aspect kept), run through
// the model, and what it found above its score sent to JARVIS (POST
// /camera/seen) -- when it changes, and every DETECT_REPEAT_MS while anything
// is in view. Names and scores and where each is (0 to 1 across the picture),
// never the picture: JARVIS decides what is worth saying.

// How sure the model must be. Someone is said aloud, so a person must be
// clear; small things -- a cup in a hand -- score lower at 256 x 256, and
// at 0.40 a cup held up to the camera was never reported (19 October
// 2026). 0.25 is FEMTO-ML's own example's threshold. Scores above 0.5 are
// real only from FEMTO-ML's class-logit fix (fuse.py, October 2026): before
// it every confident detection read 0.500, so these thresholds, both below
// 0.5, pass exactly what they passed then.
static const float DETECT_SCORE_PERSON = 0.40f;
static const float DETECT_SCORE_OTHER = 0.25f;
static const unsigned long DETECT_GAP_MS = 200UL;        // rest between runs (was 1000: a second of every look)
static const unsigned long DETECT_REPEAT_MS = 15000UL;
static const int DETECT_MAX = 12;
static const int MODEL_SIZE = 256;

extern "C" {
int activations_init(void);
void run_model(void);
}

NetworkClientSecure detectLink;
bool detectReady = false;

// Set by the loop (core 1) when JARVIS asks what this camera sees; the next
// look is reported as that look saw it, whatever it is sure of yet.
volatile bool lookWanted = false;

// Which picture column (or row) each of the model's columns (rows) takes, or
// -1 for the grey bars: the same sums as ever, done once for each of the 256
// rather than for every pixel.
static void letterboxMap(int16_t *map, int length, int shown, int pad, float scale) {
  for (int i = 0; i < MODEL_SIZE; ++i) {
    int source = (int)((i - pad) / scale);
    map[i] = (i < pad || i >= pad + shown || source < 0 || source >= length) ? -1 : (int16_t)source;
  }
}

// The frame as the model wants it, letterboxed: [pixels] is three bytes a
// pixel, red first, or blue first with [bgr].
//
// With FEMTO-ML's int8 input (MODEL_INPUT_INT8): straight into the tensor the
// first conv reads, int8 and pixel by pixel (one byte a value, where the float
// input was four, then quantized and transposed by the model), each byte what
// the model's own QuantizeLinear made of RGB / 255 -- so the same input.
bool fillInput(const uint8_t *pixels, int width, int height, bool bgr) {
  float scale = fminf((float)MODEL_SIZE / width, (float)MODEL_SIZE / height);
  int shown_w = (int)(width * scale), shown_h = (int)(height * scale);
  int pad_x = (MODEL_SIZE - shown_w) / 2, pad_y = (MODEL_SIZE - shown_h) / 2;
  const float grey = 114.0f / 255.0f;
  static int16_t column[MODEL_SIZE], row[MODEL_SIZE];
  static float level[256];
  const int r = bgr ? 2 : 0, b = bgr ? 0 : 2;

  letterboxMap(column, width, shown_w, pad_x, scale);
  letterboxMap(row, height, shown_h, pad_y, scale);

  for (int v = 0; v < 256; ++v) {
    level[v] = v / 255.0f;
  }

#ifdef MODEL_INPUT_INT8
  static int8_t quantized[256];
  const int8_t grey_q = model_quantize_input(grey);

  for (int v = 0; v < 256; ++v) {
    quantized[v] = model_quantize_input(level[v]);
  }

  int8_t *in = model_input_int8;

  for (int y = 0; y < MODEL_SIZE; ++y) {
    for (int x = 0; x < MODEL_SIZE; ++x, in += 3) {
      if (row[y] < 0 || column[x] < 0) {
        in[0] = in[1] = in[2] = grey_q;
        continue;
      }

      const uint8_t *px = pixels + (row[y] * width + column[x]) * 3;
      in[0] = quantized[px[r]];
      in[1] = quantized[px[1]];
      in[2] = quantized[px[b]];
    }
  }

  return true;
#endif

  for (int y = 0; y < MODEL_SIZE; ++y) {
    for (int x = 0; x < MODEL_SIZE; ++x) {
      if (row[y] < 0 || column[x] < 0) {
        tensor_images[0][y][x] = tensor_images[1][y][x] = tensor_images[2][y][x] = grey;
        continue;
      }

      const uint8_t *px = pixels + (row[y] * width + column[x]) * 3;
      tensor_images[0][y][x] = level[px[r]];
      tensor_images[1][y][x] = level[px[1]];
      tensor_images[2][y][x] = level[px[b]];
    }
  }

  return true;
}

// How much two boxes overlap: the shared area over the area of either.
static float overlap(const Seen &a, const Seen &b) {
  float w = fminf(a.x2, b.x2) - fmaxf(a.x1, b.x1), h = fminf(a.y2, b.y2) - fmaxf(a.y1, b.y1);

  if (w <= 0 || h <= 0) {
    return 0;
  }

  float shared = w * h;
  float either = (a.x2 - a.x1) * (a.y2 - a.y1) + (b.x2 - b.x1) * (b.y2 - b.y1) - shared;
  return either > 0 ? shared / either : 0;
}

// How much of the smaller box lies inside the other.
static float inside(const Seen &a, const Seen &b) {
  float w = fminf(a.x2, b.x2) - fmaxf(a.x1, b.x1), h = fminf(a.y2, b.y2) - fmaxf(a.y1, b.y1);

  if (w <= 0 || h <= 0) {
    return 0;
  }

  float smaller = fminf((a.x2 - a.x1) * (a.y2 - a.y1), (b.x2 - b.x1) * (b.y2 - b.y1));
  return smaller > 0 ? w * h / smaller : 0;
}

// One name, and one box, for one thing: the model can call the same box two
// things -- an open laptop also "tv" -- or give one thing two boxes, one in
// the other -- "two laptops" for one (19 October 2026) -- and only the surer
// is kept. Two different things are never a person: someone on a couch is a
// person and a couch, both there.
static const float DETECT_SAME_BOX = 0.50f;   // 0.80 let a laptop and a "tv" through (19 October)
static const float DETECT_BOX_INSIDE = 0.80f;

int oneNameEach(Seen *seen, int count) {
  int kept = 0;

  for (int i = 0; i < count; ++i) {
    bool beaten = false;

    for (int j = 0; j < count && !beaten; ++j) {
      if (j == i || !(seen[j].score > seen[i].score || (seen[j].score == seen[i].score && j < i))) {
        continue;
      }

      if (seen[j].label == seen[i].label) {
        beaten = overlap(seen[i], seen[j]) >= DETECT_SAME_BOX || inside(seen[i], seen[j]) >= DETECT_BOX_INSIDE;
      } else {
        beaten = seen[i].label != 0 && seen[j].label != 0 && overlap(seen[i], seen[j]) >= DETECT_SAME_BOX;
      }
    }

    if (!beaten) {
      seen[kept++] = seen[i];
    }
  }

  return kept;
}

// What is reported is steadier than any one look. The model flickers: three
// bottles held up read "bottle x2, bottle x2, nothing, bottle x2" (19 October
// 2026), and its one-look guesses -- a toilet, a book -- come and go in a
// single look. Requiring two looks running (the first try) threw the bottles
// away with the guesses. So, over the last three looks:
//   someone is reported when seen in this look or the last one;
//   anything else when seen in two of the last three -- or at once, when the
//   model is sure of it (DETECT_SURE) -- and kept, with its last boxes, until
//   it has been missed twice running.
static const float DETECT_SURE = 0.60f;
struct Track {
  uint8_t hits;        // this look and the two before, one bit each
  uint8_t count;       // boxes kept from the last look it was seen in
  bool shown;          // reported after the last look
  Seen boxes[3];
};

Track *tracks = nullptr;   // one per kind of thing, in PSRAM

int steadyOnes(Seen *seen, int count) {
  if (!tracks) {
    tracks = (Track *)heap_caps_calloc(LABEL_COUNT, sizeof(Track), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);

    if (!tracks) {
      return count;
    }
  }

  uint8_t now[LABEL_COUNT] = {0};

  for (int i = 0; i < count; ++i) {
    now[seen[i].label]++;
  }

  Seen kept[DETECT_MAX];
  int found = 0;

  for (int label = 0; label < LABEL_COUNT; ++label) {
    Track &track = tracks[label];
    track.hits = ((track.hits << 1) | (now[label] ? 1 : 0)) & 7;

    float best = 0;

    if (now[label]) {
      track.count = 0;

      for (int i = 0; i < count; ++i) {
        if (seen[i].label == label) {
          best = fmaxf(best, seen[i].score);

          if (track.count < 3) {
            track.boxes[track.count++] = seen[i];
          }
        }
      }
    }

    bool sure = label == 0 ? (track.hits & 3) != 0
                           : (__builtin_popcount(track.hits) >= 2 || best >= DETECT_SURE ||
                              (track.shown && (track.hits & 3)));
    track.shown = sure;

    for (int k = 0; sure && k < track.count && found < DETECT_MAX; ++k) {
      kept[found++] = track.boxes[k];
    }
  }

  memcpy(seen, kept, sizeof(Seen) * found);
  return found;
}

// The model's boxes are in its 256 x 256 letterbox; JARVIS is told where
// they are in the picture, 0 to 1 across and down.
int readOutput(Seen *seen, int width, int height) {
  float scale = fminf((float)MODEL_SIZE / width, (float)MODEL_SIZE / height);
  float pad_x = (MODEL_SIZE - width * scale) / 2, pad_y = (MODEL_SIZE - height * scale) / 2;
  int found = 0;

  for (int d = 0; d < 300 && found < DETECT_MAX; ++d) {
    const float *row = tensor_output0[d];

    int label = (int)row[5];

    if (label < 0 || label >= LABEL_COUNT ||
        row[4] < (label == 0 ? DETECT_SCORE_PERSON : DETECT_SCORE_OTHER)) {
      continue;
    }

    seen[found].label = label;
    seen[found].score = row[4];
    seen[found].x1 = fmaxf(0.0f, fminf(1.0f, (row[0] - pad_x) / scale / width));
    seen[found].y1 = fmaxf(0.0f, fminf(1.0f, (row[1] - pad_y) / scale / height));
    seen[found].x2 = fmaxf(0.0f, fminf(1.0f, (row[2] - pad_x) / scale / width));
    seen[found].y2 = fmaxf(0.0f, fminf(1.0f, (row[3] - pad_y) / scale / height));
    found++;
  }

  return oneNameEach(seen, found);
}

// What was seen, one word per kind and how many when more than one, sorted:
// "bottle x3,cup,person" -- so a change in what is there, or in how many, is
// told at once, and the same things are not told every run.
String kinds(const Seen *seen, int count) {
  uint8_t present[LABEL_COUNT] = {0};

  for (int i = 0; i < count; ++i) {
    present[seen[i].label]++;
  }

  String said;

  for (int label = 0; label < LABEL_COUNT; ++label) {
    if (present[label]) {
      said += (said.length() ? "," : "") + String(LABELS[label]);

      if (present[label] > 1) {
        said += " x" + String(present[label]);
      }
    }
  }

  return said;
}

static void addObjects(String &json, const Seen *seen, int count) {
  for (int i = 0; i < count; ++i) {
    char item[160];
    snprintf(item, sizeof(item), "%s{\"label\":\"%s\",\"score\":%.3f,\"box\":[%.3f,%.3f,%.3f,%.3f]}",
             i ? "," : "", LABELS[seen[i].label], seen[i].score, seen[i].x1, seen[i].y1, seen[i].x2, seen[i].y2);
    json += item;
  }
}

// What the board is sure of; and, when JARVIS asked, what this one look saw.
void sendSeen(const Seen *seen, int count, unsigned long took, const Seen *now = nullptr, int nowCount = 0) {
  String json = String("{\"name\":") + quoted(SENSOR_NAME) + ",\"ms\":" + String(took) + ",\"objects\":[";
  addObjects(json, seen, count);
  json += "]";

  if (now) {
    json += ",\"fresh\":true,\"now\":[";
    addObjects(json, now, nowCount);
    json += "]";
  }

  json += "}";

  HTTPClient https;
  String url = String("https://") + JARVIS_HOST + ":" + JARVIS_PORT + "/camera/seen";

  if (!https.begin(detectLink, url)) {
    return;
  }

  https.setTimeout(8000);
  https.addHeader("Content-Type", "application/json");
  https.addHeader("X-Jarvis-Token", JARVIS_TOKEN);
  https.addHeader("X-Jarvis-Camera", SENSOR_NAME);
  int code = https.POST(json);
  https.end();

  if (code != HTTP_CODE_OK) {
    Serial.printf("[jarvis] detection: JARVIS answered %d\n", code);
  }
}

// FEMTO-ML runs each conv in blocks of output channels whose filters fit in
// one buffer of internal SRAM, rather than streaming every filter from PSRAM
// for every pixel (esp_nn_conv_s8_simd.c, "Fast buffer"): the bigger the
// buffer, the fewer the blocks. Taken once wifi is up, and only while enough
// internal SRAM stays free for the TLS connections to JARVIS, which mbedTLS
// makes in internal SRAM; failing that, 16 KB of PSRAM, small enough for a
// block to stay in the data cache.
static const size_t DETECT_KEEP_INTERNAL = 96 * 1024;

// FEMTO-ML's depthwise convs (October 2026 on) work a few rows at a time in the
// fast buffer, on both cores, after checking themselves against FEMTO-ML's plain-C
// ones; if that check ever fails they are never used and the old way runs. Weak,
// so a FEMTO-ML from before still builds (and says so).
extern "C" int esp_nn_depthwise_conv_s8_simd_self_check(void) __attribute__((weak));

void sayDepthwiseCheck() {
  if (!esp_nn_depthwise_conv_s8_simd_self_check) {
    Serial.println("[jarvis] detection: depthwise convs the old way (FEMTO-ML from before October 2026)");
    return;
  }

  int check = esp_nn_depthwise_conv_s8_simd_self_check();
  Serial.printf("[jarvis] detection: depthwise convs %s\n",
                check == 1 ? "by rows, both cores (check passed)" :
                check == 0 ? "the old way: the rows check FAILED" : "the old way (no rows path here)");
}

void takeFastBuffer() {
  static const int sizes[] = {49152, 32768, 24576, 16384, 8192};
  size_t free_before = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);

  for (int size : sizes) {
    if (free_before < size + DETECT_KEEP_INTERNAL ||
        heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL) < (size_t)size) {
      continue;
    }

    void *buffer = heap_caps_malloc(size, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);

    if (buffer) {
      esp_nn_conv_s8_simd_set_fast_buf(buffer, size);
      Serial.printf("[jarvis] detection: %d KB of internal SRAM for the model's filters, %u bytes left\n",
                    size / 1024, (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
      return;
    }
  }

  void *buffer = heap_caps_malloc(16384, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  esp_nn_conv_s8_simd_set_fast_buf(buffer, buffer ? 16384 : 0);
  Serial.printf("[jarvis] detection: only %u bytes of internal SRAM free, so 16 KB of PSRAM for the model's filters\n",
                (unsigned)free_before);
}

#ifdef JARVIS_FACES
// When someone comes into view, up to FACE_LOOKS looks with them in it are also run through the
// face finder, on the same half-size picture, until one has a face in it: that look's picture is
// sent to JARVIS as it is (POST /camera/face), so JARVIS can tell who it is at once rather than
// waiting for live view to start. One is enough -- live view's frames carry on the check -- and
// the face finder takes about 1.9 s a look on the board (23 October 2026), so it stops there.
// Then no more until they have been out of view for FACE_GONE_LOOKS looks.
static const int FACE_LOOKS = 3;
static const int FACE_GONE_LOOKS = 3;
static const float FACE_SURE = 0.6f;              // YuNet's score, as JARVIS's own (actions/faces.py)
static const size_t LOOK_JPEG_SIZE = 96 * 1024;   // a VGA JPEG is 10 to 40 KB
static uint8_t *lookJpeg = nullptr;
static size_t lookJpegLength = 0;
static bool facesReady = false;

// The look's half-size picture (320 x 240, three bytes a pixel, red first or blue first with
// [bgr]) as the face finder wants it: blue, green, red, 0 to 255, padded below to its 256 rows.
static bool fillFaceInput(const uint8_t *pixels, int width, int height, bool bgr) {
  if (width != face_MODEL_INPUT_W || height > face_MODEL_INPUT_H || face_MODEL_INPUT_C != 3) {
    return false;
  }

  static int8_t quantized[256];
  static bool tabled = false;

  if (!tabled) {
    for (int v = 0; v < 256; ++v) {
      quantized[v] = face_model_quantize_input((float)v);
    }

    tabled = true;
  }

  const int b = bgr ? 0 : 2, r = bgr ? 2 : 0;
  int8_t *in = face_model_input_int8;
  const uint8_t *px = pixels;

  for (int i = 0; i < width * height; ++i, px += 3, in += 3) {
    in[0] = quantized[px[b]];
    in[1] = quantized[px[1]];
    in[2] = quantized[px[r]];
  }

  memset(in, quantized[0], (size_t)(face_MODEL_INPUT_H - height) * width * 3);
  return true;
}

// The surest face the face finder saw: YuNet's score is the square root of its class and
// object scores, cell by cell, at each of its three scales.
static float bestFace() {
  const float *cls[3] = {(const float *)face_tensor_cls_8, (const float *)face_tensor_cls_16,
                         (const float *)face_tensor_cls_32};
  const float *obj[3] = {(const float *)face_tensor_obj_8, (const float *)face_tensor_obj_16,
                         (const float *)face_tensor_obj_32};
  const int strides[3] = {8, 16, 32};
  float best = 0.0f;

  for (int s = 0; s < 3; ++s) {
    int cells = (face_MODEL_INPUT_W / strides[s]) * (face_MODEL_INPUT_H / strides[s]);

    for (int i = 0; i < cells; ++i) {
      float c = fminf(fmaxf(cls[s][i], 0.0f), 1.0f), o = fminf(fmaxf(obj[s][i], 0.0f), 1.0f);
      best = fmaxf(best, sqrtf(c * o));
    }
  }

  return best;
}

// The look's own picture, with a face in it, to JARVIS: untouched, as the camera made it.
static void sendFace(float score) {
  HTTPClient https;
  String url = String("https://") + JARVIS_HOST + ":" + JARVIS_PORT + "/camera/face";

  if (!https.begin(detectLink, url)) {
    return;
  }

  https.setTimeout(8000);
  https.addHeader("Content-Type", "image/jpeg");
  https.addHeader("X-Jarvis-Token", JARVIS_TOKEN);
  https.addHeader("X-Jarvis-Camera", SENSOR_NAME);
  https.addHeader("X-Jarvis-Face", String(score, 2));
  int code = https.POST(lookJpeg, lookJpegLength);
  https.end();

  if (code != HTTP_CODE_OK) {
    Serial.printf("[jarvis] face: JARVIS answered %d\n", code);
  }
}
#endif

// A decoded frame: VGA at three bytes a pixel, in PSRAM, kept for the run (reserveDetection).
static const size_t DETECT_RGB_SIZE = 640 * 480 * 3;
static uint8_t *detectRgb = nullptr;
static bool detectReserved = false;

// The model's memory and a decoded frame's, taken before the camera starts, so the camera sizes its
// picture buffers -- and so its stills -- from what is left, never the other way about.
void reserveDetection() {
  int problem = activations_init();

  if (problem) {
    Serial.printf("[jarvis] detection off: the model's memory could not be had (%d)\n", problem);
    return;
  }

  detectRgb = (uint8_t *)heap_caps_malloc(DETECT_RGB_SIZE, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);

  if (!detectRgb) {
    Serial.println("[jarvis] detection off: no memory for a decoded frame");
    return;
  }

  detectReserved = true;

#ifdef JARVIS_FACES
  // The face finder's memory too, before the camera's: without it, detection goes on without faces.
  int faceProblem = face_activations_init();
  lookJpeg = faceProblem ? nullptr : (uint8_t *)heap_caps_malloc(LOOK_JPEG_SIZE, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  facesReady = lookJpeg != nullptr;

  if (!facesReady) {
    Serial.printf("[jarvis] faces off: the face finder's memory could not be had (%d)\n", faceProblem);
  }
#endif
}

void detectTask(void *) {
  size_t rgb_size = DETECT_RGB_SIZE;
  uint8_t *rgb = detectRgb;
  String lastKinds = "";
  unsigned long lastSent = 0;

  for (;;) {
    vTaskDelay(pdMS_TO_TICKS(DETECT_GAP_MS));

    if (WiFi.status() != WL_CONNECTED) {
      continue;
    }

    if (!esp_nn_conv_s8_simd_fast_size()) {
      takeFastBuffer();
      // The convolutions on both cores: a worker on core 1 takes half of each
      // layer's rows. Core 1 runs the sketch's loop -- heartbeats, pictures, live
      // view -- which mostly waits on the network, so it has the time; live view
      // and detection share it while both run.
      Serial.printf("[jarvis] detection: convolutions on %d cores\n", esp_nn_conv_s8_simd_set_cores(2));
      sayDepthwiseCheck();
    }

    unsigned long asked = millis();
    camera_fb_t *frame = esp_camera_fb_get();

    if (!frame) {
      continue;
    }

    // A VGA picture is decoded at half size: 320 x 240 is still more than the
    // model's 256 across, the decoder skips most of its work at that scale
    // (it was about 300 ms of each look at full size), and halving in the
    // decoder averages pixels where the letterbox would only have dropped them.
    int width = frame->width, height = frame->height;
    bool bgr = false, decoded = false;

    if (frame->format == PIXFORMAT_JPEG && width >= 2 * MODEL_SIZE) {
      esp_jpeg_image_cfg_t jpeg = {};
      esp_jpeg_image_output_t out = {};
      jpeg.indata = frame->buf;
      jpeg.indata_size = frame->len;
      jpeg.outbuf = rgb;
      jpeg.outbuf_size = rgb_size;
      jpeg.out_format = JPEG_IMAGE_FORMAT_RGB888;
      jpeg.out_scale = JPEG_IMAGE_SCALE_1_2;
      decoded = esp_jpeg_decode(&jpeg, &out) == ESP_OK;
      width = out.width;
      height = out.height;
    }

    if (!decoded) {
      width = frame->width;
      height = frame->height;
      // The camera library's "RGB888" is blue, green, red in memory.
      bgr = true;
      decoded = (size_t)width * height * 3 <= rgb_size &&
                fmt2rgb888(frame->buf, frame->len, frame->format, rgb);
    }

#ifdef JARVIS_FACES
    // The look's own picture, kept in case it is sent (the frame goes back to the camera now).
    lookJpegLength = 0;

    if (facesReady && frame->format == PIXFORMAT_JPEG && frame->len <= LOOK_JPEG_SIZE) {
      memcpy(lookJpeg, frame->buf, frame->len);
      lookJpegLength = frame->len;
    }
#endif

    esp_camera_fb_return(frame);

    if (!decoded) {
      continue;
    }

    fillInput(rgb, width, height, bgr);
    unsigned long started = millis();
#ifdef MODEL_INPUT_INT8
    run_model_int8_input();
#else
    run_model();
#endif
    unsigned long took = millis() - started;
    unsigned long ready = started - asked;   // the picture taken, decoded and letterboxed

    Seen seen[DETECT_MAX], looked[DETECT_MAX];
    int lookedCount = readOutput(looked, width, height);
    memcpy(seen, looked, sizeof(Seen) * lookedCount);
    int count = steadyOnes(seen, lookedCount);
    String now = kinds(seen, count);

    // This look as the model saw it, with its scores -- what a threshold is set from.
    String scores;

    for (int i = 0; i < lookedCount; ++i) {
      char one[48];
      snprintf(one, sizeof(one), "%s%s %.2f", i ? ", " : "", LABELS[looked[i].label], looked[i].score);
      scores += one;
    }

    Serial.printf("[jarvis] detection: %s in %lu ms, the picture ready in %lu ms (this look: %s)\n",
                  now.length() ? now.c_str() : "nothing", took, ready, scores.length() ? scores.c_str() : "nothing");

    // For the live view: what the board is sure of and this look, with their boxes (streamLive).
    String marksJson = "{\"objects\":[";
    addObjects(marksJson, seen, count);
    marksJson += "],\"now\":[";
    addObjects(marksJson, looked, lookedCount);
    marksJson += "]}";

    if (marksJson.length() < sizeof(liveMarks)) {
      portENTER_CRITICAL(&liveMarksLock);
      memcpy(liveMarks, marksJson.c_str(), marksJson.length());
      liveMarksLength = marksJson.length();
      liveMarksSeq++;
      portEXIT_CRITICAL(&liveMarksLock);
    }

#if FEMTO_PROFILE
    // PlatformIO env "s3profile": where those milliseconds went, node by node.
    static char profile[3072];
    femto_prof_report(profile, sizeof(profile));
    Serial.print(profile);
#endif

    if (lookWanted) {
      lookWanted = false;
      sendSeen(seen, count, took, looked, lookedCount);
      lastKinds = now;
      lastSent = millis();
    } else if (now != lastKinds || (count > 0 && millis() - lastSent >= DETECT_REPEAT_MS)) {
      sendSeen(seen, count, took);
      lastKinds = now;
      lastSent = millis();
    }

#ifdef JARVIS_FACES
    // After the report, so JARVIS has begun the sighting the picture belongs to.
    static int faceLooks = 0, goneLooks = 0;
    static bool faceSent = false;
    bool personHere = false;

    for (int i = 0; i < lookedCount; ++i) {
      personHere = personHere || looked[i].label == 0;
    }

    goneLooks = personHere ? 0 : goneLooks + 1;

    if (goneLooks >= FACE_GONE_LOOKS) {
      faceLooks = 0;
      faceSent = false;
    }

    if (personHere && !faceSent && faceLooks < FACE_LOOKS && lookJpegLength &&
        fillFaceInput(rgb, width, height, bgr)) {
      faceLooks++;
      unsigned long faceStarted = millis();
      face_run_model_int8_input();
      float face = bestFace();
      unsigned long faceTook = millis() - faceStarted;

#if FEMTO_PROFILE
      // The face finder's own steps, apart from YOLO's (whose report came before it ran).
      static char faceProfile[3072];
      femto_prof_report(faceProfile, sizeof(faceProfile));
      Serial.print("[jarvis] face finder ");
      Serial.print(faceProfile);
#endif

      if (face >= FACE_SURE) {
        unsigned long sendStarted = millis();
        sendFace(face);
        faceSent = true;
        Serial.printf("[jarvis] face: %.2f in %lu ms, sent in %lu ms\n", face, faceTook, millis() - sendStarted);
      } else {
        Serial.printf("[jarvis] face: none in %lu ms (surest %.2f)\n", faceTook, face);
      }
    }
#endif
  }
}

void startDetection() {
  if (!detectReserved) {
    return;
  }

  detectLink.setCACert(JARVIS_CA);
  detectLink.setHandshakeTimeout(HANDSHAKE_SECONDS);

  // Core 0, below the wifi's own tasks; pictures and live view run on core 1.
  if (xTaskCreatePinnedToCore(detectTask, "detect", 16384, nullptr, 1, nullptr, 0) == pdPASS) {
    detectReady = true;
    Serial.printf("[jarvis] detection on: YOLO26n, %d kinds of thing%s; %u bytes of PSRAM left\n",
                  LABEL_COUNT,
#ifdef JARVIS_FACES
                  facesReady ? ", and faces" : ", no faces",
#else
                  "",
#endif
                  (unsigned)ESP.getFreePsram());
  }
}
#endif

#if defined(JARVIS_BOARD_DFR1154)
// Talking to JARVIS through the toy: hold BOOT and speak, let go, and JARVIS's answer comes out of
// the toy's own speaker. While the button is held the board records its microphone (PDM, 16 kHz,
// the LED on); on letting go it posts the recording to POST /toy/talk and plays the reply -- raw
// 16-bit PCM at the same rate -- as it arrives. Nothing is recorded unless the button is held, so
// "Jarvis" said to the PC is never heard here. Its own task and its own connection: the polls,
// live view and detection carry on meanwhile.
static const int TALK_BUTTON_PIN = 0;        // BOOT: free once the board has started
static const int TALK_LED_PIN = 3;
static const int TALK_MIC_CLOCK_PIN = 38;
static const int TALK_MIC_DATA_PIN = 39;
static const int TALK_SPEAKER_BCLK_PIN = 45;
static const int TALK_SPEAKER_LRC_PIN = 46;
static const int TALK_SPEAKER_DOUT_PIN = 42;
static const uint32_t TALK_RATE = 16000;
static const unsigned long TALK_MOST_MS = 10000UL;    // a recording stops here, button or not
static const unsigned long TALK_LEAST_MS = 400UL;     // shorter is a stray press: nothing sent
static const unsigned long TALK_ANSWER_MS = 45000UL;  // hearing, thinking and speaking, at most
static const size_t WAV_HEADER = 44;

static I2SClass talkMic;
static I2SClass talkSpeaker;
NetworkClientSecure talkLink;
static uint8_t *talkBuffer = nullptr;

static void putLittle(uint8_t *at, uint32_t value, int bytes) {
  for (int i = 0; i < bytes; i++) {
    at[i] = (value >> (8 * i)) & 0xFF;
  }
}

// A 44-byte WAV header for [dataBytes] of 16-bit mono PCM at TALK_RATE.
static void wavHeader(uint8_t *at, uint32_t dataBytes) {
  memcpy(at, "RIFF", 4);
  putLittle(at + 4, 36 + dataBytes, 4);
  memcpy(at + 8, "WAVEfmt ", 8);
  putLittle(at + 16, 16, 4);
  putLittle(at + 20, 1, 2);                 // PCM
  putLittle(at + 22, 1, 2);                 // mono
  putLittle(at + 24, TALK_RATE, 4);
  putLittle(at + 28, TALK_RATE * 2, 4);
  putLittle(at + 32, 2, 2);
  putLittle(at + 34, 16, 2);
  memcpy(at + 36, "data", 4);
  putLittle(at + 40, dataBytes, 4);
}

static void blinkTalkLed(int times) {
  for (int i = 0; i < times; i++) {
    digitalWrite(TALK_LED_PIN, HIGH);
    delay(120);
    digitalWrite(TALK_LED_PIN, LOW);
    delay(120);
  }
}

// Records while the button is held; the number of PCM bytes after the header.
static size_t recordWhileHeld() {
  const size_t most = TALK_MOST_MS * TALK_RATE * 2 / 1000;
  size_t got = 0;
  unsigned long began = millis();

  digitalWrite(TALK_LED_PIN, HIGH);

  while (digitalRead(TALK_BUTTON_PIN) == LOW && got < most) {
    size_t want = most - got < 1024 ? most - got : 1024;
    got += talkMic.readBytes((char *)(talkBuffer + WAV_HEADER + got), want);
  }

  digitalWrite(TALK_LED_PIN, LOW);
  Serial.printf("[jarvis] talk: %lu ms recorded\n", millis() - began);
  return got & ~(size_t)1;
}

// Sends [pcm] bytes of recording and plays the answer as it comes. True if an answer was played.
static bool askAndPlay(size_t pcm) {
  wavHeader(talkBuffer, pcm);

  HTTPClient https;
  String url = String("https://") + JARVIS_HOST + ":" + JARVIS_PORT + "/toy/talk";

  if (!https.begin(talkLink, url)) {
    Serial.println("[jarvis] talk: could not start the request");
    return false;
  }

  https.setTimeout(TALK_ANSWER_MS);
  https.addHeader("Content-Type", "audio/wav");
  https.addHeader("X-Jarvis-Token", JARVIS_TOKEN);
  https.addHeader("X-Jarvis-Camera", SENSOR_NAME);

  unsigned long sent = millis();
  int code = https.POST(talkBuffer, WAV_HEADER + pcm);

  if (code != HTTP_CODE_OK) {
    Serial.printf("[jarvis] talk: JARVIS answered %d\n", code);
    https.end();
    return false;
  }

  lastDelivered = millis();
  NetworkClient *answer = https.getStreamPtr();
  int remaining = https.getSize();          // -1: until the connection closes
  uint8_t chunk[1024];
  size_t played = 0;
  bool odd = false;
  uint8_t carry = 0;
  unsigned long quiet = millis();

  Serial.printf("[jarvis] talk: answered in %lu ms\n", millis() - sent);

  while (https.connected() && (remaining > 0 || remaining == -1)) {
    int ready = answer->available();

    if (ready <= 0) {
      if (millis() - quiet > 5000UL) {
        break;
      }

      delay(2);
      continue;
    }

    quiet = millis();
    int wanted = ready > (int)sizeof(chunk) - 1 ? (int)sizeof(chunk) - 1 : ready;

    if (remaining > 0 && wanted > remaining) {
      wanted = remaining;
    }

    // Keep the samples whole: a byte left over waits for the next read.
    size_t start = 0;

    if (odd) {
      chunk[0] = carry;
      start = 1;
    }

    int got = answer->readBytes(chunk + start, wanted);

    if (got <= 0) {
      break;
    }

    if (remaining > 0) {
      remaining -= got;
    }

    size_t have = start + got;
    odd = have & 1;

    if (odd) {
      carry = chunk[have - 1];
      have--;
    }

    talkSpeaker.write(chunk, have);
    played += have;
  }

  https.end();
  Serial.printf("[jarvis] talk: %u ms of answer played\n", (unsigned)(played * 1000UL / (TALK_RATE * 2)));
  return played > 0;
}

void talkTask(void *) {
  for (;;) {
    if (digitalRead(TALK_BUTTON_PIN) != LOW || WiFi.status() != WL_CONNECTED) {
      delay(20);
      continue;
    }

    size_t pcm = recordWhileHeld();

    if (pcm < TALK_LEAST_MS * TALK_RATE * 2 / 1000) {
      Serial.println("[jarvis] talk: too short, not sent");
    } else if (!askAndPlay(pcm)) {
      blinkTalkLed(3);
    }

    // Let go of the button before listening again.
    while (digitalRead(TALK_BUTTON_PIN) == LOW) {
      delay(20);
    }
  }
}

void startTalk() {
  pinMode(TALK_BUTTON_PIN, INPUT_PULLUP);
  pinMode(TALK_LED_PIN, OUTPUT);
  digitalWrite(TALK_LED_PIN, LOW);

  talkBuffer = (uint8_t *)heap_caps_malloc(WAV_HEADER + TALK_MOST_MS * TALK_RATE * 2 / 1000,
                                           MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  talkMic.setPinsPdmRx(TALK_MIC_CLOCK_PIN, TALK_MIC_DATA_PIN);
  talkSpeaker.setPins(TALK_SPEAKER_BCLK_PIN, TALK_SPEAKER_LRC_PIN, TALK_SPEAKER_DOUT_PIN);

  if (!talkBuffer) {
    Serial.println("[jarvis] talk: no room to record; the toy cannot talk");
    return;
  }

  if (!talkMic.begin(I2S_MODE_PDM_RX, TALK_RATE, I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO)) {
    Serial.println("[jarvis] talk: the microphone did not start");
    return;
  }

  if (!talkSpeaker.begin(I2S_MODE_STD, TALK_RATE, I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO)) {
    Serial.println("[jarvis] talk: the speaker did not start");
    return;
  }

  talkLink.setCACert(JARVIS_CA);
  talkLink.setHandshakeTimeout(HANDSHAKE_SECONDS);

  if (xTaskCreatePinnedToCore(talkTask, "talk", 8192, nullptr, 1, nullptr, 1) != pdPASS) {
    Serial.println("[jarvis] talk: could not start");
    return;
  }

  Serial.println("[jarvis] talk: hold BOOT and speak");
}
#endif

void setup() {
  Serial.begin(115200);
  delay(200);

  if (FLASH_LED_PIN >= 0) {
    pinMode(FLASH_LED_PIN, OUTPUT);
    digitalWrite(FLASH_LED_PIN, LOW);
  }

#if defined(JARVIS_BOARD_DFR1154)
  // Its infrared lights held off, never left to float.
  pinMode(DFR1154_IR_PIN, OUTPUT);
  digitalWrite(DFR1154_IR_PIN, LOW);
#endif

#if defined(JARVIS_BOARD_DFR1154)
  Serial.print("[jarvis] DFRobot ESP32-S3 AI Camera as ");
#elif defined(CONFIG_IDF_TARGET_ESP32S3)
  Serial.print("[jarvis] ESP32-S3-CAM as ");
#else
  Serial.print("[jarvis] ESP32-CAM as ");
#endif
  Serial.println(SENSOR_NAME);

#ifdef JARVIS_DETECT
  reserveDetection();
#endif
  cameraReady = startCamera();

  // JARVIS's own authority: the board accepts JARVIS and nothing else.
  secure.setCACert(JARVIS_CA);
  secure.setHandshakeTimeout(HANDSHAKE_SECONDS);
  liveLink.setCACert(JARVIS_CA);
  liveLink.setHandshakeTimeout(HANDSHAKE_SECONDS);

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

#ifdef JARVIS_DETECT
  if (cameraReady) {
    startDetection();
  }
#endif

#if defined(JARVIS_BOARD_DFR1154)
  startTalk();
#endif
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

    Wanted wanted = pictureWanted();

#ifdef JARVIS_DETECT
    if (wanted.look) {
      lookWanted = true;
    }
#endif

    if (wanted.still) {
      takeStill();
    }

    if (wanted.live) {
      streamLive();
      lastAsk = millis();
    } else if (wanted.snap) {
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
