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


# =========================================================
# System Log
# =========================================================

def log_event(
    conn,
    event_type: str,
    message: str,
    task_id: Optional[int] = None,
):
    """
    현재 요청의 DB 연결로 시스템 이벤트를 남긴다.

    별도 DB 연결을 만들지 않아 같은 요청의 Task 상태 변경과
    로그가 함께 저장된다.
    """
    conn.execute("""
        INSERT INTO system_log
        (event_type, message, task_id, created_at)
        VALUES (?, ?, ?, ?)
    """, (
        event_type,
        message,
        task_id,
        now_iso(),
    ))


def get_system_logs(conn, limit: int = 100):
    """
    최근 시스템 로그를 조회한다.

    DB에는 기존 event_type을 그대로 저장하고,
    GUI에서 사용하기 편하도록 source와 level을 계산해서 반환한다.

    level:
        ERROR → 오류 이벤트
        INFO  → 정상적인 시스템 이벤트

    source:
        AGV
        WATERING
        TASK
        SYSTEM
    """

    rows = conn.execute("""
        SELECT
            id,
            event_type,
            message,
            task_id,
            created_at
        FROM system_log
        ORDER BY id DESC
        LIMIT ?
    """, (limit,)).fetchall()

    logs = []

    for row in rows:
        event_type = row["event_type"]

        # ---------------------------------------------
        # 로그 source 분류
        # ---------------------------------------------
        if event_type.startswith("AGV_"):
            source = "AGV"

        elif event_type.startswith("WATERING_"):
            source = "WATERING"

        elif event_type.startswith("TASK_"):
            source = "TASK"

        else:
            source = "SYSTEM"

        # ---------------------------------------------
        # 로그 level 분류
        # ---------------------------------------------
        if event_type.endswith("_ERROR"):
            level = "ERROR"
        else:
            level = "INFO"

        logs.append({
            "id": row["id"],
            "timestamp": row["created_at"],
            "level": level,
            "source": source,
            "message": row["message"],
            "task_id": row["task_id"],
        })

    # DB에서는 최신 로그부터 조회되므로
    # GUI에서는 오래된 로그 → 최신 로그 순서로 반환한다.
    logs.reverse()

    return logs


# =========================================================
# Plant
# =========================================================

def get_plant(conn, plant_id: int):
    """화분을 조회하고, 존재하지 않으면 API에 404 응답을 반환한다."""
    row = conn.execute(
        "SELECT * FROM plants WHERE id=?",
        (plant_id,),
    ).fetchone()

    if row is None:
        raise HTTPException(
            404,
            "Plant not found",
        )

    return row


# =========================================================
# Moisture History
# =========================================================

def get_moisture_history(
    conn,
    plant_id: int,
    date: str,
):
    """
    특정 날짜의 전체 토양 수분 이력을
    5분 단위 평균으로 집계하여 반환한다.

    예:
        date = "2026-08-21"

    하루 전체를 다음과 같이 5분 단위로 집계한다.

        00:00~00:04 → 00:00
        00:05~00:09 → 00:05
        00:10~00:14 → 00:10
        ...
        23:55~23:59 → 23:55

    DB에는 1분 원본 데이터를 그대로 유지한다.
    """

    from datetime import datetime, timedelta

    # 날짜 형식 검증
    try:
        date_dt = datetime.strptime(
            date,
            "%Y-%m-%d",
        )
    except ValueError:
        raise HTTPException(
            400,
            "Invalid date format. Use YYYY-MM-DD",
        )

    # 조회 시작: 해당 날짜 00:00:00
    start_at = date_dt.isoformat(
        timespec="seconds"
    )

    # 조회 종료: 다음 날 00:00:00
    # end는 미포함이므로 해당 날짜 전체가 조회된다.
    end_at = (
        date_dt + timedelta(days=1)
    ).isoformat(
        timespec="seconds"
    )

    rows = conn.execute("""
        SELECT
            plant_id,

            (
                strftime('%Y-%m-%dT%H:', created_at)
                ||
                printf(
                    '%02d:00',
                    (
                        CAST(
                            strftime('%M', created_at)
                            AS INTEGER
                        ) / 5
                    ) * 5
                )
            ) AS bucket_start,

            AVG(moisture) AS moisture

        FROM moisture_log

        WHERE plant_id=?
          AND created_at >= ?
          AND created_at < ?

        GROUP BY bucket_start
        ORDER BY bucket_start ASC
    """, (
        plant_id,
        start_at,
        end_at,
    )).fetchall()

    return [
        {
            "plant_id": row["plant_id"],
            "measured_at": row["bucket_start"],
            "moisture": round(
                float(row["moisture"]),
                2,
            ),
        }
        for row in rows
    ]


