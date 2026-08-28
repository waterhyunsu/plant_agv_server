#include <SoftwareSerial.h>

SoftwareSerial espSerial(2, 3); // (RX=2, TX=3)

// =====================================================
// 핀 설정
// =====================================================
const int PIN_STBY = 11;
const int PIN_AIN1 = 7;
const int PIN_AIN2 = 4;
const int PIN_PWMA = 9;
const int PIN_LED  = 13;

const int PUMP_SPEED = 150;
float MS_PER_ML = 40.0;

// =====================================================
// 서버 및 네트워크 설정
// =====================================================
const char* SERVER_HOST = "192.168.0.51";
const int   SERVER_PORT = 8000;

// =====================================================
// 전역 제어 변수 (SRAM 최적화)
// =====================================================
bool isOn = false;
unsigned long wateringStartTime = 0;
unsigned long requiredWateringTime = 0;

unsigned long lastPollTime = 0;
const unsigned long POLL_INTERVAL_MS = 2000;

String currentTaskId = "";
String lastCompletedTaskId = ""; // 중복 급수 방지용 기록 변수

// =====================================================
// 펌프 제어 함수
// =====================================================
void startPump() {
  digitalWrite(PIN_LED, HIGH);
  digitalWrite(PIN_STBY, HIGH);
  digitalWrite(PIN_AIN1, HIGH);
  digitalWrite(PIN_AIN2, LOW);
  analogWrite(PIN_PWMA, PUMP_SPEED);
}

void stopPump() {
  digitalWrite(PIN_STBY, LOW);
  analogWrite(PIN_PWMA, 0);
  digitalWrite(PIN_LED, LOW);
}

// =====================================================
// AT 명령어 송신 유틸리티
// =====================================================
bool sendATCmd(const __FlashStringHelper* cmd, const char* expected, unsigned long timeoutMs) {
  espSerial.println(cmd);
  unsigned long start = millis();
  String res = "";
  while (millis() - start < timeoutMs) {
    while (espSerial.available() > 0) {
      char c = espSerial.read();
      res += c;
      if (res.indexOf(expected) != -1) {
        return true;
      }
    }
  }
  return false;
}

// =====================================================
// HTTP GET (JSON 바디 직접 추출 방식)
// =====================================================
String httpGet(const String& path) {
  while (espSerial.available()) espSerial.read();

  // 1. TCP 연결
  espSerial.print(F("AT+CIPSTART=\"TCP\",\""));
  espSerial.print(SERVER_HOST);
  espSerial.print(F("\","));
  espSerial.println(SERVER_PORT);

  espSerial.setTimeout(2000);
  if (!espSerial.find("OK") && !espSerial.find("ALREADY CONNECTED")) {
    espSerial.println(F("AT+CIPCLOSE"));
    return "";
  }

// 2. Request 생성 및 송신
  String req = "";
  req.reserve(100);  // 메모리 조각남 방지
  
  req += F("GET ");
  req += path;
  req += F(" HTTP/1.1\r\nHost: ");
  req += SERVER_HOST;
  req += F("\r\nConnection: close\r\n\r\n");
  
  espSerial.print(F("AT+CIPSEND="));
  espSerial.println(req.length());

  if (espSerial.find(">")) {
    espSerial.print(req);
  } else {
    espSerial.println(F("AT+CIPCLOSE"));
    return "";
  }

  // 3. JSON 데이터 수신 ({ 시작 위치 수신)
  String body = "";
  if (espSerial.find("{")) {
    body = "{";
    body.reserve(100);
    unsigned long start = millis();
    bool isComplete = false; // [수정됨] 단일 문자 검사를 통한 버퍼 최적화
    while (millis() - start < 2000) {
      while (espSerial.available()) {
        char c = (char)espSerial.read();
        body += c;
        if (c == '}') {
          isComplete = true;
          break;
        }
      }
      if (isComplete) break; 
    }
  }

  espSerial.println(F("AT+CIPCLOSE"));
  return body;
}

