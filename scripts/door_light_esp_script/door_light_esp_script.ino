#include <WiFi.h>
#include <WebServer.h>

const char* ssid = "xxxxxxxxx";
const char* password = "xxxxxxxxxxxxxxxxx";

#define DOOR_PIN 13
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
  Serial.println("Failed to setup static IP");
}

  WiFi.begin(ssid, password);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println(WiFi.localIP());

  server.on("/door", handleDoor);
  server.begin();
}

void loop() {
  server.handleClient();
}
