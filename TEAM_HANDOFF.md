# Plant Watering AGV Server — 팀원 배포 안내

## 시스템 역할

서버는 센서, AGV, **급수 모터 Arduino2**, GUI를 연결하는 중앙 관제 역할을 한다. Trailer는 프로젝트 구성에서 제거했다.

```text
센서 → 서버 → 급수 Task → AGV → 급수 모터 Arduino2 → 서버 로그/GUI
```

- 통신: Wi-Fi + HTTP REST + JSON
- 서버: FastAPI / SQLite
- 서버는 모터를 직접 제어하지 않는다. Arduino2가 명령을 조회하고 결과를 보고한다.

## 실행

```powershell
cd plant_agv_server_v1
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Swagger: `http://<서버-PC-IP>:8000/docs`

## 담당별 연동

### 수분 센서 Arduino

`POST /api/plants/{plant_id}/moisture`에 0~100 수분값을 전송한다. 급수 후에는 재측정한 값을 다시 전송한다.

### AGV Arduino

`GET /api/agv/command`를 polling하고, `GO_TO_PLANT`이면 `target_position`으로 이동한다. 이동·도착·오류는 `POST /api/agv/telemetry`로 전송한다.

### 급수 모터 Arduino2

`GET /api/watering/command`를 polling한다. `WATER` 명령의 `amount_ml`에 따라 펌프를 제어한다. 급수 시작·완료·오류는 `POST /api/watering/telemetry`로 전송한다.

> `amount_ml`을 펌프 구동 시간 또는 유량 제어로 변환하는 공식과 보정은 Arduino2 담당이 확정한다.

### GUI

`GET /api/dashboard`의 `plants`, `agv`, `watering_device`, `tasks`를 표시한다. 강제 급수는 `POST /api/watering`으로 Task를 생성한다.

## 통합 테스트

1. `POST /api/plants/3/moisture`에 `20` 전송 → AUTO Task 생성 확인
2. `GET /api/agv/command` → `GO_TO_PLANT` 확인
3. `POST /api/agv/telemetry`에 `ARRIVED` 전송
4. `GET /api/watering/command` → `WATER`, `amount_ml` 확인
5. `POST /api/watering/telemetry`에 `COMPLETED` 전송
6. `/api/watering/tasks`의 COMPLETED와 `/api/watering/log`의 SUCCESS 확인

## API 변경 원칙

- 이 파일과 `API_SPEC.md`, Swagger를 최신 기준으로 사용한다.
- Trailer API는 사용 금지다.
- Arduino2 구현 전에도 요청/응답 형식은 변경하지 않는다. 변경이 필요하면 팀 전체에 먼저 공지한다.
