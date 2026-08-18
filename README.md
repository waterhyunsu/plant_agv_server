# Plant Watering AGV Server v2.0

화분 수분 상태를 감지해 AGV 이동과 **급수 모터 Arduino2** 동작을 순서대로 관리하는 중앙 서버입니다.

```text
수분 센서 → FastAPI 서버 → 급수 Task → AGV → 급수 모터 Arduino2 → 로그/GUI
```

## 구성

- 서버: FastAPI + Uvicorn
- DB: SQLite
- 통신: Wi-Fi + HTTP REST + JSON
- 급수 장치: 급수 모터 Arduino2
- 제외: MQTT, Trailer, AGV 수동 이동 API

## 실행

```powershell
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Swagger: `http://127.0.0.1:8000/docs`

## 핵심 흐름

1. 센서가 `POST /api/plants/{plant_id}/moisture`로 수분값을 보낸다.
2. 수분 부족이면 서버가 `QUEUED` 급수 Task를 생성한다.
3. AGV가 `GET /api/agv/command`로 목적지와 Task를 받는다.
4. AGV가 `POST /api/agv/telemetry`로 `ARRIVED`를 보고한다.
5. Arduino2가 `GET /api/watering/command`로 `WATER`와 `amount_ml`을 받는다.
6. Arduino2가 `POST /api/watering/telemetry`로 `COMPLETED`를 보고한다.
7. 서버는 Task를 완료하고 급수 이력을 저장한다.

## 주요 API

| 담당 | API |
|---|---|
| 센서 | `POST /api/plants/{plant_id}/moisture` |
| AGV | `GET /api/agv/command`, `POST /api/agv/telemetry` |
| 급수 모터 Arduino2 | `GET /api/watering/command`, `POST /api/watering/telemetry`, `GET /api/watering/device-status` |
| GUI | `GET /api/dashboard`, `GET /api/watering/tasks`, `GET /api/watering/log`, `POST /api/watering` |

상세 요청/응답은 [API_SPEC.md](API_SPEC.md), 역할별 진행 방법은 [TEAM_HANDOFF.md](TEAM_HANDOFF.md)를 참고하세요.

## 주의

- 완료 후 화분 수분 상태는 서버가 임의로 바꾸지 않습니다. 센서의 재측정값으로 갱신합니다.
- 같은 화분에서 진행 중인 Task가 있으면 중복 Task를 만들지 않습니다.
- 이전 Trailer API(`/api/trailer/*`)는 사용하지 않습니다.