# =========================================================
# Task
# =========================================================

def has_active_task(conn, plant_id: int) -> bool:
    """같은 화분에서 아직 끝나지 않은 급수 작업이 있는지 확인한다."""
    qmarks = ",".join(
        "?" for _ in ACTIVE_TASK_STATUSES
    )

    row = conn.execute(
        f"""
        SELECT 1
        FROM watering_tasks
        WHERE plant_id=?
          AND status IN ({qmarks})
        LIMIT 1
        """,
        (
            plant_id,
            *ACTIVE_TASK_STATUSES,
        ),
    ).fetchone()

    return row is not None


def create_task(
    conn,
    plant_id: int,
    source: str,
    amount_ml: float,
):
    """중복을 검사한 뒤 대기(QUEUED) 상태의 급수 Task를 생성한다."""
    ts = now_iso()

    # 센서가 같은 DRY 값을 반복 전송하거나
    # GUI가 중복 요청해도 중복 급수를 막는다.
    if has_active_task(conn, plant_id):
        return None

    cur = conn.execute("""
        INSERT INTO watering_tasks
        (plant_id, status, source, amount_ml, created_at, updated_at)
        VALUES (?, 'QUEUED', ?, ?, ?, ?)
    """, (
        plant_id,
        source,
        amount_ml,
        ts,
        ts,
    ))

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
        (task_id,),
    ).fetchone()

    if row is None:
        raise HTTPException(
            404,
            "Task not found",
        )

    return row


def get_active_task(conn):
    """AGV가 처리할 가장 오래된 활성 Task를 반환한다."""
    qmarks = ",".join(
        "?" for _ in ACTIVE_TASK_STATUSES
    )

    return conn.execute(
        f"""
        SELECT *
        FROM watering_tasks
        WHERE status IN ({qmarks})
        ORDER BY id ASC
        LIMIT 1
        """,
        ACTIVE_TASK_STATUSES,
    ).fetchone()


def set_task_status(
    conn,
    task_id: int,
    status: str,
    error_message=None,
):
    """Task 상태와 갱신 시각을 변경하고, 오류가 있으면 메시지를 함께 남긴다."""
    conn.execute("""
        UPDATE watering_tasks
        SET status=?,
            updated_at=?,
            error_message=?
        WHERE id=?
    """, (
        status,
        now_iso(),
        error_message,
        task_id,
    ))


def complete_task(
    conn,
    task_id: int,
    plant_id: int,
):
    """완료된 급수를 이력과 시스템 로그에 기록한다."""
    ts = now_iso()

    # 어느 화분의 급수가 언제 성공했는지만 기록한다.
    conn.execute("""
        INSERT INTO watering_log
        (task_id, plant_id, result, created_at)
        VALUES (?, ?, 'SUCCESS', ?)
    """, (
        task_id,
        plant_id,
        ts,
    ))

    conn.execute("""
        INSERT INTO system_log
        (event_type, message, task_id, created_at)
        VALUES (?, ?, ?, ?)
    """, (
        "WATERING_COMPLETE",
        f"watering completed for plant {plant_id}",
        task_id,
        ts,
    ))

    # 실제 수분 상태는 급수 후 센서가 다시 전송한 값으로 갱신한다.