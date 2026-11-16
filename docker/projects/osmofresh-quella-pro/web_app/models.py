from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


CONFIRMED_TEMPERATURES = (25, 45, 55, 80, 90, 95)


class DispenseRequest(BaseModel):
    ml: int = Field(ge=1, le=65535)
    temperature_c: int

    @field_validator("temperature_c")
    @classmethod
    def confirmed_temperature(cls, value: int) -> int:
        if value not in CONFIRMED_TEMPERATURES:
            raise ValueError("temperature is not a confirmed Mega setpoint")
        return value


class TankDelayRequest(BaseModel):
    enabled: bool
    delay_ms: int = Field(ge=0, le=60000)


class PresetRequest(BaseModel):
    id: int = Field(ge=0, le=7)
    ml: int = Field(ge=1, le=65535)
    temperature_c: int = 25

    @field_validator("temperature_c")
    @classmethod
    def confirmed_temperature(cls, value: int) -> int:
        if value not in CONFIRMED_TEMPERATURES:
            raise ValueError("temperature is not a confirmed Mega setpoint")
        return value


class OverrideRequest(BaseModel):
    type: str = "VOLUME_PRESET"
    source_value: int = Field(ge=1, le=65535)
    target_value: int = Field(ge=1, le=65535)
    enabled: bool = True

    @field_validator("type")
    @classmethod
    def supported_type(cls, value: str) -> str:
        if value != "VOLUME_PRESET":
            raise ValueError("only VOLUME_PRESET is currently confirmed")
        return value

