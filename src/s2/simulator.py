"""
S2-01: Simulated data generation.
Provides the same read()/status() interface as S1SensorService
so S2-02 core can use either real or simulated data transparently.
"""

from __future__ import annotations

import math
import random
import time
import threading
from typing import List, Optional

from src.s1.ble_tunnel import SensorSample, SensorStatus


class SimulatedSensorService:
    """Generates fake IMU data mimicking multiple sensors.

    Simulates sinusoidal joint motion for testing the full pipeline
    without real BLE hardware.
    """

    def __init__(
        self,
        sensor_ids: Optional[List[dict]] = None,
        sample_rate_hz: float = 50.0,
    ) -> None:
        if sensor_ids is None:
            sensor_ids = [
                {"deviceId": "SIM_SENSOR_A", "deviceName": "SimUpperLeg"},
                {"deviceId": "SIM_SENSOR_B", "deviceName": "SimLowerLeg"},
            ]

        self._sensors = sensor_ids
        self._interval = 1.0 / sample_rate_hz
        self._lock = threading.Lock()
        self._buffer: List[SensorSample] = []
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._start_time = 0.0

    def start_generation(self) -> None:
        if self._running:
            return
        self._running = True
        self._start_time = time.time()
        self._thread = threading.Thread(
            target=self._generate_loop, daemon=True, name="sim-data-gen"
        )
        self._thread.start()

    def stop_generation(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None

    def read(self) -> List[SensorSample]:
        """Same interface as s1.sensor.read()."""
        with self._lock:
            samples = list(self._buffer)
            self._buffer.clear()
            return samples

    def status(self) -> SensorStatus:
        """Same interface as s1.sensor.status()."""
        return SensorStatus(
            connected=self._running,
            errorMessage=None if self._running else "sensor_disconnected",
        )

    def _generate_loop(self) -> None:
        while self._running:
            t = time.time()
            elapsed = t - self._start_time

            for sensor_info in self._sensors:
                sample = self._generate_sample(
                    elapsed, sensor_info["deviceId"], sensor_info["deviceName"]
                )
                with self._lock:
                    self._buffer.append(sample)

            time.sleep(self._interval)

    def _generate_sample(
        self, elapsed: float, device_id: str, device_name: str
    ) -> SensorSample:
        freq = 0.5  # 0.5 Hz oscillation (one cycle per 2 seconds)
        phase = hash(device_id) % 100 * 0.01 * math.pi  # different phase per sensor
        noise = random.gauss(0, 0.5)

        angle_base = 45.0 * math.sin(2 * math.pi * freq * elapsed + phase)

        return SensorSample(
            timestamp=int(time.time() * 1000),
            deviceId=device_id,
            deviceName=device_name,
            accX=round(math.sin(elapsed) * 0.2 + noise * 0.01, 4),
            accY=round(math.cos(elapsed) * 0.3 + noise * 0.01, 4),
            accZ=round(0.98 + noise * 0.01, 4),
            gyroX=round(noise * 2, 2),
            gyroY=round(noise * 3, 2),
            gyroZ=round(noise * 1.5, 2),
            roll=round(angle_base + noise, 2),
            pitch=round(angle_base * 0.8 + noise, 2),
            yaw=round(angle_base * 0.3 + noise, 2),
        )


class SimulatedS1SessionService:
    """Session wrapper for simulator, matching s1.session interface."""

    def __init__(self, sim_service: SimulatedSensorService) -> None:
        self._sim = sim_service

    def start(self, sessionMetaData: dict) -> dict:
        self._sim.start_generation()
        return {"success": True, "error": None}

    def stop(self) -> dict:
        self._sim.stop_generation()
        return {"success": True, "error": None}


class SimulatedS1Module:
    """Drop-in replacement for S1Module when using simulated data."""

    def __init__(
        self,
        sensor_ids: Optional[List[dict]] = None,
        sample_rate_hz: float = 50.0,
    ) -> None:
        sim_service = SimulatedSensorService(
            sensor_ids=sensor_ids, sample_rate_hz=sample_rate_hz
        )
        self.sensor = sim_service
        self.session = SimulatedS1SessionService(sim_service)
