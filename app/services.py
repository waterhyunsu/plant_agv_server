"""여러 API에서 함께 쓰는 Task, 로그, DB 조회 비즈니스 로직."""

from typing import Optional
from fastapi import HTTPException

from .db import now_iso


ACTIVE_TASK_STATUSES = (
    "QUEUED",
    "MOVING",
    "ARRIVED",
    "WATERING",
)


def log_event(conn, event_type: str, message: str, task_id: Optional[int] = None):
    """현재 요청의 DB 연결로 시스템 이벤트를 남긴다.

    별도 DB 연결을 만들지 않아 같은 요청의 Task 상태 변경과 로그가 함께 저장된다.
    """
    conn.execute("""
            INSERT INTO system_log
            (event_type, message, task_id, created_at)
            VALUES (?, ?, ?, ?)
        """, (event_type, message, task_id, now_iso()))


def get_plant(conn, plant_id: int):
    """화분을 조회하고, 존재하지 않으면 API에 404 응답을 반환한다."""
    row = conn.execute(
        "SELECT * FROM plants WHERE id=?",
        (plant_id,)
    ).fetchone()

    if row is None:
        raise HTTPException(404, "Plant not found")

    return row


def has_active_task(conn, plant_id: int) -> bool:
    """같은 화분에서 아직 끝나지 않은 급수 작업이 있는지 확인한다."""
    qmarks = ",".join("?" for _ in ACTIVE_TASK_STATUSES)
    row = conn.execute(
        f"""
        SELECT 1
        FROM watering_tasks
        WHERE plant_id=?
          AND status IN ({qmarks})
        LIMIT 1
        """,
        (plant_id, *ACTIVE_TASK_STATUSES),
    ).fetchone()

    return row is not None


def create_task(conn, plant_id: int, amount_ml: float, source: str):
    """중복을 검사한 뒤 대기(QUEUED) 상태의 급수 Task를 생성한다."""
    ts = now_iso()

    # 센서가 같은 DRY 값을 반복 전송하거나 GUI가 중복 요청해도 중복 급수를 막는다.
    if has_active_task(conn, plant_id):
        return None

    cur = conn.execute("""
        INSERT INTO watering_tasks
        (plant_id, amount_ml, status, source, created_at, updated_at)
        VALUES (?, ?, 'QUEUED', ?, ?, ?)
    """, (plant_id, amount_ml, source, ts, ts))

    task_id = cur.lastrowid

    conn.execute("""
        INSERT INTO system_log
        (event_type, message, task_id, created_at)
        VALUES (?, ?, ?, ?)
    """, (
        "TASK_CREATED",
        f"watering task created for plant {plant_id}",
        task_id,
        ts,
    ))

    return task_id


def get_task(conn, task_id: int):
    """Task를 조회하고, 존재하지 않으면 API에 404 응답을 반환한다."""
    row = conn.execute(
        "SELECT * FROM watering_tasks WHERE id=?",
        (task_id,)
    ).fetchone()

    if row is None:
        raise HTTPException(404, "Task not found")

    return row


def get_active_task(conn):
    """AGV가 처리할 가장 오래된 진행 중 Task와 목적지 정보를 반환한다."""
    qmarks = ",".join("?" for _ in ACTIVE_TASK_STATUSES)
    return conn.execute(
        f"""
        SELECT
            t.*,
            p.position AS target_position,
            p.name AS plant_name
        FROM watering_tasks t
        JOIN plants p ON p.id=t.plant_id
        WHERE t.status IN ({qmarks})
        ORDER BY t.id ASC
        LIMIT 1
        """,
        ACTIVE_TASK_STATUSES,
    ).fetchone()


def set_task_status(conn, task_id: int, status: str, error_message=None):
    """Task 상태와 갱신 시각을 변경하고, 오류가 있으면 메시지를 함께 남긴다."""
    conn.execute("""
        UPDATE watering_tasks
        SET status=?, updated_at=?, error_message=?
        WHERE id=?
    """, (status, now_iso(), error_message, task_id))


def complete_task(conn, task_id: int, plant_id: int, amount_ml: float):
    """완료된 급수를 이력과 시스템 로그에 기록한다."""
    ts = now_iso()

    conn.execute("""
        INSERT INTO watering_log
        (task_id, plant_id, amount_ml, result, created_at)
        VALUES (?, ?, ?, 'SUCCESS', ?)
    """, (task_id, plant_id, amount_ml, ts))

    conn.execute("""
        INSERT INTO system_log
        (event_type, message, task_id, created_at)
        VALUES (?, ?, ?, ?)
    """, (
        "WATERING_COMPLETE",
        f"watering completed for plant {plant_id}, target {amount_ml} mL",
        task_id,
        ts,
    ))

    # 실제 수분값은 센서가 다시 보고해야 하므로
    # 서버가 moisture 값을 임의로 NORMAL로 바꾸지는 않는다.
