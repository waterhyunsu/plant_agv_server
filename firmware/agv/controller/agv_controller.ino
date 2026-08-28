#include <Arduino.h>
#include <SoftwareSerial.h>

// ============================================================================
// [1] 설정 및 전역 변수
// ============================================================================
const String SSID = "3F_302";
const String PASS = "0424719222!!";
const String SERVER_IP = "192.168.0.51";
const String PORT = "8000";

SoftwareSerial esp(2, 3);

const int PIN_PWM_L = 9, PIN_L_IN1 = 4, PIN_L_IN2 = 7;
const int PIN_PWM_R = 10, PIN_R_IN1 = 8, PIN_R_IN2 = 12;
const int PIN_STBY = 11;

#define TRIG_PIN 5
#define ECHO_PIN 6

const float CALIB_L = 1.05;
const int BASE_PWM = 180;
const unsigned long KICKSTART_TIME = 200;
const unsigned long PRE_KICKSTART_DELAY = 500; 
const long STOP_DISTANCE = 30;
const int SENSOR_INTERVAL_MS = 40;
const unsigned long POLL_INTERVAL_MS = 2000;
const unsigned long TIMEOUT_LIMIT = 15000; // 💡 15초 이상 헤매면 강제 정지하여 GET 통신 복구

enum DriveMode { STOP, GO, RETURN };
DriveMode current_mode = STOP;

bool is_kickstart_done = false;
unsigned long kickstart_start_time = 0;
unsigned long mode_start_time = 0;
unsigned long last_sensor_time = 0;
unsigned long last_poll_time = 0;

int current_task_id = 0; // 최신 태스크 ID 저장용
int current_pos = 0;
int target_pos = 0;

void changeMode(DriveMode new_mode);
void parseCommand(const String& response);

// ============================================================================
// [2] 와이파이 기본 통신
// ============================================================================
void sendCmd(String cmd, int timeout) {
  esp.println(cmd);
  delay(timeout);
  while (esp.available()) esp.read();
}

// ============================================================================
// [3] 서버 통신 - 송신, 수신, 파싱
// ============================================================================
void sendTelemetry(const char* state) {
  while (esp.available()) esp.read();
  sendCmd("AT+CIPCLOSE", 150);

  esp.print("AT+CIPSTART=\"TCP\",\"");
  esp.print(SERVER_IP);
  esp.print("\",");
  esp.println(PORT);
  
  esp.setTimeout(3000);
  if (!esp.find((char*)"OK")) {
    sendCmd("AT+CIPCLOSE", 100);
    return;
  }

  char body[64];
  snprintf(body, sizeof(body), "{\"task_id\":%d,\"state\":\"%s\"}\r\n", current_task_id, state);
  int bodyLen = strlen(body);

  char header[140];
  snprintf(header, sizeof(header),
    "POST /api/agv/telemetry HTTP/1.1\r\n"
    "Host: %s:%s\r\n"
    "Content-Type: application/json\r\n"
    "Content-Length: %d\r\n"
    "Connection: close\r\n\r\n",
    SERVER_IP.c_str(), PORT.c_str(), bodyLen
  );

  int totalLen = strlen(header) + bodyLen;
  esp.print("AT+CIPSEND=");
  esp.println(totalLen);

  esp.setTimeout(2500);
  if (esp.find((char*)">")) {
    while (esp.available()) esp.read();
    esp.print(header);
    delay(50);
    esp.print(body);
    delay(1000);
  }
  sendCmd("AT+CIPCLOSE", 150);
}

void pollServerCommand() {
  sendCmd("AT+CIPCLOSE", 150);

  esp.print("AT+CIPSTART=\"TCP\",\"");
  esp.print(SERVER_IP);
  esp.print("\",");
  esp.println(PORT);

  esp.setTimeout(2000);
  if (!esp.find((char*)"OK")) {
    sendCmd("AT+CIPCLOSE", 100);
    return;
  }

  // 💡 기존 API 주소 유지 (서버에 맞게)
  char httpReq[160];
  snprintf(httpReq, sizeof(httpReq),
    "GET /api/agv/command HTTP/1.1\r\n"
    "Host: %s:%s\r\n"
    "Connection: close\r\n\r\n",
    SERVER_IP.c_str(), PORT.c_str()
  );

  esp.print("AT+CIPSEND=");
  esp.println(strlen(httpReq));

  esp.setTimeout(2000);
  if (!esp.find((char*)">")) {
    sendCmd("AT+CIPCLOSE", 100);
    return;
  }

  esp.print(httpReq);

  String response = "";
  response.reserve(250);
  unsigned long start = millis();
  unsigned long lastByteTime = millis();

  while (millis() - start < 3500) {
    while (esp.available()) {
      char c = (char)esp.read();
      response += c;
      lastByteTime = millis();
    }
    if (response.length() > 0 && (millis() - lastByteTime > 500)) break;
  }
  sendCmd("AT+CIPCLOSE", 150);
  if (response.length() == 0) return;

  parseCommand(response);
}

