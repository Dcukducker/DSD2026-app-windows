"""
S2 session service — the external facade of S2.
Exposes s2.session.start/stop and s2.data.read per IS §2.3 and §2.4.

Internally supports dual data sources (S2-01 simulator / S1 real BLE),
switchable at runtime before starting a session.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from src.s1.ble_tunnel import SensorSample
from src.s2.async_buffer import AsyncBuffer
from src.s2.core import BINDMODE_BACK, BINDMODE_PORT, BINDMODE_SCREEN, DataAcqCore
from src.s2.data_classes import (
    ErrorEvent,
    FormatData,
    SessionContext,
    SessionSummary,
    StartResult,
    TargetAngle,
)

LOGGER = logging.getLogger("s2.session")


class S2SessionControl:
    """IF-M1-S2: s2.session.start() and s2.session.stop()."""

    def __init__(self, s1_real_module, s1_sim_module) -> None:
        """
        Args:
            s1_real_module: Real BLE S1 module (may be None if no BLE config).
            s1_sim_module: Simulated S1 module (always available).
        """
        self._s1_real = s1_real_module
        self._s1_sim = s1_sim_module
        self._use_simulator = s1_real_module is None  # default to real if available
        self._active = False
        self._core: Optional[DataAcqCore] = None
        self._buffer: Optional[AsyncBuffer] = None
        self._session_context: Optional[SessionContext] = None
        self._start_time: Optional[str] = None
        self._current_s1 = None  # which s1 module is in use for current session

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def buffer(self) -> Optional[AsyncBuffer]:
        return self._buffer

    @property
    def use_simulator(self) -> bool:
        return self._use_simulator

    def set_mode(self, use_simulator: bool) -> None:
        """Switch data source mode. Must not be called during an active session."""
        if self._active:
            raise RuntimeError("Cannot switch mode during active session")
        if not use_simulator and self._s1_real is None:
            raise RuntimeError("No real BLE sensors configured. Please configure sensors first.")
        self._use_simulator = use_simulator
        LOGGER.info("Data source mode set to: %s", "simulator" if use_simulator else "real BLE")

    def set_real_s1(self, s1_real_module) -> None:
        """Set/replace the real BLE S1 module (e.g. after user configures sensors from UI)."""
        if self._active:
            raise RuntimeError("Cannot reconfigure sensors during active session")
        self._s1_real = s1_real_module
        LOGGER.info("Real S1 module updated")

    def start(
        self,
        sessionId: int,
        userId: int,
        sensorJointMapping: dict = None,
        payloadStatus: str = "",
    ) -> StartResult:
        """IS §2.3.1 — Start a data acquisition session.

        Raises ValueError if required parameters are invalid.
        """
        if sensorJointMapping is None:
            sensorJointMapping = {}

        if self._active:
            return StartResult(
                success=False, errorMessage="session_already_active"
            )

        # Select data source
        if self._use_simulator:
            self._current_s1 = self._s1_sim
        else:
            self._current_s1 = self._s1_real

        # Start S1 session (this initiates BLE connection for real sensors)
        s1_result = self._current_s1.session.start(
            {"sessionId": sessionId, "payloadStatus": payloadStatus}
        )
        if not s1_result.get("success"):
            return StartResult(
                success=False,
                errorMessage=s1_result.get("error", "s1_start_failed"),
            )

        # Build session context
        self._session_context = SessionContext(
            sessionId=sessionId,
            userId=userId,
            sensorJointMapping=sensorJointMapping,
            payloadStatus=payloadStatus,
        )

        # Create async buffer (S2-03)
        self._buffer = AsyncBuffer(self._session_context)

        # Parse bind mode from payloadStatus (format: "exercise_type-bind_mode")
        bind_mode = BINDMODE_PORT  # default
        if "-" in payloadStatus:
            parts = payloadStatus.rsplit("-", 1)
            if parts[1] in (BINDMODE_PORT, BINDMODE_SCREEN, BINDMODE_BACK):
                bind_mode = parts[1]

        # Create and start core (S2-02)
        data_source = self._current_s1.sensor
        self._core = DataAcqCore(
            data_source=data_source,
            sensor_joint_mapping=sensorJointMapping,
            bind_mode=bind_mode,
            on_samples=self._buffer.push_samples,
            on_angles=self._buffer.push_angles,
            on_error=self._buffer.push_error,
        )
        self._core.start()

        self._start_time = datetime.now(timezone.utc).isoformat()
        self._active = True
        LOGGER.info(
            "S2 session started: sessionId=%d userId=%d payloadStatus=%s mode=%s",
            sessionId, userId, payloadStatus,
            "simulator" if self._use_simulator else "real",
        )
        return StartResult(success=True, errorMessage=None)

    def stop(self) -> SessionSummary:
        """IS §2.3.2 — Stop the current session.

        Raises RuntimeError if no session is active.
        """
        if not self._active:
            raise RuntimeError("No active session")

        # Stop core
        self._core.stop()

        # Stop S1 session
        self._current_s1.session.stop()

        end_time = datetime.now(timezone.utc).isoformat()

        summary = SessionSummary(
            sessionId=self._session_context.sessionId,
            sampleCount=self._core.sample_count,
            errorCount=self._core.error_count,
            startTime=self._start_time,
            endTime=end_time,
        )

        LOGGER.info(
            "S2 session stopped: %d samples, %d errors",
            summary.sampleCount, summary.errorCount,
        )

        # Reset state — keep buffer alive so M1 can still drain remaining data.
        self._active = False
        self._core = None
        self._session_context = None
        self._start_time = None
        self._current_s1 = None

        return summary


class S2DataProvider:
    """IF-S2-M1: s2.data.read()."""

    def __init__(self, session_control: S2SessionControl) -> None:
        self._session_ctrl = session_control

    def read(self) -> FormatData:
        """IS §2.4.1 — Read all FormatData accumulated since the last call.

        Works during an active session and also after stop() to drain
        remaining buffered data (IS §2.3.2 "flush remaining buffered data").
        """
        buf = self._session_ctrl.buffer
        if buf is None:
            raise RuntimeError("No active session — cannot read data")
        return buf.drain("s2_data_read")


class S2Module:
    """S2 module facade.

    Attributes:
        session: S2SessionControl — s2.session.start() / s2.session.stop()
        data: S2DataProvider — s2.data.read()
    """

    def __init__(self, s1_real_module, s1_sim_module) -> None:
        """
        Args:
            s1_real_module: Real BLE S1 module (None if unavailable).
            s1_sim_module: Simulated S1 module.
        """
        self.session = S2SessionControl(s1_real_module, s1_sim_module)
        self.data = S2DataProvider(self.session)
