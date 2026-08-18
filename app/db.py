"""SQLite 연결 관리와 서버 시작 시 실행되는 초기 데이터 구성."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from .config import DB_PATH


def now_iso() -> str:
    """DB에 저장할 공통 시간 형식을 반환한다."""
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def get_db():
    """요청 단위 DB 연결을 열고, 정상 종료 시 변경 내용을 저장한다."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """필요한 테이블과 최초 실행용 화분·장치 상태를 준비한다."""
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS plants (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            position INTEGER NOT NULL,
            moisture REAL NOT NULL DEFAULT 0,
            threshold REAL NOT NULL DEFAULT 30,
            status TEXT NOT NULL DEFAULT 'UNKNOWN',
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS moisture_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plant_id INTEGER NOT NULL,
            moisture REAL NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS watering_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plant_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            source TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            error_message TEXT
        );

        CREATE TABLE IF NOT EXISTS watering_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            plant_id INTEGER NOT NULL,
            result TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS agv_status (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            state TEXT NOT NULL DEFAULT 'IDLE',
            position INTEGER NOT NULL DEFAULT 0,
            battery REAL NOT NULL DEFAULT 100,
            current_task_id INTEGER,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS watering_device_status (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            state TEXT NOT NULL DEFAULT 'IDLE',
            pump INTEGER NOT NULL DEFAULT 0,
            current_task_id INTEGER,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS system_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            message TEXT NOT NULL,
            task_id INTEGER,
            created_at TEXT NOT NULL
        );
        """)

        # 화분 데이터는 DB가 비어 있는 최초 실행 때만 넣는다.
        # 이후 서버를 재시작해도 실제 센서값과 상태는 유지된다.
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM plants"
        ).fetchone()["c"]

        if count == 0:
            plants = [
                (1, "화분 1", 0, 50, 30, "NORMAL", now_iso()),
                (2, "화분 2", 100, 50, 30, "NORMAL", now_iso()),
            ]
            conn.executemany("""
                INSERT INTO plants
                (id, name, position, moisture, threshold, status, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, plants)

        # AGV와 급수 모터 Arduino2는 각각 한 대를 관리하므로 id=1인 상태 행을 사용한다.
        ts = now_iso()

        conn.execute("""
            INSERT OR IGNORE INTO agv_status
            (id, state, position, battery, current_task_id, updated_at)
            VALUES (1, 'IDLE', 0, 100, NULL, ?)
        """, (ts,))

        conn.execute("""
            INSERT OR IGNORE INTO watering_device_status
            (id, state, pump, current_task_id, updated_at)
            VALUES (1, 'IDLE', 0, NULL, ?)
        """, (ts,))
