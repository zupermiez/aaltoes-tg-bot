#include <WiFi.h>
#include <WebServer.h>

#define DOOR_PIN 13
#define RGB_BUILTIN 48
#define RGB_BRIGHTNESS 25  // 0–255

const char* ssid = "xxxxxxx";
const char* password = "xxxxxxxxxx";


IPAddress local_IP(192, 168, 1, 184);
IPAddress gateway(192, 168, 1, 1);
IPAddress subnet(255, 255, 0, 0);

WebServer server(80);

void handleDoor() {
  for (int i = 0; i < 3; i++) {
    digitalWrite(DOOR_PIN, HIGH);  
    delay(1000);
    digitalWrite(DOOR_PIN, LOW);
    delay(1000);
  }
  server.send(200, "text/plain", "The light should be blinking now, lets hope someone comes to let you in.");
}

void setup() {
  pinMode(DOOR_PIN, OUTPUT);
  digitalWrite(DOOR_PIN, LOW);
  Serial.begin(115200);

  if (!WiFi.config(local_IP, gateway, subnet)) {
    Serial.println("STA Failed to configure");
  }

  WiFi.begin(ssid, password);


  while (WiFi.status() != WL_CONNECTED) {
    Serial.print(".");
    neopixelWrite(RGB_BUILTIN, RGB_BRIGHTNESS, 0, 0);  // red
    delay(500);
    neopixelWrite(RGB_BUILTIN, 0, 0, 0);  // off
    delay(500);
  }

  Serial.println(WiFi.localIP());

  server.on("/door", handleDoor);
  server.begin();

  neopixelWrite(RGB_BUILTIN, 0, RGB_BRIGHTNESS, 0);
  delay(200);
  neopixelWrite(RGB_BUILTIN, 0, 0, 0);


}

void loop() {
  server.handleClient();
}