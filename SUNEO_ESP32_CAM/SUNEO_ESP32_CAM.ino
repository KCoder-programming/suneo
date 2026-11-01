/*
  NETRA / SUNEO smart cap - ESP32-CAM firmware

  Board     : AI Thinker ESP32-CAM  (esp32 core 3.x)
  Tools     : PSRAM = Enabled, Partition Scheme = Huge APP (3MB No OTA)
  Library   : "VL53L0X" by Pololu (Library Manager)

  Wiring
    Button        : GPIO13 -> button -> GND
    Lidar VL53L0X : SDA = GPIO14, SCL = GPIO15, VIN = 5V, GND = GND
    Active buzzer : GPIO2 (+)  ->  GND

  What it does
    - Button clicks : 1 click  = scene description
                      2 clicks = OCR (read text)
                      3 clicks = SOS
                      Takes a photo and POSTs the JPEG to SERVER_URL with
                      headers  X-Click-Type (scene / ocr / sos)  and  X-Device-Id
    - Lidar         : obstacle closer than ALERT_MM => buzzer beeps
                      (closer = faster beeps, very close = continuous)
    - Feedback beeps: N short (1-3) = N clicks detected
                      2 short       = photo sent OK
                      1 long        = failed (wifi / camera / server)

  Note: ESP32 cannot use "localhost". SERVER_URL must be the PC's LAN IP,
        and the Python server must run with host="0.0.0.0".
*/

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <Wire.h>
#include <VL53L0X.h>
#include "esp_camera.h"
#include "img_converters.h"

// ================= USER SETTINGS =================
#include "secrets.h"

const char* WIFI_SSID = SUNEO_WIFI_SSID;
const char* WIFI_PASS = SUNEO_WIFI_PASS;
const char* SERVER_URL = SUNEO_SERVER_URL;
const char* DEVICE_ID  = "cap-01";
const bool  USE_FLASH  = true;   // true = onboard white LED lights up while taking photo
const int   ALERT_MM   = 1000;   // obstacle alert distance in mm
const uint32_t CLICK_WINDOW_MS = 450;  // wait this long after last click for more clicks
// =================================================

#define PIN_BUTTON 13
#define PIN_BUZZER 2
#define PIN_FLASH  4
#define PIN_SDA    14
#define PIN_SCL    15

VL53L0X lidar;
bool lidarOk = false;
bool camOk   = false;

// ---------- buzzer helpers ----------
void beep(int ms) {
  digitalWrite(PIN_BUZZER, HIGH);
  delay(ms);
  digitalWrite(PIN_BUZZER, LOW);
}

void beepN(int n, int ms = 80) {
  for (int i = 0; i < n; i++) {
    beep(ms);
    delay(80);
  }
}

// ---------- camera ----------
bool initCamera(pixformat_t fmt) {
  camera_config_t c = {};
  c.ledc_channel = LEDC_CHANNEL_0;
  c.ledc_timer   = LEDC_TIMER_0;
  c.pin_d0 = 5;   c.pin_d1 = 18;  c.pin_d2 = 19;  c.pin_d3 = 21;
  c.pin_d4 = 36;  c.pin_d5 = 39;  c.pin_d6 = 34;  c.pin_d7 = 35;
  c.pin_xclk  = 0;
  c.pin_pclk  = 22;
  c.pin_vsync = 25;
  c.pin_href  = 23;
  c.pin_sccb_sda = 26;
  c.pin_sccb_scl = 27;
  c.pin_pwdn  = 32;
  c.pin_reset = -1;
  c.xclk_freq_hz = 20000000;
  c.pixel_format = fmt;
  c.jpeg_quality = 12;
  c.grab_mode = CAMERA_GRAB_LATEST;

  if (psramFound()) {
    c.frame_size  = (fmt == PIXFORMAT_JPEG) ? FRAMESIZE_SVGA : FRAMESIZE_VGA;  // 800x600 for JPEG
    c.fb_count    = 2;
    c.fb_location = CAMERA_FB_IN_PSRAM;
  } else {
    c.frame_size  = FRAMESIZE_VGA;
    c.fb_count    = 1;
    c.fb_location = CAMERA_FB_IN_DRAM;
  }

  esp_err_t err = esp_camera_init(&c);
  if (err != ESP_OK) {
    Serial.printf("Camera init failed (0x%x) with format %d\n", err, (int)fmt);
    return false;
  }
  return true;
}

void setupCamera() {
  camOk = initCamera(PIXFORMAT_JPEG);
  if (!camOk) {
    // some cheap sensors have no JPEG engine: fall back to RGB565 and convert in software
    esp_camera_deinit();
    Serial.println("JPEG not supported, trying RGB565 fallback...");
    camOk = initCamera(PIXFORMAT_RGB565);
  }
  if (camOk) {
    sensor_t* s = esp_camera_sensor_get();
    Serial.printf("Camera OK, sensor PID: 0x%x\n", s->id.PID);
  } else {
    Serial.println("CAMERA FAILED - check ribbon cable and PSRAM setting");
  }
}

// ---------- wifi ----------
bool connectWiFi(uint32_t timeoutMs = 15000) {
  if (WiFi.status() == WL_CONNECTED) return true;
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("WiFi connecting");
  uint32_t t = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t < timeoutMs) {
    delay(250);
    Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    WiFi.setSleep(false);
    Serial.print("WiFi OK, IP: ");
    Serial.println(WiFi.localIP());
    return true;
  }
  Serial.println("WiFi failed");
  return false;
}

