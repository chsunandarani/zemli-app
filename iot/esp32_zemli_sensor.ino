/*
  Zemli IoT box: ESP32 + 4G LTE modem (SIM7600 / A7670C family) + soil sensors.
  Every 15 minutes: read soil moisture, air temp/humidity, NPK -> POST to the Zemli server -> deep sleep.

  Hardware (typical, check your modules' datasheets):
    - Capacitive soil moisture sensor v1.2  -> GPIO34 (analog)
    - DHT22 air temperature/humidity        -> GPIO4
    - RS485 NPK soil sensor (Modbus) via MAX485: RO->GPIO16, DI->GPIO17, DE+RE->GPIO25 ; sensor needs 9-24 V
    - 4G modem on Serial1: modem TX->GPIO26, modem RX->GPIO27, PWRKEY->GPIO12 (change to your board's pins)

  Libraries (Arduino Library Manager): TinyGSM, ArduinoHttpClient, DHT sensor library (Adafruit).
  PROTOTYPE CODE: compile-check and calibrate on your own hardware before field use.
*/
#define TINY_GSM_MODEM_SIM7600   // A7670C: try TINY_GSM_MODEM_A7672X if your TinyGSM version has it
#define TINY_GSM_RX_BUFFER 1024
#include <TinyGsmClient.h>
#include <ArduinoHttpClient.h>
#include <DHT.h>

// ---------- settings ----------
const char APN[]        = "iot.com";            // your IoT SIM's APN (ask the SIM provider)
const char SERVER[]     = "app.zemliagritech.in";
const int  PORT         = 443;                  // HTTPS. For a quick local test you can use plain HTTP on 80
const char DEVICE_ID[]  = "ZM-0001";            // from the admin panel -> IoT devices
const char DEVICE_KEY[] = "PASTE-KEY-FROM-ADMIN-PANEL";
const uint64_t SLEEP_MINUTES = 15;

// soil moisture calibration: raw ADC in dry air and in water (measure yours!)
const int SOIL_DRY_RAW = 3300;
const int SOIL_WET_RAW = 1400;

// ---------- pins ----------
#define PIN_SOIL     34
#define PIN_DHT      4
#define PIN_RS485_RX 16
#define PIN_RS485_TX 17
#define PIN_RS485_DE 25
#define PIN_MODEM_RX 26
#define PIN_MODEM_TX 27
#define PIN_MODEM_PWR 12

DHT dht(PIN_DHT, DHT22);
HardwareSerial RS485(2);
TinyGsm modem(Serial1);
TinyGsmClientSecure net(modem);
HttpClient http(net, SERVER, PORT);

// Modbus "read 1 register" frames used by the common 7-in-1 / NPK soil sensors (address 0x01).
// Check your sensor's manual: register numbers differ between models.
const byte Q_N[] = {0x01, 0x03, 0x00, 0x1E, 0x00, 0x01, 0xE4, 0x0C};
const byte Q_P[] = {0x01, 0x03, 0x00, 0x1F, 0x00, 0x01, 0xB5, 0xCC};
const byte Q_K[] = {0x01, 0x03, 0x00, 0x20, 0x00, 0x01, 0x85, 0xC0};

int readNPK(const byte *q) {
  byte buf[7];
  while (RS485.available()) RS485.read();
  digitalWrite(PIN_RS485_DE, HIGH); delay(5);
  RS485.write(q, 8); RS485.flush();
  digitalWrite(PIN_RS485_DE, LOW);
  unsigned long t = millis(); int n = 0;
  while (millis() - t < 400 && n < 7) if (RS485.available()) buf[n++] = RS485.read();
  if (n < 7 || buf[1] != 0x03) return -1;      // no answer
  return (buf[3] << 8) | buf[4];               // mg/kg
}

float soilPercent() {
  long sum = 0; for (int i = 0; i < 10; i++) { sum += analogRead(PIN_SOIL); delay(20); }
  float raw = sum / 10.0;
  float pct = 100.0 * (SOIL_DRY_RAW - raw) / (SOIL_DRY_RAW - SOIL_WET_RAW);
  return constrain(pct, 0, 100);
}

void sleepNow() {
  modem.poweroff();
  esp_sleep_enable_timer_wakeup(SLEEP_MINUTES * 60ULL * 1000000ULL);
  esp_deep_sleep_start();
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_RS485_DE, OUTPUT); digitalWrite(PIN_RS485_DE, LOW);
  RS485.begin(4800, SERIAL_8N1, PIN_RS485_RX, PIN_RS485_TX);
  dht.begin();

  // 1. read sensors
  float soil = soilPercent();
  delay(2000);                                   // DHT22 needs time after power-up
  float temp = dht.readTemperature(), hum = dht.readHumidity();
  int n = readNPK(Q_N); delay(100); int p = readNPK(Q_P); delay(100); int k = readNPK(Q_K);

  String body = "{\"soil_moisture\":" + String(soil, 1);
  if (!isnan(temp)) body += ",\"air_temp\":" + String(temp, 1);
  if (!isnan(hum))  body += ",\"humidity\":" + String(hum, 0);
  if (n >= 0) body += ",\"nitrogen\":" + String(n);
  if (p >= 0) body += ",\"phosphorus\":" + String(p);
  if (k >= 0) body += ",\"potassium\":" + String(k);
  body += "}";
  Serial.println(body);

  // 2. connect 4G and send
  pinMode(PIN_MODEM_PWR, OUTPUT); digitalWrite(PIN_MODEM_PWR, HIGH); delay(1000); digitalWrite(PIN_MODEM_PWR, LOW);
  Serial1.begin(115200, SERIAL_8N1, PIN_MODEM_RX, PIN_MODEM_TX);
  if (!modem.restart() || !modem.waitForNetwork(60000L) || !modem.gprsConnect(APN)) {
    Serial.println("No network, sleeping"); sleepNow();
  }
  http.beginRequest();
  http.post("/api/iot/reading");
  http.sendHeader("Content-Type", "application/json");
  http.sendHeader("X-Device-Id", DEVICE_ID);
  http.sendHeader("X-Device-Key", DEVICE_KEY);
  http.sendHeader("Content-Length", body.length());
  http.beginBody(); http.print(body); http.endRequest();
  Serial.printf("Server replied %d\n", http.responseStatusCode());
  http.stop(); modem.gprsDisconnect();

  // 3. sleep 15 minutes
  sleepNow();
}

void loop() {}
