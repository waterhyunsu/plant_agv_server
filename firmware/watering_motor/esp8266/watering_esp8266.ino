// =====================================================
// Arduino2 - ESP 8266 - Server => Serial Monitor로 확인하는 코드 
// command 값이 WAIT 면 no task
// command 값이 WATER 명령 받고 led on, off시 completed 상태 전달
//
// 확인용: ESP8266이 보내는 걸 컴퓨터로 그대로 중계
// =====================================================

#include <SoftwareSerial.h>

SoftwareSerial espSerial(2, 3);  // (RX, TX) - 아두이노 기준: D2=RX, D3=TX

const int LED_PIN = 8;
const long SERIAL_BAUD_RATE = 9600;

float MS_PER_ML = 125.0;   // 측정 후 수정 필요

bool isPumpRunning = false;
unsigned long pumpStartTime = 0;
unsigned long pumpTargetDuration = 0;
String currentTaskId = "";

void handleCommand(String line)
{
  if (!line.startsWith("WATER:")) return;
  if (isPumpRunning) return;

  String rest = line.substring(6);

  int firstColon = rest.indexOf(':');
  String taskId = rest.substring(0, firstColon);

  String afterTaskId = rest.substring(firstColon + 1);
  int secondColon = afterTaskId.indexOf(':');

  String amountStr = afterTaskId.substring(secondColon + 1);
  float amountMl = amountStr.toFloat();

  currentTaskId = taskId;
  pumpTargetDuration = (unsigned long)(amountMl * MS_PER_ML);
  pumpStartTime = millis();

  digitalWrite(LED_PIN, HIGH);
  isPumpRunning = true;

  espSerial.print("STATE:");
  espSerial.print(taskId);
  espSerial.println(":WATERING");

  Serial.print("STATE:");   // 컴퓨터에도 동일하게 출력
  Serial.print(taskId);
  Serial.println(":WATERING");
}

void checkSerialCommand()
{
  while (espSerial.available() > 0)
  {
    String line = espSerial.readStringUntil('\n');
    line.trim();
    if (line.length() == 0) continue;

    Serial.println(line);   // ESP8266이 보낸 걸 컴퓨터로 그대로 중계

    handleCommand(line);
  }
}

void checkPumpProgress()
{
  if (!isPumpRunning) return;

  if (millis() - pumpStartTime >= pumpTargetDuration)
  {
    digitalWrite(LED_PIN, LOW);
    isPumpRunning = false;

    espSerial.print("STATE:");
    espSerial.print(currentTaskId);
    espSerial.println(":COMPLETED");

    Serial.print("STATE:");   // 컴퓨터에도 동일하게 출력
    Serial.print(currentTaskId);
    Serial.println(":COMPLETED");
  }
}

void setup()
{
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  Serial.begin(SERIAL_BAUD_RATE);      // 컴퓨터(USB)와 통신용
  espSerial.begin(SERIAL_BAUD_RATE);   // ESP8266과 통신용

  Serial.println("=== Arduino ready ===");
}

void loop()
{
  checkSerialCommand();
  checkPumpProgress();
  yield();
}