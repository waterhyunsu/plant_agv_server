"""Plant Watering AGV 중앙 서버의 HTTP REST API.

센서, AGV, 급수 모터 Arduino2, GUI는 이 파일의 API를 통해 상태와 명령을 주고받는다.
"""

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
    ACTIVE_TASK_STATUSES,
    create_task,
    get_active_task,
    get_plant,
    get_task,
    set_task_status,
    complete_task,
    log_event,
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
    allow_credentials=True,
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
            SELECT state, position, battery, current_task_id, updated_at
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
    """센서 수분값을 저장하고 NORMAL→DRY 전환 시 자동 Task를 만든다."""
    with get_db() as conn:
        plant = get_plant(conn, plant_id)
        # 임계값 미만일 때만 DRY로 판단한다.
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


# =========================================================
# AGV
# =========================================================

@app.get("/api/agv/status")
def get_agv_status():
    """현재 AGV의 상태, 위치, 배터리 및 수행 중 Task를 반환한다."""
    with get_db() as conn:
        return dict(conn.execute("""
            SELECT state, position, battery, current_task_id, updated_at
            FROM agv_status
            WHERE id=1
        """).fetchone())


@app.get("/api/agv/command")
def get_agv_command():
    """AGV가 polling하여 다음 이동 명령을 받는 API."""
    with get_db() as conn:
        task = get_active_task(conn)

        if task is None:
            return {"command": "WAIT"}

        # 처음 명령을 가져간 순간 Task를 MOVING으로 변경한다.
        # 이후 polling에는 같은 task_id를 반환해 AGV가 명령을 계속 확인할 수 있다.
        if task["status"] == "QUEUED":
            set_task_status(conn, task["id"], "MOVING")

            conn.execute("""
                UPDATE agv_status
                SET state='MOVING',
                    current_task_id=?,
                    updated_at=?
                WHERE id=1
            """, (task["id"], now_iso()))

            # report는 이 함수에 존재하지 않는다. 출동 시에는 Task id로 이벤트를 기록한다.
            log_event(
                conn,
                "AGV_DISPATCH",
                f"AGV dispatched to plant {task['plant_id']}",
                task["id"],
            )

        elif task["status"] == "MOVING":
            pass

        else:
            return {"command": "WAIT"}

        return {
            "command": "GO_TO_PLANT",
            "task_id": task["id"],
            "plant_id": task["plant_id"],
            "target_position": task["target_position"],
        }


@app.post("/api/agv/telemetry")
def agv_telemetry(report: AGVTelemetry):
    """AGV의 이동·도착·오류를 저장하고 Task 상태를 다음 단계로 넘긴다."""
    with get_db() as conn:
        ts = now_iso()

        conn.execute("""
            UPDATE agv_status
            SET state=?, position=?, battery=?,
                current_task_id=?, updated_at=?
            WHERE id=1
        """, (
            report.state,
            report.position,
            report.battery,
            report.task_id,
            ts,
        ))

        if report.task_id is None:
            return {"ok": True}

        task = get_task(conn, report.task_id)

        # 도착 보고가 오면 급수 모터 Arduino2가 WATER 명령을 가져갈 수 있는 ARRIVED 상태가 된다.
        if report.state == "ARRIVED":
            if task["status"] in ("MOVING", "QUEUED"):
                set_task_status(conn, report.task_id, "ARRIVED")

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
            set_task_status(conn, task["id"], "WATERING")

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
def watering_device_telemetry(report: WateringDeviceTelemetry):
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
                set_task_status(conn, report.task_id, "WATERING")

        elif report.state == "COMPLETED":
            # WATERING 상태의 Task만 완료 처리해 중복 로그 생성을 막는다.
            if task["status"] != "WATERING":
                raise HTTPException(
                    409,
                    "Task is not in WATERING state",
                )

            set_task_status(conn, report.task_id, "COMPLETED")
            complete_task(
                conn,
                report.task_id,
                task["plant_id"],
            )

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


@app.post("/api/watering")
def create_manual_watering(request: ManualWateringRequest):
    """GUI의 강제 급수 요청을 Task로 등록한다. AGV를 직접 제어하지는 않는다."""
    with get_db() as conn:
        get_plant(conn, request.plant_id)

        task_id = create_task(
            conn,
            request.plant_id,
            "MANUAL",
        )

        if task_id is None:
            raise HTTPException(
                409,
                "Active watering task already exists for this plant",
            )

        return {
            "task_id": task_id,
            "plant_id": request.plant_id,
            "status": "QUEUED",
        }


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
