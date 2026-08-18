# Plant Watering AGV Server API v2.0

## 공통

- Base URL: `http://192.168.0.51:8000`
- 통신: HTTP REST / JSON / Wi-Fi
- Swagger: `http://192.168.0.51:8000/docs`
- 급수 장치: **급수 모터 Arduino2**. Trailer는 사용하지 않는다.

## 핵심 자동 급수 흐름

```text
센서 수분 보고 → AUTO Task 생성 → AGV 이동 → AGV 도착
→ Arduino2 급수 모터 동작 → 완료 보고 → 급수 이력 저장
```

Task 상태: `QUEUED → MOVING → ARRIVED → WATERING → COMPLETED`

## 센서

### POST /api/plants/{plant_id}/moisture

```json
{ "moisture": 20 }
```

수분값이 임계값보다 낮고 이전 상태가 NORMAL이면 AUTO Task를 하나 생성한다.

## AGV

### GET /api/agv/command

```json
{
  "command": "GO_TO_PLANT",
  "task_id": 1,
  "plant_id": 3,
  "target_position": 200,
  "amount_ml": 80
}
```

작업이 없으면 `{ "command": "WAIT" }`를 반환한다.

### POST /api/agv/telemetry

```json
{ "task_id": 1, "state": "ARRIVED", "position": 200, "battery": 86 }
```

상태: `IDLE`, `MOVING`, `ARRIVED`, `ERROR`

## 급수 모터 Arduino2

### GET /api/watering/command

AGV가 도착해 `ARRIVED` 상태인 Task를 가져간다.

```json
{ "command": "WATER", "task_id": 1, "plant_id": 3, "amount_ml": 80 }
```

Arduino2는 `amount_ml`을 기준으로 모터 시간 또는 유량 제어를 수행한다.
작업이 없으면 `{ "command": "WAIT" }`를 반환한다.

### POST /api/watering/telemetry

급수 시작:

```json
{ "task_id": 1, "state": "WATERING" }
```

급수 완료:

```json
{ "task_id": 1, "state": "COMPLETED" }
```

오류:

```json
{ "task_id": 1, "state": "ERROR", "error_message": "Pump failure" }
```

상태: `IDLE`, `WATERING`, `COMPLETED`, `ERROR`

### GET /api/watering/device-status

Arduino2의 상태, 모터 작동 여부, 수행 중 Task를 반환한다.

## GUI

- `GET /api/dashboard`: `plants`, `agv`, `watering_device`, 최근 `tasks`를 반환한다.
- `GET /api/plants`: 화분 상태 조회
- `GET /api/agv/status`: AGV 상태 조회
- `GET /api/watering/tasks`: Task 목록 조회
- `GET /api/watering/log`: 완료 급수 이력 조회
- `POST /api/watering`: 강제 급수 Task 생성

## 제외된 API

- `/api/trailer/status`
- `/api/trailer/command`
- `/api/trailer/telemetry`
