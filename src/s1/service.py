"""
S1 module facade for S2 consumption.
Exposes s1.sensor (read/status) and s1.session (start/stop) per IS §2.1 and §2.2.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import List, Optional

from src.s1.ble_tunnel import (
    S1SensorService,
    SensorConfig,
    SensorSample,
    SensorStatus,
    ServiceConfig,
)

LOGGER = logging.getLogger("s1.service")


class S1SessionService:
    """IF-S2-S1 provider: s1.session.start(sessionMetaData) and s1.session.stop().

    Wraps the async S1SensorService start/stop into synchronous calls
    that S2 can invoke directly. Recreates the underlying SensorService
    on each start() to ensure BLE workers are in a fresh state.
    """

    def __init__(
        self,
        sensor_configs: list[SensorConfig],
        service_cfg: ServiceConfig,
        sensor_service: S1SensorService,
    ) -> None:
        self._sensor_configs = sensor_configs
        self._service_cfg = service_cfg
        self._sensor_service = sensor_service
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self, sessionMetaData: dict) -> dict:
        """IS §2.2.1 — Start raw data collection on S1.

        Returns confirmation signal on success; error signal on failure.
        """
        if self._running:
            return {"success": False, "error": "session_already_active"}

        try:
            # Recreate sensor service to get fresh BLE workers
            self._sensor_service = S1SensorService(
                sensors=self._sensor_configs,
                service_cfg=self._service_cfg,
            )

            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._run_loop, daemon=True, name="s1-event-loop"
            )
            self._thread.start()

            future = asyncio.run_coroutine_threadsafe(
                self._sensor_service.start(), self._loop
            )
            future.result(timeout=30)
            self._running = True
            LOGGER.info("S1 session started with metadata: %s", sessionMetaData)
            return {"success": True, "error": None}
        except Exception as exc:
            LOGGER.exception("Failed to start S1 session: %s", exc)
            self._cleanup_loop()
            return {"success": False, "error": str(exc)}

    def stop(self) -> dict:
        """IS §2.2.2 — Stop raw data collection on S1.

        Returns confirmation signal on success; error signal on failure.
        """
        if not self._running:
            return {"success": False, "error": "no_active_session"}

        try:
            future = asyncio.run_coroutine_threadsafe(
                self._sensor_service.stop(), self._loop
            )
            future.result(timeout=30)
            self._running = False
            self._cleanup_loop()
            LOGGER.info("S1 session stopped")
            return {"success": True, "error": None}
        except Exception as exc:
            LOGGER.exception("Failed to stop S1 session: %s", exc)
            self._running = False
            self._cleanup_loop()
            return {"success": False, "error": str(exc)}

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _cleanup_loop(self) -> None:
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._loop = None
        self._thread = None


class S1SensorProxy:
    """Proxy that always delegates to the latest S1SensorService instance.

    Since S1SessionService recreates the SensorService on each start(),
    S2 core needs to read from the current instance, not a stale one.
    """

    def __init__(self, session_service: S1SessionService) -> None:
        self._session = session_service

    def read(self) -> list[SensorSample]:
        return self._session._sensor_service.read()

    def status(self) -> SensorStatus:
        return self._session._sensor_service.status()


class S1Module:
    """Convenience wrapper so S2 can call s1.sensor.read()/status()
    and s1.session.start()/stop().

    Attributes:
        sensor: provides read() -> List[SensorSample] and status() -> SensorStatus
        session: provides start(sessionMetaData) and stop()
    """

    def __init__(
        self,
        sensor_configs: list[SensorConfig],
        service_cfg: ServiceConfig,
    ) -> None:
        sensor_service = S1SensorService(
            sensors=sensor_configs, service_cfg=service_cfg
        )
        self.session = S1SessionService(sensor_configs, service_cfg, sensor_service)
        self.sensor = S1SensorProxy(self.session)


def create_s1_module(
    sensors: list[SensorConfig],
    service_cfg: Optional[ServiceConfig] = None,
) -> S1Module:
    """Build the S1 module with both sensor and session interfaces."""
    return S1Module(
        sensor_configs=sensors,
        service_cfg=service_cfg or ServiceConfig(),
    )
