// JARVIS room sensor: an ESP32 (WROOM-32, or C3 Super Mini) with a DHT22 and a PIR.
//
// It reports to JARVIS over HTTPS, checking JARVIS's certificate against
// JARVIS's own certificate authority (setCACert) -- never setInsecure(),
// which would let anything on the wifi pretend to be JARVIS.
//
//   - "online" once, when it joins the wifi;
//   - temperature and humidity every 30 seconds;
//   - "motion" the moment the PIR sees someone (at most every 5 seconds).
//
// JARVIS decides what is worth saying (sensors.py): a board coming online,
// movement in an empty room. The board just reports.
//
// Before uploading:
//   1. python tools/esp32_setup.py        (writes jarvis_config.h)
//   2. put your wifi in jarvis_secrets.h
//   3. Arduino IDE: esp32 board package 3.1 or later, and the library
//      "DHT sensor library" by Adafruit (Library Manager). Board:
//        WROOM-32 DevKit   "ESP32 Dev Module"
//        C3 Super Mini     "ESP32C3 Dev Module", with Tools > USB CDC On Boot
//                          set to Enabled, or the Serial Monitor stays blank
//      Give each board its own SENSOR_NAME in jarvis_secrets.h before uploading.
//
// The pins are picked for the board it is built for (below), and either
// can be changed by defining DHT_PIN or PIR_PIN in jarvis_secrets.h.
//
// Wiring, WROOM-32 DevKit:
//   DHT22   + to 3V3,  - to GND,  out to GPIO 4   (a bare DHT22 needs a
//                                                   10k resistor from out to 3V3;
//                                                   a module has one on board)
//   PIR     VCC to VIN (5V),  GND to GND,  OUT to GPIO 27
//
// Wiring, C3 Super Mini (GPIO 2, 8 and 9 are avoided: they decide how the
// board starts, and 8 drives the on-board LED):
//   DHT22   + to 3V3,  - to GND,  out to GPIO 4
//   PIR     VCC to 5V, GND to GND,  OUT to GPIO 3
//
// The PIR's output is 3.3V, safe for either board. It needs a minute to
// settle after power-up before it reports reliably. With no PIR connected
// yet, the pin is held low, so a bare board reports no phantom movement.

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <NetworkClientSecure.h>
#include <DHT.h>

#include "jarvis_config.h"
#include "jarvis_secrets.h"

#if defined(CONFIG_IDF_TARGET_ESP32C3)
  #define JARVIS_BOARD "C3 Super Mini"
  #ifndef DHT_PIN
    #define DHT_PIN 4
  #endif
  #ifndef PIR_PIN
    #define PIR_PIN 3
  #endif
#else
  #define JARVIS_BOARD "WROOM-32"
  #ifndef DHT_PIN
    #define DHT_PIN 4
  #endif
  #ifndef PIR_PIN
    #define PIR_PIN 27
  #endif
#endif

static const unsigned long READING_EVERY_MS = 30UL * 1000UL;
static const unsigned long MOTION_GAP_MS = 5UL * 1000UL;
static const unsigned long WIFI_RETRY_MS = 10UL * 1000UL;

DHT dht(DHT_PIN, DHT22);
NetworkClientSecure secure;

unsigned long lastReading = 0;
unsigned long lastMotion = 0;
bool wasMoving = false;
bool saidOnline = false;

// Post one JSON report to JARVIS. Returns the HTTP status, or a negative
// HTTPClient error, which is printed with its meaning.
int report(const String &json) {
  HTTPClient https;
  String url = String("https://") + JARVIS_HOST + ":" + JARVIS_PORT + "/sensor";

  if (!https.begin(secure, url)) {
    Serial.println("[jarvis] could not start the request");
    return -1;
  }

  https.setTimeout(8000);
  https.addHeader("Content-Type", "application/json");
  https.addHeader("X-Jarvis-Token", JARVIS_TOKEN);

  int code = https.POST(json);

  if (code == HTTP_CODE_OK) {
    Serial.println("[jarvis] " + json + " -> " + https.getString());
  } else if (code == 403) {
    Serial.println("[jarvis] refused: the token in jarvis_secrets.h is not JARVIS's");
  } else if (code < 0) {
    // A certificate problem shows here, as a connection failure.
    Serial.println("[jarvis] no connection: " + HTTPClient::errorToString(code) +
                   " (JARVIS running? same wifi? run tools/esp32_setup.py again?)");
  } else {
    Serial.println("[jarvis] JARVIS answered " + String(code));
  }

  https.end();
  return code;
}

String quoted(const char *text) {
  return String("\"") + text + "\"";
}

void reportEvent(const char *event) {
  report(String("{\"name\":") + quoted(SENSOR_NAME) + ",\"event\":" + quoted(event) + "}");
}

void reportReadings() {
  float temperature = dht.readTemperature();
  float humidity = dht.readHumidity();

  // A DHT22 misses a reading now and then; a missed one is left out, not sent as nonsense.
  String json = String("{\"name\":") + quoted(SENSOR_NAME);

  if (!isnan(temperature)) {
    json += ",\"temperature\":" + String(temperature, 1);
  }

  if (!isnan(humidity)) {
    json += ",\"humidity\":" + String(humidity, 0);
  }

  if (isnan(temperature) && isnan(humidity)) {
    Serial.println("[jarvis] the DHT22 gave no reading (check its wiring)");
    return;
  }

  report(json + "}");
}

bool joinWifi() {
  if (WiFi.status() == WL_CONNECTED) {
    return true;
  }

  Serial.print("[jarvis] joining wifi ");
  Serial.println(WIFI_SSID);

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

#if defined(CONFIG_IDF_TARGET_ESP32C3)
  // Many C3 Super Minis cannot join a network at full transmit power (the
  // tiny antenna and its matching are to blame), and simply never connect.
  // A lower power is the well-known cure, and in one room costs nothing.
  WiFi.setTxPower(WIFI_POWER_8_5dBm);
#endif

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

  // Held low when nothing drives it: an unconnected pin floats and reads
  // noise as movement. The PIR's own output overrides the weak pull-down.
  pinMode(PIR_PIN, INPUT_PULLDOWN);
  dht.begin();

  Serial.print("[jarvis] ");
  Serial.print(JARVIS_BOARD);
  Serial.print(": DHT22 on GPIO ");
  Serial.print(DHT_PIN);
  Serial.print(", PIR on GPIO ");
  Serial.println(PIR_PIN);

  // JARVIS's own authority: the board accepts JARVIS and nothing else.
  secure.setCACert(JARVIS_CA);

  WiFi.setAutoReconnect(true);
  joinWifi();
}

void loop() {
  if (!joinWifi()) {
    delay(1000);
    return;
  }

  unsigned long now = millis();

  if (!saidOnline) {
    reportEvent("online");
    saidOnline = true;
    lastReading = 0;
  }

  // Movement is reported as it starts, not for as long as it lasts.
  bool moving = digitalRead(PIR_PIN) == HIGH;

  if (moving && !wasMoving && (lastMotion == 0 || now - lastMotion >= MOTION_GAP_MS)) {
    reportEvent("motion");
    lastMotion = now;
  }

  wasMoving = moving;

  if (lastReading == 0 || now - lastReading >= READING_EVERY_MS) {
    reportReadings();
    lastReading = now;
  }

  delay(50);
}
