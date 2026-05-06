"""
IS-defined data classes for S2 module.
All field names, types, and structures strictly follow Total Interface Specification v1.0.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass(slots=True, frozen=True)
class StartResult:
    """IS §2.3.3 — Result of s2.session.start()."""

    success: bool
    errorMessage: Optional[str]


@dataclass(slots=True, frozen=True)
class SessionSummary:
    """IS §2.3.4 — Result of s2.session.stop()."""

    sessionId: int
    sampleCount: int
    errorCount: int
    startTime: str  # ISO 8601
    endTime: str  # ISO 8601


@dataclass(slots=True, frozen=True)
class SessionContext:
    """IS §2.4.3 — Session-level info, fixed for the entire session."""

    sessionId: int
    userId: int
    sensorJointMapping: dict
    payloadStatus: str


@dataclass(slots=True, frozen=True)
class TargetAngle:
    """IS §2.4.4 — Computed target angle."""

    timestamp: int  # Unix timestamp in milliseconds
    angleID: str
    angle: float  # degrees


@dataclass(slots=True, frozen=True)
class ErrorEvent:
    """IS §2.4.5 — Error event."""

    timestamp: int  # Unix timestamp in milliseconds
    sensorId: Optional[str]
    errorType: str  # "sensor_disconnected", "validation_failure", "timeout"
    message: str


@dataclass(slots=True)
class FormatData:
    """IS §2.4.2 — Output data structure of S2."""

    sessionContext: SessionContext
    sensorData: list  # List[SensorSample] - imported from S1
    targetAngles: List[TargetAngle]
    errors: List[ErrorEvent]
