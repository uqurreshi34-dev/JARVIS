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

// The toy's cloud brain (jarvis-toy-cloud on Render), when tools/toy_cloud_setup.py has written
// toy_cloud.h (its address and token): the toy then has a PC mode and a cloud mode. Without that
// file, and on every other board, nothing here changes.
#if defined(JARVIS_BOARD_DFR1154) && __has_include("toy_cloud.h")
#define JARVIS_TOY_CLOUD
#include "toy_cloud.h"
#include "toy_cloud_ca.h"
#include <Preferences.h>
#endif

#if defined(JARVIS_BOARD_DFR1154)
#include "ESP_I2S.h"
#include "esp_heap_caps.h"
#include "mbedtls/platform.h"
#include "driver/ledc.h"
#include "esp32-hal-rmt.h"
#include "model_path.h"
#include "esp_wn_iface.h"
#include "esp_wn_models.h"
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

#if defined(JARVIS_BOARD_DFR1154)
// The toy's head turns to whoever detection sees (toySawPeople, with the toy's body below).
void toySawPeople(const Seen *seen, int count);
#endif

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

#ifdef JARVIS_TOY_CLOUD
// ---- PC mode and cloud mode ------------------------------------------------------------------
//
// "Jarvis, switch to cloud mode": the toy talks, sends what it sees and the faces it finds to
// jarvis-toy-cloud, and needs no PC. "Jarvis, switch to PC mode": all of it goes to JARVIS on the
// PC, as before, live view too -- and while the PC does not answer its heartbeat, to the cloud
// instead, which says so ("Defaulting to the cloud, sir ..."). Whichever side hears the words
// answers with X-Jarvis-Switch; the toy keeps the choice in its own flash, so a restart keeps it.
// Cloud mode until told otherwise: the toy is meant to work with the PC off.
static Preferences toyPrefs;
static volatile bool toyCloudMode = true;
static volatile bool pcReachable = false;   // the PC answered its last heartbeat
static volatile bool toyAnnounceDue = false;   // the cloud has words about what the toy sees
// "Jarvis, learn my face, I'm Sarah": the cloud asks for this many faces (X-Jarvis-Faces), and the
// face finder sends one from each look with someone in it until they are given -- not just the one
// it sends when someone comes into view.
static volatile int toyFacesWanted = 0;
// Learning stops here even if faces are still wanted, as the cloud gives up after a minute too.
static const unsigned long TOY_LEARN_MS = 60UL * 1000UL;
static volatile unsigned long toyLearnUntil = 0;
// In cloud mode faces are also looked for now and then whether or not YOLO sees a person (at a desk,
// behind a laptop, it sees the laptop), and a face in view is sent again each minute: so the cloud
// greets whoever it knows and can say who is there. The face finder takes about 1.9 s, so not often.
static const unsigned long FACE_PROBE_MS = 10UL * 1000UL;
static const unsigned long FACE_RESEND_MS = 60UL * 1000UL;
static const unsigned long CLOUD_PING_MS = 60UL * 1000UL;
static unsigned long lastCloudPing = 0;
NetworkClientSecure cloudLink;   // the loop's: the cloud's /health

void loadToyMode() {
  toyPrefs.begin("toy", false);
  toyCloudMode = toyPrefs.getBool("cloud", true);
  Serial.printf("[jarvis] toy: %s mode\n", toyCloudMode ? "cloud" : "PC");
}

void saveToyMode(bool cloud) {
  if (cloud != toyCloudMode) {
    toyPrefs.putBool("cloud", cloud);
  }

  toyCloudMode = cloud;

  if (!cloud) {
    // Asked about at once: the PC's heartbeat says whether it is there.
    pcReachable = false;
    saidOnline = false;
  }

  Serial.printf("[jarvis] toy: switched to %s mode\n", cloud ? "cloud" : "PC");
}

// Where talk, detections and faces go now: the PC in PC mode while it answers, else the cloud.
bool toyUsesCloud() {
  return toyCloudMode || !pcReachable;
}

String cloudUrl(const char *path) {
  return String("https://") + TOY_CLOUD_HOST + path;
}

// The cloud is there: counts as JARVIS reached, so the board does not restart for want of the PC.
bool cloudPing() {
  HTTPClient https;

  if (!https.begin(cloudLink, cloudUrl("/health"))) {
    return false;
  }

  https.setTimeout(POST_TIMEOUT_MS);
  int code = https.GET();
  https.end();

  if (code == HTTP_CODE_OK) {
    lastDelivered = millis();
    return true;
  }

  Serial.printf("[jarvis] cloud: /health answered %d\n", code);
  return false;
}
#endif

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
#ifdef JARVIS_TOY_CLOUD
NetworkClientSecure cloudDetectLink;   // the same reports, in cloud mode
#endif
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
#ifdef JARVIS_TOY_CLOUD
  // In cloud mode (or with the PC off) the cloud hears it, and answers "say" when something has
  // come into view: the talk task fetches the words and says them on the toy's speaker.
  bool cloud = toyUsesCloud();
  String url = cloud ? cloudUrl("/camera/seen") : String("https://") + JARVIS_HOST + ":" + JARVIS_PORT + "/camera/seen";

  if (!https.begin(cloud ? cloudDetectLink : detectLink, url)) {
    return;
  }

  https.addHeader("X-Jarvis-Token", cloud ? TOY_CLOUD_TOKEN : JARVIS_TOKEN);
#else
  String url = String("https://") + JARVIS_HOST + ":" + JARVIS_PORT + "/camera/seen";

  if (!https.begin(detectLink, url)) {
    return;
  }

  https.addHeader("X-Jarvis-Token", JARVIS_TOKEN);
