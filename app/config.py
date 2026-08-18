"""서버 실행 환경별 설정값을 모아 둔 모듈."""

import os

# 환경변수를 설정하면 배포 PC마다 DB 위치와 기본 급수량을 바꿀 수 있다.
# 설정하지 않은 개발 환경에서는 아래 기본값을 사용한다.
DB_PATH = os.getenv("DB_PATH", "plant_agv.db")
DEFAULT_WATERING_AMOUNT_ML = float(
    os.getenv("DEFAULT_WATERING_AMOUNT_ML", "80")
)