// ---------- capture + upload ----------
bool captureAndSend(const char* clickType) {
  if (!camOk) {
    Serial.println("Camera not ready");
    return false;
  }
  if (!connectWiFi()) return false;

  if (USE_FLASH) digitalWrite(PIN_FLASH, HIGH);
  camera_fb_t* fb = esp_camera_fb_get();
  if (USE_FLASH) digitalWrite(PIN_FLASH, LOW);

  if (!fb) {
    Serial.println("Capture failed");
    return false;
  }

  uint8_t* jpg = fb->buf;
  size_t len = fb->len;
  bool converted = false;
  if (fb->format != PIXFORMAT_JPEG) {
    if (!frame2jpg(fb, 80, &jpg, &len)) {
      Serial.println("JPEG conversion failed");
      esp_camera_fb_return(fb);
      return false;
    }
    converted = true;
  }

  Serial.printf("Sending %u bytes (%s) to %s\n", (unsigned)len, clickType, SERVER_URL);

  HTTPClient http;
  WiFiClient client;
  WiFiClientSecure sclient;
  if (String(SERVER_URL).startsWith("https")) {
    sclient.setInsecure();               // fine for hackathon demo
    http.begin(sclient, SERVER_URL);
  } else {
    http.begin(client, SERVER_URL);
  }
  http.setTimeout(20000);
  http.addHeader("Content-Type", "image/jpeg");
  http.addHeader("X-Device-Id", DEVICE_ID);
  http.addHeader("X-Click-Type", clickType);   // scene / ocr / sos

  int code = http.POST(jpg, len);
  if (code > 0) {
    Serial.printf("Server replied %d: %s\n", code, http.getString().c_str());
  } else {
    Serial.printf("POST failed: %s\n", http.errorToString(code).c_str());
  }
  http.end();

  if (converted) free(jpg);
  esp_camera_fb_return(fb);

  return code >= 200 && code < 300;
}

// ---------- button: 1 click = scene, 2 = ocr, 3 = sos ----------
void onClicks(int n) {
  const char* type;
  if (n == 1)      type = "scene";
  else if (n == 2) type = "ocr";
  else             type = "sos";      // 3 or more

  Serial.printf("Clicks: %d -> %s\n", n, type);
  digitalWrite(PIN_BUZZER, LOW);
  beepN(n > 3 ? 3 : n, 60);           // confirm how many clicks were detected
  delay(200);
  bool ok = captureAndSend(type);
  delay(100);
  if (ok) beepN(2, 80);
  else    beep(500);
}

void handleButton() {
  static bool prev = HIGH;
  static uint32_t tChange = 0;        // debounce
  static uint32_t lastPress = 0;
  static int clicks = 0;

  bool cur = digitalRead(PIN_BUTTON);
  uint32_t now = millis();

  if (cur != prev && now - tChange > 40) {
    tChange = now;
    prev = cur;
    if (cur == LOW) {                 // button pressed
      clicks++;
      lastPress = now;
    }
  }
  // button released and click window over => clicks are final
  if (clicks > 0 && cur == HIGH && now - lastPress > CLICK_WINDOW_MS) {
    int n = clicks;
    clicks = 0;
    onClicks(n);
  }
}

// ---------- lidar obstacle alert ----------
void handleLidar() {
  static uint32_t lastChange = 0;
  static bool buzzOn = false;
  if (!lidarOk) return;

  uint16_t mm = lidar.readRangeContinuousMillimeters();
  if (lidar.timeoutOccurred() || mm > ALERT_MM) {
    digitalWrite(PIN_BUZZER, LOW);
    buzzOn = false;
    return;
  }
  if (mm < 150) {                    // very close: continuous tone
    digitalWrite(PIN_BUZZER, HIGH);
    buzzOn = true;
    return;
  }

  uint32_t gap = map(mm, 150, ALERT_MM, 60, 700);   // pause between beeps
  uint32_t now = millis();
  if (buzzOn && now - lastChange >= 40) {
    digitalWrite(PIN_BUZZER, LOW);
    buzzOn = false;
    lastChange = now;
  } else if (!buzzOn && now - lastChange >= gap) {
    digitalWrite(PIN_BUZZER, HIGH);
    buzzOn = true;
    lastChange = now;
  }
}

// ---------- main ----------
void setup() {
  Serial.begin(115200);
  Serial.println("\nSUNEO cap starting...");

  pinMode(PIN_BUZZER, OUTPUT);
  digitalWrite(PIN_BUZZER, LOW);
  pinMode(PIN_FLASH, OUTPUT);
  digitalWrite(PIN_FLASH, LOW);
  pinMode(PIN_BUTTON, INPUT_PULLUP);

  setupCamera();                     // camera first, then our own I2C

  Wire.begin(PIN_SDA, PIN_SCL);
  lidar.setTimeout(500);
  if (lidar.init()) {
    lidar.startContinuous();
    lidarOk = true;
    Serial.println("Lidar OK");
  } else {
    Serial.println("Lidar NOT found - check SDA/SCL/VIN wiring");
  }

  connectWiFi();

  if (camOk && lidarOk) beepN(2, 60);   // ready
  else                  beep(800);      // something failed, see Serial Monitor
}

void loop() {
  handleButton();
  handleLidar();
}