void parseCommand(const String& response) {
  int jsonStart = response.indexOf('{');
  if (jsonStart == -1) return;

  // 💡 API 규격 호환성 강화: "state" 또는 "command" 둘 다 알아듣도록 처리
  int cmdIdx = response.indexOf("\"state\"", jsonStart);
  if (cmdIdx == -1) cmdIdx = response.indexOf("\"command\"", jsonStart);
  if (cmdIdx == -1) cmdIdx = response.indexOf("command", jsonStart); // 따옴표 없는 경우 대비
  if (cmdIdx == -1) return;

  if (response.indexOf("WAIT", cmdIdx) != -1 || response.indexOf("wait", cmdIdx) != -1) {
    return;
  }

  // 💡 명령 중복 실행 방지: task_id가 똑같으면 무시하고 리턴!
  int taskIdx = response.indexOf("task_id", jsonStart);
  if (taskIdx != -1) {
    int colonIdx = response.indexOf(':', taskIdx);
    if (colonIdx != -1) {
      int received_task_id = response.substring(colonIdx + 1).toInt();
      if (received_task_id == current_task_id && received_task_id != 0) return; // 이미 실행한 태스크면 종료
      current_task_id = received_task_id; // 새 태스크면 갱신
    }
  }

  if (response.indexOf("GO", cmdIdx) != -1 || response.indexOf("go", cmdIdx) != -1) {
    int plantIdx = response.indexOf("plant_id", jsonStart);
    if (plantIdx != -1) {
      int colonIdx = response.indexOf(':', plantIdx);
      if (colonIdx != -1) target_pos = response.substring(colonIdx + 1).toInt();
    }
    changeMode(GO);
  } else if (response.indexOf("RETURN", cmdIdx) != -1 || response.indexOf("return", cmdIdx) != -1) {
    target_pos = 0;
    changeMode(RETURN);
  }
}

// ============================================================================
// [4] 하드웨어 및 주행 제어
// ============================================================================
void setMotorDrive(int speed_L, int speed_R) {
  digitalWrite(PIN_STBY, HIGH);
  int pwm_L = constrain(abs((int)(speed_L * CALIB_L)), 0, 255);
  int pwm_R = constrain(abs(speed_R), 0, 255);

  digitalWrite(PIN_L_IN1, speed_L >= 0 ? LOW : HIGH);
  digitalWrite(PIN_L_IN2, speed_L >= 0 ? HIGH : LOW);
  analogWrite(PIN_PWM_L, pwm_L);

  digitalWrite(PIN_R_IN1, speed_R >= 0 ? LOW : HIGH);
  digitalWrite(PIN_R_IN2, speed_R >= 0 ? HIGH : LOW);
  analogWrite(PIN_PWM_R, pwm_R);
}

long getDistance() {
  long d[3];
  for (int i = 0; i < 3; i++) {
    digitalWrite(TRIG_PIN, LOW);
    delayMicroseconds(2);
    digitalWrite(TRIG_PIN, HIGH);
    delayMicroseconds(10);
    digitalWrite(TRIG_PIN, LOW);

    unsigned long duration = pulseIn(ECHO_PIN, HIGH, 6000);
    d[i] = (duration == 0) ? 999 : (duration * 0.034 / 2);
    delay(4);
  }
  if (d[0] > d[1]) { long t = d[0]; d[0] = d[1]; d[1] = t; }
  if (d[1] > d[2]) { long t = d[1]; d[1] = d[2]; d[2] = t; }
  if (d[0] > d[1]) { long t = d[0]; d[0] = d[1]; d[1] = t; }
  return d[1];
}

void changeMode(DriveMode new_mode) {
  current_mode = new_mode;
  if (new_mode != STOP) {
    is_kickstart_done = false;
    kickstart_start_time = 0;
    mode_start_time = millis();
  }
}