#endif

  https.setTimeout(8000);
  https.addHeader("Content-Type", "application/json");
  https.addHeader("X-Jarvis-Camera", SENSOR_NAME);
  int code = https.POST(json);

#ifdef JARVIS_TOY_CLOUD
  if (cloud && code == HTTP_CODE_OK) {
    String answer = https.getString();
    answer.replace(" ", "");

    if (answer.indexOf("\"say\":true") >= 0) {
      toyAnnounceDue = true;
    }
  }
#endif

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
#ifdef JARVIS_TOY_CLOUD
  bool cloud = toyUsesCloud();
  String url = cloud ? cloudUrl("/camera/face") : String("https://") + JARVIS_HOST + ":" + JARVIS_PORT + "/camera/face";

  if (!https.begin(cloud ? cloudDetectLink : detectLink, url)) {
    return;
  }

  https.addHeader("X-Jarvis-Token", cloud ? TOY_CLOUD_TOKEN : JARVIS_TOKEN);
#else
  String url = String("https://") + JARVIS_HOST + ":" + JARVIS_PORT + "/camera/face";

  if (!https.begin(detectLink, url)) {
    return;
  }

  https.addHeader("X-Jarvis-Token", JARVIS_TOKEN);
#endif

  https.setTimeout(8000);
  https.addHeader("Content-Type", "image/jpeg");
  https.addHeader("X-Jarvis-Camera", SENSOR_NAME);
  https.addHeader("X-Jarvis-Face", String(score, 2));
  int code = https.POST(lookJpeg, lookJpegLength);

#ifdef JARVIS_TOY_CLOUD
  // A known face is greeted by name, and a face lesson says how it went: fetched and said by the talk task.
  if (cloud && code == HTTP_CODE_OK) {
    String answer = https.getString();
    answer.replace(" ", "");

    if (answer.indexOf("\"say\":true") >= 0) {
      toyAnnounceDue = true;
    }
  }
#endif

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
#if defined(JARVIS_BOARD_DFR1154)
    toySawPeople(looked, lookedCount);
#endif
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

    bool learning = false;
    bool probing = false;

#ifdef JARVIS_TOY_CLOUD
    // Learning a face: the face finder runs on every look, whether or not YOLO calls it a person
    // (close up, or behind a laptop, it often doesn't), until enough faces have gone or time is up.
    if (toyFacesWanted > 0 && (long)(millis() - toyLearnUntil) >= 0) {
      Serial.println("[jarvis] face: learning timed out");
      toyFacesWanted = 0;
    }

    learning = toyFacesWanted > 0;

    if (learning) {
      faceLooks = 0;
      faceSent = false;
    }

    static unsigned long lastProbe = 0, lastFaceSent = 0;

    if (!learning && toyUsesCloud() && millis() - lastProbe >= FACE_PROBE_MS &&
        (lastFaceSent == 0 || millis() - lastFaceSent >= FACE_RESEND_MS)) {
      lastProbe = millis();
      probing = true;
    }
