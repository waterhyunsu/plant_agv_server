# Plant Watering AGV 중앙 서버의 HTTP REST API.
# 센서, AGV, 급수 모터 Arduino2, GUI는 이 파일의 API를 통해 상태와 명령을 주고받는다.

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .config import DEFAULT_WATERING_AMOUNT_ML

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
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================================================
# Health / Dashboard
# =========================================================

@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "service": "plant-agv-server",
    }


@app.get("/api/dashboard")
def dashboard():
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
            SELECT state, current_task_id, updated_at
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
    with get_db() as conn:
        plant = get_plant(conn, plant_id)

        new_status = (
            "DRY"
            if report.moisture < plant["threshold"]
            else "NORMAL"
        )

        previous_status = plant["status"]
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

        if new_status == "DRY" and previous_status != "DRY":
            task_id = create_task(
                conn,
                plant_id,
                "AUTO",
                DEFAULT_WATERING_AMOUNT_ML,
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
    with get_db() as conn:
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

        return [
            dict(row)
            for row in reversed(rows)
        ]


@app.get("/api/plants/{plant_id}/moisture/history")
def get_moisture_history_api(plant_id: int, date: str):
    with get_db() as conn:
        get_plant(conn, plant_id)
        return get_moisture_history(conn, plant_id, date)


# =========================================================
# System Logs
# =========================================================

@app.get("/api/system/logs")
def get_logs():
    with get_db() as conn:
        return get_system_logs(conn)


# =========================================================
# AGV
# =========================================================

@app.get("/api/agv/status")
def get_agv_status():
    with get_db() as conn:
        return dict(conn.execute("""
            SELECT state, current_task_id, updated_at
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

        # 1. 수행해야 할 활성 Task(QUEUED 또는 MOVING) 조회
        task = get_active_task(conn)

        if task is not None:
            # 1-1. 신규 Task인 경우 (QUEUED -> MOVING 전환 및 GO 명령 발송)
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

                return {
                    "command": "GO",
                    "task_id": task["id"],
                    "plant_id": task["plant_id"],
                }

            # 1-2. 이미 MOVING 중인 Task인 경우
            elif task["status"] == "MOVING":
                return {
                    "command": "GO",
                    "task_id": task["id"],
                    "plant_id": task["plant_id"],
                }

            # 1-3. 화분에 도착해서 급수 중인 경우 (ARRIVED, WATERING) ➔ AGV는 제자리 대기
            else:
                return {"command": "WAIT"}

        # 2. 더 이상 처리할 활성 Task가 없고, 이전 Task가 COMPLETED된 경우 ➔ RETURN 처리
        if agv["current_task_id"] is not None:
            completed_task = conn.execute("""
                SELECT id, status
                FROM watering_tasks
                WHERE id=?
            """, (agv["current_task_id"],)).fetchone()

            if (
                completed_task is not None
                and completed_task["status"] == "COMPLETED"
                and agv["state"] != "RETURN"
            ):
                conn.execute("""
                    UPDATE agv_status
                    SET state='RETURN',
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
    with get_db() as conn:
        ts = now_iso()

        if report.task_id is None:
            conn.execute("""
                UPDATE agv_status
                SET state=?, updated_at=?
                WHERE id=1
            """, (report.state, ts))
            return {
                "ok": True,
                "task_id": None,
                "state": report.state,
            }

        task = get_task(conn, report.task_id)

        if report.state == "STOP":
            if task and task["status"] in ("MOVING", "QUEUED"):
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
                
                conn.execute("""
                    UPDATE agv_status
                    SET state=?, current_task_id=?, updated_at=?
                    WHERE id=1
                """, (report.state, report.task_id, ts))

            elif task and task["status"] == "COMPLETED":
                conn.execute("""
                    UPDATE agv_status
                    SET state=?, current_task_id=NULL, updated_at=?
                    WHERE id=1
                """, (report.state, ts))
                
                log_event(
                    conn,
                    "AGV_HOME",
                    "AGV returned to home position and stopped.",
                    report.task_id,
                )
            else:
                conn.execute("""
                    UPDATE agv_status
                    SET state=?, current_task_id=?, updated_at=?
                    WHERE id=1
                """, (report.state, report.task_id, ts))

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
            conn.execute("""
                UPDATE agv_status
                SET state=?, current_task_id=?, updated_at=?
                WHERE id=1
            """, (report.state, report.task_id, ts))
        else:
            conn.execute("""
                UPDATE agv_status
                SET state=?, current_task_id=?, updated_at=?
                WHERE id=1
            """, (report.state, report.task_id, ts))

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
            "amount_ml": task["amount_ml"]
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
            if task["status"] == "COMPLETED":
                return {
                    "ok": True,
                    "task_id": report.task_id,
                    "state": report.state,
                    "note": "already completed",
                }

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
    with get_db() as conn:
        get_plant(conn, request.plant_id)

        task_id = create_task(
            conn,
            request.plant_id,
            "MANUAL",
            DEFAULT_WATERING_AMOUNT_ML,
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