unsigned long getIgnoreTimeByDiff(int diff) {
  if (diff == 1) return 1000;
  else if (diff == 2) return 4000;
  else if (diff == 3) return 12000;
  return 500;
}

// ============================================================================
// [5] 아두이노 초기화 & 메인 루프
// ============================================================================
void setup() {
  esp.begin(9600);
  esp.setTimeout(1000);
  delay(2000);

  sendCmd("AT+RST", 2000);
  sendCmd("ATE0", 500);
  sendCmd("AT+CWQAP", 500);
  sendCmd("AT+CWMODE=1", 500);
  
  esp.println("AT+CWJAP=\"" + SSID + "\",\"" + PASS + "\"");
  esp.setTimeout(15000);
  esp.find((char*)"OK");
  while (esp.available()) esp.read();

  sendCmd("AT+CIPMUX=0", 500);

  const int outPins[] = { PIN_PWM_L, PIN_L_IN1, PIN_L_IN2,
                          PIN_PWM_R, PIN_R_IN1, PIN_R_IN2,
                          PIN_STBY, TRIG_PIN };
  for (int pin : outPins) pinMode(pin, OUTPUT);
  pinMode(ECHO_PIN, INPUT);

  digitalWrite(PIN_STBY, HIGH);
  setMotorDrive(0, 0);
}

void loop() {
  unsigned long current_time = millis();

  // 1. 서버 폴링 (STOP 상태일 때만 2초 간격)
  if (current_mode == STOP && current_time - last_poll_time >= POLL_INTERVAL_MS) {
    last_poll_time = current_time;
    pollServerCommand();
  }

  int move_diff = abs(target_pos - current_pos);
  unsigned long dynamic_ignore_time = getIgnoreTimeByDiff(move_diff);

  // 2. 모드별 주행 로직
  switch (current_mode) {
    case STOP: {
      setMotorDrive(0, 0);
      break;
    }

    case GO: {
      if (current_time - mode_start_time < PRE_KICKSTART_DELAY) {
        setMotorDrive(0, 0);
      } else if (!is_kickstart_done) {
        if (kickstart_start_time == 0) kickstart_start_time = current_time;
        // 💡 배터리 컷오프를 막기 위해 200으로 안전한 킥스타트
        setMotorDrive(200, 200); 
        if (current_time - kickstart_start_time >= KICKSTART_TIME) {
          is_kickstart_done = true;
        }
      } else {
        setMotorDrive(BASE_PWM, BASE_PWM);

        if (current_time - last_sensor_time >= SENSOR_INTERVAL_MS) {
          last_sensor_time = current_time;

          if (current_time - mode_start_time >= (PRE_KICKSTART_DELAY + dynamic_ignore_time)) {
            long dist = getDistance();

            // 💡 무한 대기(Deadlock) 방지: 센서 감지 성공하거나, 15초 이상 못 찾으면 강제로 멈춰서 다음 GET 통신을 살림
            if (dist <= STOP_DISTANCE || current_time - mode_start_time > TIMEOUT_LIMIT) {
              setMotorDrive(0, 0);       
              current_pos = target_pos;
              changeMode(STOP);          
              delay(300);                
              sendTelemetry("STOP");     
            }
          }
        }
      }
      break;
    }

    case RETURN: {
      if (current_time - mode_start_time < PRE_KICKSTART_DELAY) {
        setMotorDrive(0, 0);
      } else if (!is_kickstart_done) {
        if (kickstart_start_time == 0) kickstart_start_time = current_time;
        // 💡 역회전 시에도 컷오프를 막기 위해 -200으로 안전한 킥스타트
        setMotorDrive(-200, -200); 
        if (current_time - kickstart_start_time >= KICKSTART_TIME) {
          is_kickstart_done = true;
        }
      } else {
        setMotorDrive(-BASE_PWM, -BASE_PWM);

        if (current_time - last_sensor_time >= SENSOR_INTERVAL_MS) {
          last_sensor_time = current_time;

          if (current_time - mode_start_time >= (PRE_KICKSTART_DELAY + dynamic_ignore_time)) {
            long dist = getDistance();

            // 💡 15초 타임아웃 방어막 적용
            if (dist <= STOP_DISTANCE || current_time - mode_start_time > TIMEOUT_LIMIT) {
              setMotorDrive(0, 0);       
              current_pos = 0;           
              changeMode(STOP);          
              delay(300);                
              sendTelemetry("STOP");     
            }
          }
        }
      }
      break;
    }
  }
}