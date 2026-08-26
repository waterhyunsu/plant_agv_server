"""장치와 GUI가 서버에 보내는 JSON 요청 형식 및 입력값 검증 규칙."""

from typing import Literal, Optional
from pydantic import BaseModel, Field


# 허용 상태를 제한해 오탈자나 정의되지 않은 상태값이 DB에 저장되는 것을 막는다.
AGVState = Literal["STOP", "GO", "TURN", "ERROR"]
WateringDeviceState = Literal["IDLE", "WATERING", "COMPLETED", "ERROR"]


class MoistureReport(BaseModel):
    """토양수분 센서의 측정값(백분율)."""
    moisture: float = Field(ge=0, le=100)


class ManualWateringRequest(BaseModel):
    """GUI에서 생성하는 강제 급수 요청."""
    plant_id: int


class AGVTelemetry(BaseModel):
    """AGV가 이동 과정 또는 오류를 서버에 알릴 때 사용하는 형식."""
    task_id: Optional[int] = None
    state: AGVState
    error_message: Optional[str] = None


class WateringDeviceTelemetry(BaseModel):
    """급수 모터 Arduino2가 급수 시작·완료·오류를 서버에 알릴 때 사용하는 형식."""
    task_id: Optional[int] = None
    state: WateringDeviceState
    error_message: Optional[str] = None