// =====================================================
// HTTP POST 텔레메트리 전송 (완벽 수정본)
// =====================================================
bool httpPostTelemetry(const String& taskId, const String& state, const String& errorMsg = "") {
  while (espSerial.available()) espSerial.read(); // 잔여 버퍼 비우기

  espSerial.println(F("AT+CIPCLOSE"));
  delay(100);

  // 1. TCP 연결
  espSerial.print(F("AT+CIPSTART=\"TCP\",\""));
  espSerial.print(SERVER_HOST);
  espSerial.print(F("\","));
  espSerial.println(SERVER_PORT);

  espSerial.setTimeout(2500);
  if (!espSerial.find("OK")) {
    espSerial.println(F("AT+CIPCLOSE"));
    return false;
  }

  // 2. JSON Body 생성
  String json = F("{\"task_id\":");
  json += taskId;
  json += F(",\"state\":\"");
  json += state;
  json += F("\"");
  
  if (errorMsg.length() > 0) {
    json += F(",\"error_message\":\"");
    json += errorMsg;
    json += F("\"");
  }
  json += F("}");

  // 3. HTTP POST 헤더 생성
  String header = F("POST /api/watering/telemetry HTTP/1.1\r\n");
  header += F("Host: ");
  header += SERVER_HOST;
  header += F("\r\nContent-Type: application/json\r\nContent-Length: ");
  header += String(json.length());
  header += F("\r\nConnection: close\r\n\r\n");

  espSerial.print(F("AT+CIPSEND="));
  espSerial.println(header.length() + json.length());

  // 4. 전송 및 응답 확인 로직
  bool success = false;
  espSerial.setTimeout(2000);
  
  if (espSerial.find(">")) {
    while (espSerial.available()) espSerial.read(); // '>' 이후 찌꺼기 비우기
    
    // 💡 SoftwareSerial 뻗음 방지를 위한 50ms 분할 전송 (AGV 코드와 동일한 방식)
    espSerial.print(header);
    delay(50); 
    espSerial.print(json);
    
    // 💡 find() 연속 호출 버그 해결: 텍스트를 모조리 모은 뒤 한 번에 검사
    unsigned long start = millis();
    String response = "";
    while (millis() - start < 3000) {
      while (espSerial.available()) {
        response += (char)espSerial.read();
      }
      
      // 200 OK 또는 201 Created 둘 중 하나라도 들어있으면 성공!
      if (response.indexOf("200") != -1 || response.indexOf("201") != -1) {
        success = true;
        break;
      }
    }
  }

  espSerial.println(F("AT+CIPCLOSE"));
  return success;
}
// =====================================================
// JSON 경량 값 추출 유틸리티
// =====================================================
String extractJsonValue(const String& json, const String& key) {
  int kIdx = json.indexOf("\"" + key + "\"");
  if (kIdx == -1) return "";
  int cIdx = json.indexOf(':', kIdx);
  if (cIdx == -1) return "";

  int start = cIdx + 1;
  while (start < json.length() && (json[start] == ' ' || json[start] == '"')) start++;

  int end = start;
  while (end < json.length() && json[end] != '"' && json[end] != ',' && json[end] != '}') end++;

  return json.substring(start, end);
}

// =====================================================
// SERVER POLL
// =====================================================
void pollServerCommand() {
  if (isOn) return; // 급수 구동 중일 때는 폴링을 실행하지 않음

  String response = httpGet(F("/api/watering/command"));
  if (response.length() == 0) return;

  String cmd = extractJsonValue(response, "command");

  if (cmd == "WATER") {
    String taskId = extractJsonValue(response, "task_id");
    String amountStr = extractJsonValue(response, "amount_ml");

    if (taskId.length() > 0 && taskId != lastCompletedTaskId) {
      float amount = amountStr.toFloat();
      if (amount > 0) {
        currentTaskId = taskId;
        requiredWateringTime = (unsigned long)(amount * MS_PER_ML);

        // 1. 먼저 WATERING 상태 텔레메트리 전송 (네트워크 통신 완료 대기)
        httpPostTelemetry(taskId, "WATERING");

        // 💡 2. 통신이 완전히 끝난 '진짜 펌프가 돌기 직전'의 시점을 타이머 시작점으로 기록!
        wateringStartTime = millis();
        isOn = true;

        // 3. 펌프 작동 시작
        startPump();
      }
    }
  }
}

// =====================================================
// SETUP & LOOP
// =====================================================
void setup() {
  pinMode(PIN_STBY, OUTPUT);
  pinMode(PIN_AIN1, OUTPUT);
  pinMode(PIN_AIN2, OUTPUT);
  pinMode(PIN_PWMA, OUTPUT);
  pinMode(PIN_LED, OUTPUT);
  stopPump();

  Serial.begin(9600);
  espSerial.begin(9600);
  espSerial.setTimeout(100);

  // ESP8266 AT 모듈 초기화
  delay(1000);
  sendATCmd(F("AT"), "OK", 1000);
  sendATCmd(F("AT+CWMODE=1"), "OK", 1000);
  sendATCmd(F("AT+CIPMUX=0"), "OK", 500);
}

void loop() {
  unsigned long now = millis();

  // 1. 펌프 동작 및 경과 시간 측정 (최우선 처리)
  if (isOn) {
    unsigned long elapsed = now - wateringStartTime;
    
    // 💡 전압 강하가 해결되었으므로 100->125->150 단계별 상승을 지우고 처음부터 150으로 확실하게 구동
    analogWrite(PIN_PWMA, PUMP_SPEED);

    if (elapsed >= requiredWateringTime) {
      stopPump();
      isOn = false;

      // 💡 [핵심 1] 펌프가 꺼질 때 튀는 전기적 노이즈가 가라앉고 와이파이 모듈이 정신 차릴 시간 (1초 대기)
      delay(1000);

      // COMPLETED 텔레메트리 POST 전송 (최대 3회 재시도)
      bool posted = false;
      for (int i = 0; i < 3; i++) {
        if (httpPostTelemetry(currentTaskId, "COMPLETED")) {
          posted = true;
          break;
        }
        delay(1000); // 재시도 대기 시간 최적화 (3초 -> 1초)
      }

      // 💡 [핵심 2] 통신 성공 여부와 상관없이 "어쨌든 물은 줬다"는 사실을 기록!
      // 이렇게 무조건 기록해두어야 통신이 실패하더라도 펌프가 2번 도는 대참사를 막을 수 있습니다.
      lastCompletedTaskId = currentTaskId; 
      currentTaskId = "";
    }
  }

  // 2. 대기 상태일 때만 2초 주기로 폴링 실행
  if (!isOn) {
    if (now - lastPollTime >= POLL_INTERVAL_MS) {
      lastPollTime = now;
      pollServerCommand();
    }
  }
}