#endif

    bool arriving = personHere && !faceSent && faceLooks < FACE_LOOKS;

    if ((arriving || learning || probing) && lookJpegLength && fillFaceInput(rgb, width, height, bgr)) {
      if (arriving || learning) {
        faceLooks++;
      }

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
#ifdef JARVIS_TOY_CLOUD
        lastFaceSent = millis();
        if (toyFacesWanted > 0) {
          toyFacesWanted--;
          Serial.printf("[jarvis] face: sent to learn, %d more wanted\n", (int)toyFacesWanted);
        }
#endif
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
#ifdef JARVIS_TOY_CLOUD
  cloudDetectLink.setCACert(TOY_CLOUD_CA);
  cloudDetectLink.setHandshakeTimeout(HANDSHAKE_SECONDS);
#endif

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
// Talking to JARVIS through the toy, hands-free: the toy listens all the time, and when someone
// speaks it records what they say -- from half a second before the sound began to most of a second
// after it stops -- and posts it to POST /toy/talk. JARVIS turns away anything not said to him
// ("Jarvis, ..." first, as at the desk, or a follow-up to his last answer): the toy plays nothing,
// and listens on. His answer comes back as raw 16-bit PCM and plays on the toy's own speaker as it
// arrives. BOOT held is the same without his name: everything said while it is held is his. The LED
// is on while the toy is recording. Its own task and its own connection, so the polls, live view
// and detection carry on meanwhile.
static const int TALK_BUTTON_PIN = 0;        // BOOT: free once the board has started
static const int TALK_LED_PIN = 3;
static const int TALK_MIC_CLOCK_PIN = 38;
static const int TALK_MIC_DATA_PIN = 39;
static const int TALK_SPEAKER_BCLK_PIN = 45;
static const int TALK_SPEAKER_LRC_PIN = 46;
static const int TALK_SPEAKER_DOUT_PIN = 42;
static const uint32_t TALK_RATE = 16000;
static const unsigned long TALK_MOST_MS = 10000UL;    // a recording stops here, whatever is said
static const unsigned long TALK_LEAST_MS = 500UL;     // shorter is a cough or a knock: nothing sent
static const unsigned long TALK_ANSWER_MS = 45000UL;  // hearing, thinking and speaking, at most
static const size_t WAV_HEADER = 44;

// Hearing someone speak: the level of each 20 ms of sound against the room's own quiet, which the
// toy keeps learning while nobody speaks. Speech is louder than the quiet by TALK_START_TIMES for
// TALK_START_FRAMES frames running; it has ended after TALK_END_FRAMES frames back under
// TALK_END_TIMES. Never quieter than TALK_LEAST_LEVEL (of 32767), so a silent room's hiss is not
// taken for a voice.
static const int TALK_FRAME = 320;                    // samples: 20 ms
static const int TALK_PREROLL_FRAMES = 25;            // half a second kept before the sound began
static const float TALK_START_TIMES = 3.5f;
static const float TALK_END_TIMES = 2.0f;
static const int TALK_START_FRAMES = 3;
static const int TALK_END_FRAMES = 40;                // 0.8 s of quiet
static const float TALK_LEAST_LEVEL = 150.0f;
static const unsigned long TALK_AFTER_ANSWER_MS = 400UL;   // its own voice's echo, let go

// What the toy is doing, for its light ring and head (toyBodyTask, below): set as the talk goes.
enum ToyMood { TOY_IDLE, TOY_LISTENING, TOY_THINKING, TOY_SPEAKING, TOY_SPEAKING_FOND };
static volatile int toyMood = TOY_IDLE;

// How loud his answer is right now, 0 to 1, from what the speaker is playing: the ring pulses with it.
static volatile float toyVoice = 0.0f;

static I2SClass talkMic;
static I2SClass talkSpeaker;
NetworkClientSecure talkLink;
#ifdef JARVIS_TOY_CLOUD
NetworkClientSecure cloudTalkLink;   // talk, and what was seen, in cloud mode
#endif
static uint8_t *talkBuffer = nullptr;
static int16_t *talkPreroll = nullptr;

// "Jarvis" heard on the board itself (Espressif's WakeNet, the model from tools/toy_wake_model.py in the
// flash's "model" partition): a recording leaves the toy only when "Jarvis" was said in it (or just
// before it), with BOOT held, or as a follow-up within TOY_FOLLOW_MS of an answer, as the cloud's and
// the PC's own follow-up window. Anything else -- chat, the TV -- is never sent anywhere. Without the
// model every recording goes to be checked for "Jarvis" as before, and the toy says so as it starts.
static const char *WAKE_PARTITION = "model";
static const unsigned long WAKE_BEFORE_MS = 2000UL;   // "Jarvis" this long before the recording began counts
static const unsigned long TOY_FOLLOW_MS = 12000UL;   // FOLLOW_UP_SECONDS in the cloud and phone.py
static const esp_wn_iface_t *wakeNet = nullptr;
static model_iface_data_t *wakeModel = nullptr;
static int16_t *wakeChunk = nullptr;
static int wakeChunkSize = 0;
static int wakeFilled = 0;
static volatile unsigned long wakeHeardAt = 0;
static bool wakeEverHeard = false;

// This microphone is quiet: speech averages a few hundred of 32767 (the room 15), far below what
// WakeNet was trained on, and it heard no "Jarvis" at all (31 October 2026). Its copy of the sound is
// raised WAKE_GAIN times (what is sent is not), and it is told to be eager (DET_MODE_95, threshold
// WAKE_THRESHOLD, below its own 0.63): a false "Jarvis" only sends that recording to be checked,
// as every recording was before, while a missed one leaves him unanswered.
static const float WAKE_GAIN = 8.0f;
static const float WAKE_THRESHOLD = 0.5f;
static int wakeLoudest = 0;   // the loudest sample WakeNet was given since the last recording began

static bool startWake() {
  srmodel_list_t *models = esp_srmodel_init(WAKE_PARTITION);
  char *name = models && models->num > 0 ? esp_srmodel_filter(models, ESP_WN_PREFIX, NULL) : nullptr;

  if (!name) {
    Serial.println("[jarvis] wake: no \"Jarvis\" model on the board (python tools/toy_wake_model.py, then flash): "
                   "every recording goes to be checked for Jarvis");
    return false;
  }

  wakeNet = esp_wn_handle_from_name(name);
  wakeModel = wakeNet ? wakeNet->create(name, DET_MODE_95) : nullptr;
  wakeChunkSize = wakeModel ? wakeNet->get_samp_chunksize(wakeModel) : 0;
  wakeChunk = wakeChunkSize > 0 ? (int16_t *)heap_caps_malloc(wakeChunkSize * 2, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)
                                : nullptr;

  if (!wakeChunk) {
    Serial.printf("[jarvis] wake: %s did not start: every recording goes to be checked for Jarvis\n", name);
    wakeModel = nullptr;
    return false;
  }

  float own = wakeNet->get_det_threshold(wakeModel, 1);
  wakeNet->set_det_threshold(wakeModel, WAKE_THRESHOLD, 1);
  Serial.printf("[jarvis] wake: listening for \"Jarvis\" on the board (%s, %d samples at a time, the sound raised "
                "%.0f times, threshold %.2f, its own %.2f; %u bytes of internal memory free)\n", name, wakeChunkSize,
                WAKE_GAIN, wakeNet->get_det_threshold(wakeModel, 1), own,
                (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
  return true;
}

// Every frame the microphone gives, while listening and while recording: "Jarvis" noted when heard.
static void listenForWake(const int16_t *frame) {
  if (!wakeModel) {
    return;
  }

  int at = 0;

  while (at < TALK_FRAME) {
    int take = wakeChunkSize - wakeFilled < TALK_FRAME - at ? wakeChunkSize - wakeFilled : TALK_FRAME - at;
    for (int i = 0; i < take; i++) {
      float raised = frame[at + i] * WAKE_GAIN;
      int16_t sample = raised > 32767.0f ? 32767 : (raised < -32768.0f ? -32768 : (int16_t)raised);
      int size = sample < 0 ? -sample : sample;
      wakeLoudest = size > wakeLoudest ? size : wakeLoudest;
      wakeChunk[wakeFilled + i] = sample;
    }

    wakeFilled += take;
    at += take;

    if (wakeFilled == wakeChunkSize) {
      wakeFilled = 0;

      if (wakeNet->detect(wakeModel, wakeChunk) == WAKENET_DETECTED) {
        wakeHeardAt = millis();
        wakeEverHeard = true;
        Serial.println("[jarvis] wake: \"Jarvis\" heard on the board");
      }
    }
  }
}

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

// The microphone's steady offset, taken out of every sample as it is read (a DC blocker: what is left
// is the sound itself). Without it the offset alone read as speech: the toy "heard" someone all the
// time, every recording ran its full ten seconds, and the room's quiet was never learnt (29 October 2026).
static float dcIn = 0.0f;
static float dcOut = 0.0f;
static const float DC_KEEP = 0.995f;

// One frame of the microphone into [frame], its offset taken out; its level, the mean of the samples' size.
static float readFrame(int16_t *frame) {
  size_t got = 0;

  while (got < TALK_FRAME * 2) {
    size_t read = talkMic.readBytes((char *)frame + got, TALK_FRAME * 2 - got);

    if (read == 0) {
      delay(1);
    }

    got += read;
  }

  float total = 0.0f;

  for (int i = 0; i < TALK_FRAME; i++) {
    float in = frame[i];
    float out = in - dcIn + DC_KEEP * dcOut;
    dcIn = in;
    dcOut = out;
    out = out > 32767.0f ? 32767.0f : (out < -32768.0f ? -32768.0f : out);
    frame[i] = (int16_t)out;
    total += fabsf(out);
  }

  return total / TALK_FRAME;
}

// Throws away what the microphone heard over the next [ms]: the toy's own answer, still echoing.
static void letGo(unsigned long ms) {
  int16_t frame[TALK_FRAME];
  unsigned long until = millis() + ms;

  while ((long)(until - millis()) > 0) {
    readFrame(frame);
  }
}

static int playAnswer(HTTPClient &https, bool fond);

// Sends [pcm] bytes of recording and plays the answer as it comes: 1 if an answer was played, 0 if
// JARVIS had nothing to say (it was not said to him), -1 if he could not be reached. With the toy's
// cloud brain: to the cloud in cloud mode, or in PC mode while the PC does not answer (saying so,
// X-Jarvis-Fallback); a PC that stops answering mid-ask is asked of the cloud at once [toCloud].
static int askAndPlay(size_t pcm, bool button, bool named = false, bool toCloud = false) {
  wavHeader(talkBuffer, pcm);

  HTTPClient https;
#ifdef JARVIS_TOY_CLOUD
  bool cloud = toCloud || toyUsesCloud();
  String url = cloud ? cloudUrl("/toy/talk") : String("https://") + JARVIS_HOST + ":" + JARVIS_PORT + "/toy/talk";
  NetworkClientSecure &link = cloud ? cloudTalkLink : talkLink;
#else
  String url = String("https://") + JARVIS_HOST + ":" + JARVIS_PORT + "/toy/talk";
  NetworkClientSecure &link = talkLink;
#endif

  if (!https.begin(link, url)) {
    Serial.println("[jarvis] talk: could not start the request");
    return -1;
  }

  https.setTimeout(TALK_ANSWER_MS);
  https.addHeader("Content-Type", "audio/wav");
#ifdef JARVIS_TOY_CLOUD
  https.addHeader("X-Jarvis-Token", cloud ? TOY_CLOUD_TOKEN : JARVIS_TOKEN);

  if (cloud && !toyCloudMode) {
    https.addHeader("X-Jarvis-Fallback", "pc");
  }
#else
  https.addHeader("X-Jarvis-Token", JARVIS_TOKEN);
#endif
  https.addHeader("X-Jarvis-Camera", SENSOR_NAME);
  // "wake": "Jarvis" was heard on this chip, perhaps just before the recording began, so the name
  // may not be in what is sent: JARVIS and the cloud take it as his without looking for it again.
  https.addHeader("X-Jarvis-Toy", button ? "button" : (named ? "wake" : "sound"));
  const char *wanted[] = {"X-Jarvis-Mood", "X-Jarvis-Switch", "X-Jarvis-Faces"};
  https.collectHeaders(wanted, 3);

  unsigned long sent = millis();
  int code = https.POST(talkBuffer, WAV_HEADER + pcm);

  if (code == 204) {
    https.end();
    return 0;
  }

#ifdef JARVIS_TOY_CLOUD
  if (!cloud && code < 0) {
    // The PC went away: this question goes to the cloud, and so does what follows until it is back.
    https.end();
    pcReachable = false;
    Serial.println("[jarvis] talk: the PC did not answer; asking the cloud");
    return askAndPlay(pcm, button, named, true);
  }
#endif

  if (code != HTTP_CODE_OK) {
    Serial.printf("[jarvis] talk: JARVIS answered %d (%u bytes of internal memory free, largest %u)\n", code,
                  (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
                  (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL));
    https.end();
    return -1;
  }

  lastDelivered = millis();
#ifdef JARVIS_TOY_CLOUD
  Serial.printf("[jarvis] talk: answered in %lu ms (%s)\n", millis() - sent, cloud ? "cloud" : "PC");
#else
  Serial.printf("[jarvis] talk: answered in %lu ms\n", millis() - sent);
#endif

#ifdef JARVIS_TOY_CLOUD
  // "Switch to PC mode" / "... cloud mode": kept, and said in the answer playing now.
  String switched = https.header("X-Jarvis-Switch");

  if (switched == "cloud" || switched == "pc") {
    saveToyMode(switched == "cloud");
  }

  // "Learn my face": the faces it asks for, looked for once "look at me" has been said, so he is
  // facing it and the ring can breathe white while it learns.
  int facesWanted = https.header("X-Jarvis-Faces").toInt();
  bool fond = https.header("X-Jarvis-Mood") == "affection";
  int played = playAnswer(https, fond);

  if (facesWanted > 0) {
    toyLearnUntil = millis() + TOY_LEARN_MS;
    toyFacesWanted = facesWanted > 10 ? 10 : facesWanted;
    Serial.printf("[jarvis] face: %d wanted, to learn\n", (int)toyFacesWanted);
  }

  return played;
#else
  // Something warm ("I love you"): his words pulse red instead of green.
  return playAnswer(https, https.header("X-Jarvis-Mood") == "affection");
#endif
}

// Plays an answer as it arrives (raw 16-bit PCM at TALK_RATE), the ring pulsing with it; 1 if
// anything was played, else 0. Ends the request.
static int playAnswer(HTTPClient &https, bool fond) {
  NetworkClient *answer = https.getStreamPtr();
  int remaining = https.getSize();          // -1: until the connection closes
  uint8_t chunk[1024];
  size_t played = 0;
  size_t taken = 0;
  bool odd = false;
  uint8_t carry = 0;
  unsigned long quiet = millis();

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

    // How loud this piece is, for the ring to pulse with: the samples' mean size, over a loud voice's.
    uint32_t loud = 0;

    for (size_t i = 0; i + 1 < have; i += 2) {
      loud += abs((int16_t)(chunk[i] | (chunk[i + 1] << 8)));
    }

    float level = have >= 2 ? (float)loud / (have / 2) / 6000.0f : 0.0f;
    toyVoice = level > 1.0f ? 1.0f : level;
    toyMood = fond ? TOY_SPEAKING_FOND : TOY_SPEAKING;
    taken += talkSpeaker.write(chunk, have);
    played += have;
  }

  https.end();
  // What the speaker really took, beside what came: the two differ only if the speaker refused it.
  Serial.printf("[jarvis] talk: %u ms of answer played (the speaker took %u of %u bytes)\n",
                (unsigned)(played * 1000UL / (TALK_RATE * 2)), (unsigned)taken, (unsigned)played);
  return played > 0 ? 1 : 0;
}

#ifdef JARVIS_TOY_CLOUD
// What the toy's camera has seen, said on its speaker: the cloud's words (POST /toy/announce), when
// /camera/seen said there were some. Asked between recordings, so it never talks over anyone.
static void sayWhatWasSeen() {
  toyAnnounceDue = false;

  HTTPClient https;

  if (!https.begin(cloudTalkLink, cloudUrl("/toy/announce"))) {
    return;
  }

  https.setTimeout(TALK_ANSWER_MS);
  https.addHeader("X-Jarvis-Token", TOY_CLOUD_TOKEN);
  https.addHeader("X-Jarvis-Camera", SENSOR_NAME);
  int code = https.POST((uint8_t *)nullptr, 0);

  if (code != HTTP_CODE_OK) {
    https.end();
    return;
  }

  lastDelivered = millis();
  playAnswer(https, false);
  toyMood = TOY_IDLE;
  letGo(TALK_AFTER_ANSWER_MS);
}
#endif

void talkTask(void *) {
  const size_t most = TALK_MOST_MS * TALK_RATE * 2 / 1000;
  int16_t frame[TALK_FRAME];
  float quietLevel = TALK_LEAST_LEVEL;
  int prerollAt = 0;          // the oldest frame in the ring
  int prerollCount = 0;
  int loudRun = 0;
  unsigned long followUntil = 0;

  for (;;) {
    float level = readFrame(frame);
    listenForWake(frame);
    bool button = digitalRead(TALK_BUTTON_PIN) == LOW;
    float trigger = quietLevel * TALK_START_TIMES;

    if (trigger < TALK_LEAST_LEVEL) {
      trigger = TALK_LEAST_LEVEL;
    }

    // The ring of the last half second, so the first word is not cut.
    memcpy(talkPreroll + prerollAt * TALK_FRAME, frame, sizeof(frame));
    prerollAt = (prerollAt + 1) % TALK_PREROLL_FRAMES;

    if (prerollCount < TALK_PREROLL_FRAMES) {
      prerollCount++;
    }

    loudRun = level > trigger ? loudRun + 1 : 0;

    if (!button && loudRun < TALK_START_FRAMES) {
      // Nobody speaking: learn the room's quiet, slowly, from quiet frames only.
      if (level < trigger) {
        quietLevel = quietLevel * 0.98f + level * 0.02f;
      }

      // Now and then, what it is hearing: the numbers to tune it by.
      static unsigned long lastSaid = 0;

      if (millis() - lastSaid >= 60000UL) {
        lastSaid = millis();
        Serial.printf("[jarvis] talk: listening; the room's quiet %.0f, speech starts above %.0f\n", quietLevel,
                      trigger);
      }

#ifdef JARVIS_TOY_CLOUD
      if (toyAnnounceDue && WiFi.status() == WL_CONNECTED) {
        sayWhatWasSeen();
        loudRun = 0;
        prerollCount = 0;
      }
#endif

      continue;
    }

    if (WiFi.status() != WL_CONNECTED) {
      loudRun = 0;
      continue;
    }

    // Recording: the half second before, then on until the quiet comes back (or the button is let go).
    size_t got = 0;

    for (int i = 0; i < prerollCount; i++) {
      int index = (prerollAt + TALK_PREROLL_FRAMES - prerollCount + i) % TALK_PREROLL_FRAMES;
      memcpy(talkBuffer + WAV_HEADER + got, talkPreroll + index * TALK_FRAME, sizeof(frame));
      got += sizeof(frame);
    }

    int quietRun = 0;
    float loudest = level;
    unsigned long began = millis();
    wakeLoudest = 0;
    digitalWrite(TALK_LED_PIN, HIGH);
    toyMood = TOY_LISTENING;

    while (got + sizeof(frame) <= most) {
      level = readFrame(frame);
      listenForWake(frame);
      memcpy(talkBuffer + WAV_HEADER + got, frame, sizeof(frame));
      got += sizeof(frame);

      if (level > loudest) {
        loudest = level;
      }

      if (button) {
        if (digitalRead(TALK_BUTTON_PIN) != LOW) {
          break;
        }
      } else {
        quietRun = level < quietLevel * TALK_END_TIMES ? quietRun + 1 : 0;

        if (quietRun >= TALK_END_FRAMES) {
          break;
        }
      }
    }

    digitalWrite(TALK_LED_PIN, LOW);
    unsigned long heard = millis() - began;
    loudRun = 0;
    prerollCount = 0;

    if (heard < TALK_LEAST_MS) {
      toyMood = TOY_IDLE;
      continue;
    }

    // Not said to Jarvis: it stays here, and is forgotten.
    bool named = wakeEverHeard && (long)(wakeHeardAt + WAKE_BEFORE_MS - began) >= 0;
    bool following = (long)(followUntil - millis()) > 0;

    if (wakeModel && !button && !named && !following) {
      Serial.printf("[jarvis] talk: %lu ms heard, no \"Jarvis\": kept on the toy, nothing sent (the loudest the wake "
                    "word heard %d of 32767)\n", heard, wakeLoudest);
      toyMood = TOY_IDLE;
      continue;
    }

    toyMood = TOY_THINKING;

    Serial.printf("[jarvis] talk: %lu ms heard%s (the room's quiet %.0f, the loudest %.0f)\n", heard,
                  button ? " with BOOT held" : (named ? ", Jarvis heard" : (following ? ", a follow-up" : "")),
                  quietLevel, loudest);
    unsigned long asked = millis();
    int answered = askAndPlay(got, button, named);
    toyMood = TOY_IDLE;
    followUntil = answered > 0 ? asked + TOY_FOLLOW_MS : 0;

    if (answered < 0) {
      blinkTalkLed(3);
    }

    // Its own answer is not for it to hear; nor a button still held.
    while (digitalRead(TALK_BUTTON_PIN) == LOW) {
      delay(20);
    }

    letGo(TALK_AFTER_ANSWER_MS);
  }
}

// Two short notes on the toy's speaker when it is ready to listen: heard, the speaker works; not heard,
// its plug is not right home in the SPK socket. Answers had come back and been "played" with nothing
// heard (29 October 2026), and this says which end to look at.
static void chime() {
  static const int NOTE_MS = 120;
  static const float NOTES[] = {880.0f, 1320.0f};
  int16_t piece[TALK_FRAME];
  size_t taken = 0, sent = 0;

  for (float note : NOTES) {
    int samples = TALK_RATE * NOTE_MS / 1000;

    for (int done = 0; done < samples; done += TALK_FRAME) {
      int count = samples - done < TALK_FRAME ? samples - done : TALK_FRAME;

      for (int i = 0; i < count; i++) {
        piece[i] = (int16_t)(6000.0f * sinf(2.0f * PI * note * (done + i) / TALK_RATE));
      }

      taken += talkSpeaker.write((uint8_t *)piece, count * 2);
      sent += count * 2;
    }
  }

  Serial.printf("[jarvis] talk: chimed (the speaker took %u of %u bytes)\n", (unsigned)taken, (unsigned)sent);
}

// The toy's body: its light ring (WS2812 LEDs, data on Gravity 43) shows what JARVIS is doing, and
// its servo (signal on Gravity 44) moves its head. Both powered from the power bank's 5 V on the
// breadboard, never from the board; their ground joined to the board's.
//   hearing you: blue, steady        waiting for JARVIS: amber, breathing
//   speaking: green, pulsing with his voice, the head nodding        something warm: red, pulsing
//   otherwise: off
// The colours are the HUD's (hud.py's palette): amber thinking, as on the desk.
// The head turns to whoever detection sees (toySawPeople), and back to the middle when nobody has
// been seen for a while.
static const int TOY_RING_PIN = 43;
static const int TOY_SERVO_PIN = 44;
static const int TOY_RING_LEDS = 16;           // two rings of eight, chained; one ring ignores the rest
static const uint8_t TOY_BRIGHT = 60;          // of 255: bright enough, and kind to the power bank
static const int TOY_CENTRE = 90;              // degrees
static const int TOY_NOD = 12;                 // degrees either side while speaking
static const int TOY_HEAD_LEAST = 30;          // the furthest it turns either way
static const int TOY_HEAD_MOST = 150;
static const float TOY_VIEW = 90.0f;           // degrees from the picture's left edge to its right
static const float TOY_FOLLOW = 0.6f;          // of the way to a person each look: steady, never past them
static const float TOY_STILL = 0.06f;          // a person this near the middle is faced already
static const unsigned long TOY_GONE_MS = 30000UL;   // nobody seen this long: back to the middle
// The servo's own timer and channel, set directly: the camera's clock is LEDC timer 0, channel 0,
// set by its driver where Arduino's ledcAttach cannot see it -- ledcAttach would take timer 0 for
// itself at 50 Hz and stop the camera.
static const ledc_timer_t TOY_SERVO_TIMER = LEDC_TIMER_3;
static const ledc_channel_t TOY_SERVO_CHANNEL = LEDC_CHANNEL_7;
static const int TOY_SERVO_BITS = 14;

static bool toyRingReady = false;
static rmt_data_t toyBits[TOY_RING_LEDS * 24];

static void toyRing(uint8_t red, uint8_t green, uint8_t blue) {
  if (!toyRingReady) {
    return;
  }

  // WS2812: green, red, blue, highest bit first; at 10 MHz, a 1 is 0.8 us high then 0.4 low, a 0 the reverse.
  int at = 0;

  for (int led = 0; led < TOY_RING_LEDS; led++) {
    uint32_t colour = ((uint32_t)green << 16) | ((uint32_t)red << 8) | blue;

    for (int bit = 23; bit >= 0; bit--) {
      bool one = colour & (1UL << bit);
      toyBits[at].level0 = 1;
      toyBits[at].duration0 = one ? 8 : 4;
      toyBits[at].level1 = 0;
      toyBits[at].duration1 = one ? 4 : 8;
      at++;
    }
  }

  rmtWrite(TOY_RING_PIN, toyBits, at, 100);
}

static void toyServo(int degrees) {
  degrees = degrees < 0 ? 0 : (degrees > 180 ? 180 : degrees);
  // SG90: 0.5 ms is 0 degrees, 2.5 ms is 180, every 20 ms.
  uint32_t micros = 500 + (uint32_t)degrees * 2000 / 180;
  ledc_set_duty(LEDC_LOW_SPEED_MODE, TOY_SERVO_CHANNEL, micros * (1UL << TOY_SERVO_BITS) / 20000);
  ledc_update_duty(LEDC_LOW_SPEED_MODE, TOY_SERVO_CHANNEL);
}

// Where the head should face, and where it is: the head walks there a little each frame.
static volatile float toyHeadAim = TOY_CENTRE;
static float toyHeadNow = TOY_CENTRE;
static volatile unsigned long toyLastSeen = 0;

// Which way the servo turns the picture: +1 or -1. Learnt: a turn that leaves the person further
// from the middle, twice running, was the wrong way, and it turns the other way from then on.
static int toyTurnSign = 1;
static float toyLastError = 0.0f;
static bool toyLastTurned = false;
static int toyWrongWay = 0;

void toySawPeople(const Seen *seen, int count) {
  int best = -1;
  float biggest = 0.0f;

  // The nearest person: the largest box.
  for (int i = 0; i < count; i++) {
    float area = (seen[i].x2 - seen[i].x1) * (seen[i].y2 - seen[i].y1);

    if (seen[i].label == 0 && area > biggest) {
      biggest = area;
      best = i;
    }
  }

  if (best < 0) {
    toyLastTurned = false;
    return;
  }

  toyLastSeen = millis();
  float error = (seen[best].x1 + seen[best].x2) / 2.0f - 0.5f;   // -0.5 left edge, +0.5 right edge

  if (toyLastTurned) {
    bool worse = (error > 0) == (toyLastError > 0) && fabsf(error) > fabsf(toyLastError) + 0.05f;
    toyWrongWay = worse ? toyWrongWay + 1 : 0;

    if (toyWrongWay >= 2) {
      toyTurnSign = -toyTurnSign;
      toyWrongWay = 0;
      Serial.println("[jarvis] toy: the head was turning away; it turns the other way now");
    }
  }

  toyLastError = error;
  toyLastTurned = fabsf(error) >= TOY_STILL;

  if (toyLastTurned) {
    float aim = toyHeadAim - toyTurnSign * error * TOY_VIEW * TOY_FOLLOW;
    toyHeadAim = aim < TOY_HEAD_LEAST ? TOY_HEAD_LEAST : (aim > TOY_HEAD_MOST ? TOY_HEAD_MOST : aim);
  }
}

static uint8_t toyScale(int colour, float amount) {
  return (uint8_t)(colour * amount * TOY_BRIGHT / 255.0f);
}

void toyBodyTask(void *) {
  unsigned long since = millis();
  int shown = -1;
  float voice = 0.0f;

  for (;;) {
    int mood = toyMood;
    unsigned long now = millis();

    if (mood != shown) {
      shown = mood;
      since = now;
    }

    // The ring. Breathing is a slow rise and fall; speaking follows his voice, quick up, slower down.
    float breath = 0.5f + 0.5f * sinf(2.0f * PI * (now - since) / 1400.0f);
    float heard = toyVoice;
    voice = heard > voice ? voice + (heard - voice) * 0.6f : voice + (heard - voice) * 0.15f;
    float pulse = 0.15f + 0.85f * (voice > 1.0f ? 1.0f : voice);

    if (mood == TOY_LISTENING) {
      toyRing(0, 0, TOY_BRIGHT);
    } else if (mood == TOY_THINKING) {
      float amount = 0.15f + 0.85f * breath;
      toyRing(toyScale(255, amount), toyScale(180, amount), toyScale(40, amount));
    } else if (mood == TOY_SPEAKING) {
      toyRing(0, toyScale(255, pulse), 0);
    } else if (mood == TOY_SPEAKING_FOND) {
      toyRing(toyScale(255, pulse), 0, toyScale(30, pulse));
#ifdef JARVIS_TOY_CLOUD
    } else if (toyFacesWanted > 0) {
      // Learning a face: a slow white breath until it has enough, so he knows to keep still.
      float amount = 0.15f + 0.85f * breath;
      toyRing(toyScale(255, amount), toyScale(255, amount), toyScale(255, amount));
#endif
    } else {
      toyRing(0, 0, 0);
      toyVoice = 0.0f;
    }

    // The head: back to the middle when nobody has been seen for a while, then a little way each
    // frame toward where it should face, nodding about it while he speaks.
    if (toyLastSeen && now - toyLastSeen > TOY_GONE_MS) {
      toyHeadAim = TOY_CENTRE;
      toyLastSeen = 0;
    }

    float gap = toyHeadAim - toyHeadNow;
    toyHeadNow += gap > 2.0f ? 2.0f : (gap < -2.0f ? -2.0f : gap);
    float nod = 0.0f;

    if (mood == TOY_SPEAKING || mood == TOY_SPEAKING_FOND) {
      nod = TOY_NOD * sinf(2.0f * PI * (now - since) / 1200.0f);
    }

    toyServo((int)(toyHeadNow + nod));
    delay(30);
  }
}

void startToyBody() {
  toyRingReady = rmtInit(TOY_RING_PIN, RMT_TX_MODE, RMT_MEM_NUM_BLOCKS_2, 10000000);

  if (!toyRingReady) {
    Serial.println("[jarvis] toy: the light ring could not be started");
  }

  ledc_timer_config_t timer = {};
  timer.speed_mode = LEDC_LOW_SPEED_MODE;
  timer.timer_num = TOY_SERVO_TIMER;
  timer.duty_resolution = (ledc_timer_bit_t)TOY_SERVO_BITS;
  timer.freq_hz = 50;
  timer.clk_cfg = LEDC_AUTO_CLK;

  ledc_channel_config_t channel = {};
  channel.speed_mode = LEDC_LOW_SPEED_MODE;
  channel.channel = TOY_SERVO_CHANNEL;
  channel.timer_sel = TOY_SERVO_TIMER;
  channel.gpio_num = TOY_SERVO_PIN;
  channel.duty = 0;

  bool servo = ledc_timer_config(&timer) == ESP_OK && ledc_channel_config(&channel) == ESP_OK;

  if (!servo) {
    Serial.println("[jarvis] toy: the servo could not be started");
  }

  // Hello: the ring white for a moment, the head looking left, right, and back to the middle.
  toyRing(TOY_BRIGHT / 2, TOY_BRIGHT / 2, TOY_BRIGHT / 2);

  if (servo) {
    toyServo(TOY_CENTRE - 30);
    delay(400);
    toyServo(TOY_CENTRE + 30);
    delay(400);
    toyServo(TOY_CENTRE);
    delay(300);
  }

  toyRing(0, 0, 0);
  xTaskCreatePinnedToCore(toyBodyTask, "toy body", 4096, nullptr, 2, nullptr, 1);
  Serial.println("[jarvis] toy: light ring on 43, servo on 44");
}

// mbedTLS's memory, the larger pieces from PSRAM. Each TLS connection takes some 40 KB, from internal
// RAM in the Arduino build; with the polls, live view, detection's reports and the toy's talk all
// connected at once that ran out -- "SSL - Memory allocation failed" (29 October 2026). Small pieces
// stay internal, where they are fastest; a piece PSRAM cannot give comes from internal RAM.
static void *tlsCalloc(size_t count, size_t size) {
  size_t bytes = count * size;
  void *piece = nullptr;

  if (bytes >= 1024) {
    piece = heap_caps_calloc(count, size, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  }

  return piece ? piece : heap_caps_calloc(count, size, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
}

static void tlsFree(void *piece) {
  heap_caps_free(piece);
}

void talkTlsInPsram() {
  mbedtls_platform_set_calloc_free(tlsCalloc, tlsFree);
}

void startTalk() {
  pinMode(TALK_BUTTON_PIN, INPUT_PULLUP);
  pinMode(TALK_LED_PIN, OUTPUT);
  digitalWrite(TALK_LED_PIN, LOW);

  talkBuffer = (uint8_t *)heap_caps_malloc(WAV_HEADER + TALK_MOST_MS * TALK_RATE * 2 / 1000,
                                           MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  talkPreroll = (int16_t *)heap_caps_malloc(TALK_PREROLL_FRAMES * TALK_FRAME * 2, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  talkMic.setPinsPdmRx(TALK_MIC_CLOCK_PIN, TALK_MIC_DATA_PIN);
  talkSpeaker.setPins(TALK_SPEAKER_BCLK_PIN, TALK_SPEAKER_LRC_PIN, TALK_SPEAKER_DOUT_PIN);

  if (!talkBuffer || !talkPreroll) {
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
#ifdef JARVIS_TOY_CLOUD
  // The cloud's own signers (toy_cloud_ca.h): Google's and Let's Encrypt's roots, checked like JARVIS's.
  cloudTalkLink.setCACert(TOY_CLOUD_CA);
  cloudTalkLink.setHandshakeTimeout(HANDSHAKE_SECONDS);
#endif
  chime();
  startWake();

  if (xTaskCreatePinnedToCore(talkTask, "talk", 12288, nullptr, 1, nullptr, 1) != pdPASS) {
    Serial.println("[jarvis] talk: could not start");
    return;
  }

  Serial.printf("[jarvis] talk: listening -- say \"Jarvis, ...\" (%u bytes of internal memory free)\n",
                (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
}
#endif

void setup() {
  Serial.begin(115200);
  delay(200);

#if defined(JARVIS_BOARD_DFR1154)
  // Before any connection is made: its memory and its freeing must come from the same place.
  talkTlsInPsram();
#endif

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
#ifdef JARVIS_TOY_CLOUD
  cloudLink.setCACert(TOY_CLOUD_CA);
  cloudLink.setHandshakeTimeout(HANDSHAKE_SECONDS);
  loadToyMode();
#endif

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
  startToyBody();
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

#ifdef JARVIS_TOY_CLOUD
  // While the cloud is where the toy talks, it is kept in touch with (and counts as JARVIS reached).
  if (toyUsesCloud() && (lastCloudPing == 0 || now - lastCloudPing >= CLOUD_PING_MS)) {
    lastCloudPing = now;
    cloudPing();
  }

  // Cloud mode asks the PC nothing: pictures and live view are PC mode's.
  if (toyCloudMode) {
    delay(50);
    return;
  }
#endif

  // Online only with a working camera: a camera that cannot see should
  // not be drawn on the plan as if it could.
  if (cameraReady && (!saidOnline || now - lastHeartbeat >= HEARTBEAT_EVERY_MS)) {
    int online = reportOnline();

    if (online == HTTP_CODE_OK) {
      saidOnline = true;
    }

#ifdef JARVIS_TOY_CLOUD
    pcReachable = online == HTTP_CODE_OK;
#endif
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
