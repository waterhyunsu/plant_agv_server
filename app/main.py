# Plant Watering AGV 중앙 서버의 HTTP REST API.
# 센서, AGV, 급수 모터 Arduino2, GUI는 이 파일의 API를 통해 상태와 명령을 주고받는다.


from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .db import init_db, get_db, now_iso
from .schemas import (
    MoistureReport,
    ManualWateringRequest,
    AGVTelemetry,
    WateringDeviceTelemetry,
)
from .services import (
    create_task,
    get_active_task,
    get_plant,
    get_task,
    set_task_status,
    complete_task,
    log_event,
    get_moisture_history,
    get_system_logs,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI 시작 시 DB 구조와 초기 데이터를 준비한다."""
    init_db()
    yield


app = FastAPI(
    title="Plant Watering AGV Server",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 모든 도메인에서 접근 허용
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================================================
# Health / Dashboard
# =========================================================

@app.get("/api/health")
def health():
    """서버 실행 여부를 확인하는 경량 상태 확인 API."""
    return {
        "status": "ok",
        "service": "plant-agv-server",
    }


@app.get("/api/dashboard")
def dashboard():
    """GUI 첫 화면용 통합 상태(화분·AGV·급수 모터 Arduino2·최근 Task)를 반환한다."""
    with get_db() as conn:
        plants = [
            dict(row)
            for row in conn.execute("""
                SELECT *
                FROM plants
                ORDER BY id
            """).fetchall()
        ]

        agv = dict(conn.execute("""
            SELECT state, battery, current_task_id, updated_at
            FROM agv_status
            WHERE id=1
        """).fetchone())

        watering_device = dict(conn.execute("""
            SELECT state, pump, current_task_id, updated_at
            FROM watering_device_status
            WHERE id=1
        """).fetchone())

        tasks = [
            dict(row)
            for row in conn.execute("""
                SELECT id AS task_id,
                       plant_id,
                       status,
                       source,
                       created_at,
                       updated_at,
                       error_message
                FROM watering_tasks
                ORDER BY id DESC
                LIMIT 50
            """).fetchall()
        ]

        return {
            "plants": plants,
            "agv": agv,
            "watering_device": watering_device,
            "tasks": tasks,
        }


# =========================================================
# Plants / Moisture
# =========================================================

@app.get("/api/plants")
def get_plants():
    """모든 화분의 현재 수분 상태와 설정값을 반환한다."""
    with get_db() as conn:
        return [
            dict(row)
            for row in conn.execute("""
                SELECT *
                FROM plants
                ORDER BY id
            """).fetchall()
        ]


@app.post("/api/plants/{plant_id}/moisture")
def report_moisture(plant_id: int, report: MoistureReport):
    """
    센서 수분값을 저장하고 NORMAL→DRY 전환 시 자동 Task를 만든다.

    센서가 전송한 시각은 사용하지 않고,
    서버가 데이터를 수신한 시각을 created_at으로 기록한다.
    """
    with get_db() as conn:
        plant = get_plant(conn, plant_id)

        # 임계값 미만일 때만 DRY로 판단한다.
        new_status = (
            "DRY"
            if report.moisture < plant["threshold"]
            else "NORMAL"
        )

        previous_status = plant["status"]

        # 서버 수신 시각을 측정/저장 시각으로 사용한다.
        ts = now_iso()

        conn.execute("""
            UPDATE plants
            SET moisture=?, status=?, updated_at=?
            WHERE id=?
        """, (
            report.moisture,
            new_status,
            ts,
            plant_id,
        ))

        # 1분 원본 데이터를 그대로 저장한다.
        conn.execute("""
            INSERT INTO moisture_log
            (plant_id, moisture, status, created_at)
            VALUES (?, ?, ?, ?)
        """, (
            plant_id,
            report.moisture,
            new_status,
            ts,
        ))

        task_id = None

        # NORMAL → DRY로 전환될 때만 자동 Task를 생성한다.
        # DRY가 반복 보고되는 동안에는 중복 Task를 만들지 않는다.
        if new_status == "DRY" and previous_status != "DRY":
            task_id = create_task(
                conn,
                plant_id,
                "AUTO",
            )

        return {
            "plant_id": plant_id,
            "moisture": report.moisture,
            "status": new_status,
            "task_created": task_id is not None,
            "task_id": task_id,
        }


@app.get("/api/plants/{plant_id}/moisture/realtime")
def get_realtime_moisture(plant_id: int):
    """
    실시간 GUI용 최근 수분 데이터를 반환한다.

    DB에 저장된 1분 단위 원본 데이터를 그대로 반환한다.
    최근 60분 데이터를 반환한다.
    """
    with get_db() as conn:
        # 화분 존재 여부 확인
        get_plant(conn, plant_id)

        rows = conn.execute("""
            SELECT
                plant_id,
                moisture,
                created_at AS measured_at
            FROM moisture_log
            WHERE plant_id=?
            ORDER BY created_at DESC
            LIMIT 60
        """, (plant_id,)).fetchall()

        # GUI에서 시간 순서대로 그릴 수 있도록 오래된 데이터부터 반환한다.
        return [
            dict(row)
            for row in reversed(rows)
        ]


@app.get("/api/plants/{plant_id}/moisture/history")
def get_moisture_history_api(
    plant_id: int,
    date: str,
):
    """
    특정 날짜의 전체 토양 수분 이력을 조회한다.
    date:
        YYYY-MM-DD
    하루 전체(00:00:00 ~ 23:59:59)를
    5분 단위로 집계하여 반환한다.

    DB의 1분 원본 데이터는 그대로 보존하고,
    API 응답에서는 5분 단위 평균값으로 집계한다.
    """
    with get_db() as conn:
        # 화분 존재 여부 확인
        get_plant(conn, plant_id)

        return get_moisture_history(
            conn,
            plant_id,
            date,
        )


# =========================================================
# System Logs
# =========================================================

@app.get("/api/system/logs")
def get_logs():
    """
    서버/AGV/급수장치 등의 최근 시스템 이벤트 로그를 반환한다.

    GUI는 이 API를 polling하여 터미널 형태의 로그 화면을 구성한다.
    """
    with get_db() as conn:
        return get_system_logs(conn)


# =========================================================
# AGV
# =========================================================

@app.get("/api/agv/status")
def get_agv_status():
    """현재 AGV의 상태, 배터리 및 수행 중 Task를 반환한다."""
    with get_db() as conn:
        return dict(conn.execute("""
            SELECT state, battery, current_task_id, updated_at
            FROM agv_status
            WHERE id=1
        """).fetchone())


@app.get("/api/agv/command")
def get_agv_command():
    """AGV가 polling하여 GO/RETURN/WAIT 명령을 받는 API."""
    with get_db() as conn:
        agv = conn.execute("""
            SELECT state, current_task_id
            FROM agv_status
            WHERE id=1
        """).fetchone()

        # 1. 아직 수행해야 할 Task가 있으면 순서대로 처리한다.
        task = get_active_task(conn)

        if task is not None:
            if task["status"] == "QUEUED":
                set_task_status(conn, task["id"], "MOVING")

                conn.execute("""
                    UPDATE agv_status
                    SET state='GO',
                        current_task_id=?,
                        updated_at=?
                    WHERE id=1
                """, (task["id"], now_iso()))

                log_event(
                    conn,
                    "AGV_DISPATCH",
                    f"AGV dispatched to plant {task['plant_id']}",
                    task["id"],
                )

            elif task["status"] == "MOVING":
                # 이미 이동 중인 Task는 같은 GO 명령을 유지한다.
                pass

            else:
                return {"command": "WAIT"}

            return {
                "command": "GO",
                "task_id": task["id"],
                "plant_id": task["plant_id"],
            }

        # 2. 더 이상 처리할 활성 Task가 없을 때만 복귀를 시작한다.
        if agv["current_task_id"] is not None and agv["state"] == "STOP":
            completed_task = conn.execute("""
                SELECT id, status
                FROM watering_tasks
                WHERE id=?
            """, (agv["current_task_id"],)).fetchone()

            if (
                completed_task is not None
                and completed_task["status"] == "COMPLETED"
            ):
                conn.execute("""
                    UPDATE agv_status
                    SET state='TURN',
                        updated_at=?
                    WHERE id=1
                """, (now_iso(),))

                log_event(
                    conn,
                    "AGV_RETURN",
                    "all queued watering tasks completed; AGV returning home",
                    completed_task["id"],
                )

                return {
                    "command": "RETURN",
                    "task_id": completed_task["id"],
                }

        return {"command": "WAIT"}


@app.post("/api/agv/telemetry")
def agv_telemetry(report: AGVTelemetry):
    """AGV의 STOP/GO/TURN/ERROR 상태를 저장하고 Task를 갱신한다."""
    with get_db() as conn:
        ts = now_iso()

        conn.execute("""
            UPDATE agv_status
            SET state=?, battery=?, current_task_id=?, updated_at=?
            WHERE id=1
        """, (
            report.state,
            report.battery,
            report.task_id,
            ts,
        ))

        if report.task_id is None:
            return {
                "ok": True,
                "task_id": None,
                "state": report.state,
            }

        task = get_task(conn, report.task_id)

        # STOP + 진행 중 Task는 목표 화분을 감지하고 정지한 상태로 판단한다.
        if report.state == "STOP":
            if task["status"] in ("MOVING", "QUEUED"):
                set_task_status(
                    conn,
                    report.task_id,
                    "ARRIVED",
                )

                log_event(
                    conn,
                    "AGV_ARRIVED",
                    f"AGV arrived at plant {task['plant_id']}",
                    report.task_id,
                )

        elif report.state == "ERROR":
            set_task_status(
                conn,
                report.task_id,
                "FAILED",
                report.error_message or "AGV error",
            )

            log_event(
                conn,
                "AGV_ERROR",
                report.error_message or "AGV error",
                report.task_id,
            )

        # GO/TURN은 AGV 물리 동작 상태만 기록한다.
        return {
            "ok": True,
            "task_id": report.task_id,
            "state": report.state,
        }


# =========================================================
# 급수 모터 Arduino2
# =========================================================

@app.get("/api/watering/device-status")
def get_watering_device_status():
    """급수 모터 Arduino2의 상태, 모터 상태 및 수행 중 Task를 반환한다."""
    with get_db() as conn:
        row = conn.execute("""
            SELECT state, pump, current_task_id, updated_at
            FROM watering_device_status
            WHERE id=1
        """).fetchone()

        result = dict(row)
        result["pump"] = bool(result["pump"])

        return result


@app.get("/api/watering/command")
def get_watering_command():
    """급수 모터 Arduino2가 polling하여 도착 완료 Task의 WATER 명령을 받는 API."""
    with get_db() as conn:
        task = conn.execute("""
            SELECT *
            FROM watering_tasks
            WHERE status IN ('ARRIVED', 'WATERING')
            ORDER BY id ASC
            LIMIT 1
        """).fetchone()

        if task is None:
            return {"command": "WAIT"}

        # Arduino2가 명령을 가져가면 급수 중(WATERING)으로 바꾸고 펌프 상태를 켠다.
        if task["status"] == "ARRIVED":
            set_task_status(
                conn,
                task["id"],
                "WATERING",
            )

            conn.execute("""
                UPDATE watering_device_status
                SET state='WATERING',
                    pump=1,
                    current_task_id=?,
                    updated_at=?
                WHERE id=1
            """, (task["id"], now_iso()))

            log_event(
                conn,
                "WATERING_START",
                f"watering started for plant {task['plant_id']}",
                task["id"],
            )

        return {
            "command": "WATER",
            "task_id": task["id"],
            "plant_id": task["plant_id"],
        }


@app.post("/api/watering/telemetry")
def watering_device_telemetry(
    report: WateringDeviceTelemetry,
):
    """급수 모터 Arduino2의 급수 시작·완료·오류를 저장하고 Task 및 이력을 갱신한다."""
    with get_db() as conn:
        ts = now_iso()

        pump = 1 if report.state == "WATERING" else 0

        if report.state in ("COMPLETED", "ERROR"):
            pump = 0

        conn.execute("""
            UPDATE watering_device_status
            SET state=?, pump=?, current_task_id=?, updated_at=?
            WHERE id=1
        """, (
            report.state,
            pump,
            report.task_id,
            ts,
        ))

        if report.task_id is None:
            return {"ok": True}

        task = get_task(conn, report.task_id)

        if report.state == "WATERING":
            if task["status"] == "ARRIVED":
                set_task_status(
                    conn,
                    report.task_id,
                    "WATERING",
                )

        elif report.state == "COMPLETED":
            # WATERING 상태의 Task만 완료 처리해 중복 로그 생성을 막는다.
            if task["status"] != "WATERING":
                raise HTTPException(
                    409,
                    "Task is not in WATERING state",
                )

            set_task_status(
                conn,
                report.task_id,
                "COMPLETED",
            )

            complete_task(
                conn,
                report.task_id,
                task["plant_id"],
            )

            # 급수 완료 후 급수장치를 대기 상태로 초기화한다.
            conn.execute("""
                UPDATE watering_device_status
                SET state='IDLE',
                    pump=0,
                    current_task_id=NULL,
                    updated_at=?
                WHERE id=1
            """, (ts,))

        elif report.state == "ERROR":
            set_task_status(
                conn,
                report.task_id,
                "FAILED",
                report.error_message or "Watering device error",
            )

            log_event(
                conn,
                "WATERING_DEVICE_ERROR",
                report.error_message or "Watering device error",
                report.task_id,
            )

        return {
            "ok": True,
            "task_id": report.task_id,
            "state": report.state,
        }


# =========================================================
# Watering Tasks
# =========================================================

@app.get("/api/watering/tasks")
def get_watering_tasks():
    """GUI에서 작업 진행 상태를 표시할 수 있도록 최근 Task 목록을 반환한다."""
    with get_db() as conn:
        return [
            dict(row)
            for row in conn.execute("""
                SELECT id AS task_id,
                       plant_id,
                       status,
                       source,
                       created_at,
                       updated_at,
                       error_message
                FROM watering_tasks
                ORDER BY id DESC
                LIMIT 100
            """).fetchall()
        ]


@app.get("/api/watering/log")
def get_watering_log():
    """완료된 급수 이력을 최근 순으로 반환한다."""
    with get_db() as conn:
        return [
            dict(row)
            for row in conn.execute("""
                SELECT
                    id,
                    task_id,
                    plant_id,
                    result,
                    created_at
                FROM watering_log
                ORDER BY id DESC
                LIMIT 100
            """).fetchall()
        ]

@app.post("/api/watering")
def create_manual_watering(request: ManualWateringRequest):
    """GUI에서 특정 화분의 수동 급수 Task를 생성한다."""
    with get_db() as conn:
        # 존재하지 않는 화분인지 확인
        get_plant(conn, request.plant_id)

        # 해당 화분에 이미 진행 중인 Task가 있으면 중복 급수를 막는다.
        task_id = create_task(
            conn,
            request.plant_id,
            "MANUAL",
        )

        if task_id is None:
            raise HTTPException(
                status_code=409,
                detail="Active watering task already exists for this plant",
            )

        return {
            "task_id": task_id,
            "plant_id": request.plant_id,
            "status": "QUEUED",
            "source": "MANUAL",
        }