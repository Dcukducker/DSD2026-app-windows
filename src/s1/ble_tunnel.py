"""
DSD-S1
Contributors: Zhihang Yu, Derui Tang, Haoqi Sheng, Mofan Xu, Silva André
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice


LOGGER = logging.getLogger("dsd_s1_if_s1_s2")
ERROR_PRIORITY = {
    "sensor_disconnected": 0,
    "timeout": 1,
    "data_corruption": 2,
}


@dataclass(slots=True, frozen=True)
class SensorSample:
    """IF-S1-S2 payload item defined by section 2.1.3 of the interface spec."""

    timestamp: int
    deviceId: str
    deviceName: str
    accX: float
    accY: float
    accZ: float
    gyroX: float
    gyroY: float
    gyroZ: float
    roll: float
    pitch: float
    yaw: float

    def to_dict(self) -> dict[str, object]:
        return {
            "timestamp": self.timestamp,
            "deviceId": self.deviceId,
            "deviceName": self.deviceName,
            "accX": self.accX,
            "accY": self.accY,
            "accZ": self.accZ,
            "gyroX": self.gyroX,
            "gyroY": self.gyroY,
            "gyroZ": self.gyroZ,
            "roll": self.roll,
            "pitch": self.pitch,
            "yaw": self.yaw,
        }


@dataclass(slots=True, frozen=True)
class SensorStatus:
    """IF-S1-S2 payload item defined by section 2.1.4 of the interface spec."""

    connected: bool
    errorMessage: Optional[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "connected": self.connected,
            "errorMessage": self.errorMessage,
        }


@dataclass(slots=True, frozen=True)
class SensorConfig:
    device_address: str
    notify_char_uuid: str


@dataclass(slots=True)
class ServiceConfig:
    scan_timeout_sec: float = 8.0
    reconnect_delay_sec: float = 2.0
    no_data_timeout_sec: float = 2.0


class CsvSampleWriter:
    """Append sensor samples to a CSV file."""

    FIELDNAMES = [
        "timestamp",
        "deviceId",
        "deviceName",
        "accX",
        "accY",
        "accZ",
        "gyroX",
        "gyroY",
        "gyroZ",
        "roll",
        "pitch",
        "yaw",
    ]

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._header_written = self.path.exists() and self.path.stat().st_size > 0

    def write_samples(self, samples: list[SensorSample]) -> None:
        if not samples:
            return

        with self.path.open("a", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=self.FIELDNAMES)
            if not self._header_written:
                writer.writeheader()
                self._header_written = True
            for sample in samples:
                writer.writerow(sample.to_dict())


class ThreadSafeSampleBuffer:
    """Drainable thread-safe queue used by IF-S1-S2 read()."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: deque[SensorSample] = deque()

    def push(self, sample: SensorSample) -> None:
        with self._lock:
            self._items.append(sample)

    def drain(self) -> list[SensorSample]:
        with self._lock:
            drained = list(self._items)
            self._items.clear()
            return drained


class SensorState:
    """Thread-safe state holder for per-sensor status + sample buffering."""

    def __init__(self) -> None:
        self.buffer = ThreadSafeSampleBuffer()
        self._lock = threading.Lock()
        self.connected = False
        self.producing = False
        self.error_message: Optional[str] = None

    def set_connected(self, connected: bool) -> None:
        with self._lock:
            self.connected = connected
            if connected:
                self.producing = False
                self.error_message = None
            else:
                self.producing = False
                self.error_message = "sensor_disconnected"

    def mark_producing(self) -> None:
        with self._lock:
            self.producing = True
            if self.error_message in {"timeout", "sensor_disconnected"}:
                self.error_message = None

    def mark_timeout(self) -> None:
        with self._lock:
            self.producing = False
            if self.connected:
                self.error_message = "timeout"

    def mark_data_corruption(self) -> None:
        with self._lock:
            self.error_message = "data_corruption"

    def clear_error_if_any(self) -> None:
        with self._lock:
            if self.error_message == "data_corruption":
                self.error_message = None

    def snapshot(self) -> tuple[bool, bool, Optional[str]]:
        with self._lock:
            return self.connected, self.producing, self.error_message


class WitMotionParser:
    """Incremental decoder for WitMotion BLE packets and legacy 0x55 serial frames."""

    SERIAL_FRAME_LEN = 11
    BLE_PACKET_LEN = 20
    MAX_RX_BUFFER = 4096

    def __init__(self) -> None:
        self._rx = bytearray()
        self._acc: Optional[tuple[float, float, float]] = None
        self._gyro: Optional[tuple[float, float, float]] = None
        self._angle: Optional[tuple[float, float, float]] = None

    def feed(self, payload: bytes, *, device_id: str, device_name: str) -> tuple[list[SensorSample], int]:
        samples: list[SensorSample] = []
        corruption_count = 0
        self._rx.extend(payload)
        self._trim_rx_if_needed()

        while True:
            if len(self._rx) < 2:
                break

            start = self._rx.find(0x55)
            if start < 0:
                # No frame header in current bytes: drop and mark as stream corruption.
                corruption_count += len(self._rx)
                self._rx.clear()
                break
            if start > 0:
                # Drop bytes before frame header; they cannot belong to a valid frame.
                corruption_count += start
                del self._rx[:start]
            frame_type = self._rx[1]
            if frame_type == 0x61:
                if len(self._rx) < self.BLE_PACKET_LEN:
                    break
                packet = bytes(self._rx[: self.BLE_PACKET_LEN])
                sample = self._decode_ble_imu_packet(packet, device_id=device_id, device_name=device_name)
                if sample is None:
                    corruption_count += 1
                    del self._rx[0]
                    continue
                samples.append(sample)
                del self._rx[: self.BLE_PACKET_LEN]
                continue

            if len(self._rx) < self.SERIAL_FRAME_LEN:
                break

            if not self._is_standard_frame_type(frame_type):
                corruption_count += 1
                del self._rx[0]
                continue

            frame = bytes(self._rx[: self.SERIAL_FRAME_LEN])
            expected = sum(frame[:10]) & 0xFF
            if frame[10] != expected:
                corruption_count += 1
                next_header = self._rx.find(0x55, 1, self.SERIAL_FRAME_LEN)
                if next_header > 0:
                    del self._rx[:next_header]
                else:
                    del self._rx[0]
                continue

            decoded_type = self._decode_frame(frame)
            sample = None
            if decoded_type == 0x53:
                sample = self._build_sample_if_ready(device_id=device_id, device_name=device_name)
            if sample is not None:
                samples.append(sample)

            del self._rx[: self.SERIAL_FRAME_LEN]

        return samples, corruption_count

    def _decode_frame(self, frame: bytes) -> int:
        ftype = frame[1]
        values = [
            self._to_i16(frame[2:4]),
            self._to_i16(frame[4:6]),
            self._to_i16(frame[6:8]),
            self._to_i16(frame[8:10]),
        ]

        if ftype == 0x51:
            self._acc = (
                values[0] / 32768.0 * 16.0,
                values[1] / 32768.0 * 16.0,
                values[2] / 32768.0 * 16.0,
            )
        elif ftype == 0x52:
            self._gyro = (
                values[0] / 32768.0 * 2000.0,
                values[1] / 32768.0 * 2000.0,
                values[2] / 32768.0 * 2000.0,
            )
        elif ftype == 0x53:
            self._angle = (
                values[0] / 32768.0 * 180.0,
                values[1] / 32768.0 * 180.0,
                values[2] / 32768.0 * 180.0,
            )
        return ftype

    def _trim_rx_if_needed(self) -> None:
        if len(self._rx) <= self.MAX_RX_BUFFER:
            return
        start = self._rx.rfind(0x55)
        if start < 0:
            self._rx.clear()
            return
        del self._rx[:start]
        if len(self._rx) > self.MAX_RX_BUFFER:
            del self._rx[: len(self._rx) - self.MAX_RX_BUFFER]

    @staticmethod
    def _is_standard_frame_type(frame_type: int) -> bool:
        # Legacy serial protocol packet IDs.
        return 0x50 <= frame_type <= 0x5F

    def _decode_ble_imu_packet(
        self,
        packet: bytes,
        *,
        device_id: str,
        device_name: str,
    ) -> Optional[SensorSample]:
        if len(packet) != self.BLE_PACKET_LEN or packet[0] != 0x55 or packet[1] != 0x61:
            return None

        values = [self._to_i16(packet[offset : offset + 2]) for offset in range(2, 20, 2)]
        return SensorSample(
            timestamp=int(time.time() * 1000),
            deviceId=device_id,
            deviceName=device_name,
            accX=values[0] / 32768.0 * 16.0,
            accY=values[1] / 32768.0 * 16.0,
            accZ=values[2] / 32768.0 * 16.0,
            gyroX=values[3] / 32768.0 * 2000.0,
            gyroY=values[4] / 32768.0 * 2000.0,
            gyroZ=values[5] / 32768.0 * 2000.0,
            roll=values[6] / 32768.0 * 180.0,
            pitch=values[7] / 32768.0 * 180.0,
            yaw=values[8] / 32768.0 * 180.0,
        )

    def _build_sample_if_ready(self, *, device_id: str, device_name: str) -> Optional[SensorSample]:
        if self._acc is None or self._gyro is None or self._angle is None:
            return None

        acc = self._acc
        gyro = self._gyro
        ang = self._angle

        return SensorSample(
            timestamp=int(time.time() * 1000),
            deviceId=device_id,
            deviceName=device_name,
            accX=acc[0],
            accY=acc[1],
            accZ=acc[2],
            gyroX=gyro[0],
            gyroY=gyro[1],
            gyroZ=gyro[2],
            roll=ang[0],
            pitch=ang[1],
            yaw=ang[2],
        )

    @staticmethod
    def _to_i16(raw: bytes) -> int:
        return int.from_bytes(raw, byteorder="little", signed=True)


class SensorWorker:
    """Independent BLE lifecycle worker per sensor."""

    def __init__(self, config: SensorConfig, state: SensorState, service_cfg: ServiceConfig) -> None:
        self.config = config
        self.state = state
        self.service_cfg = service_cfg
        self._parser = WitMotionParser()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event = asyncio.Event()
        self._last_packet_monotonic = 0.0

    def request_stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        while not self._stop_event.is_set():
            try:
                device = await self._resolve_device()
                await self._run_session(device)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.exception("[%s] worker error: %s", self.config.device_address, exc)
                self.state.set_connected(False)

            if not self._stop_event.is_set():
                await asyncio.sleep(self.service_cfg.reconnect_delay_sec)

    async def _resolve_device(self) -> BLEDevice:
        device = await BleakScanner.find_device_by_address(
            self.config.device_address,
            timeout=self.service_cfg.scan_timeout_sec,
        )
        if not device:
            raise RuntimeError(f"BLE device not found: {self.config.device_address}")
        return device

    async def _run_session(self, device: BLEDevice) -> None:
        disconnected = asyncio.Event()
        self._last_packet_monotonic = time.monotonic()

        def on_disconnect(_: BleakClient) -> None:
            if self._loop:
                self._loop.call_soon_threadsafe(disconnected.set)

        def on_notify(_: int, payload: bytearray) -> None:
            if self._loop:
                self._loop.call_soon_threadsafe(self._handle_payload, bytes(payload), device)

        self.state.set_connected(True)

        try:
            async with BleakClient(device, disconnected_callback=on_disconnect) as client:
                await client.start_notify(self.config.notify_char_uuid, on_notify)

                while not self._stop_event.is_set() and not disconnected.is_set():
                    await asyncio.sleep(0.2)
                    self._check_timeout()

                try:
                    await client.stop_notify(self.config.notify_char_uuid)
                except Exception:
                    LOGGER.warning("[%s] stop_notify failed", self.config.device_address, exc_info=True)
        finally:
            self.state.set_connected(False)

    def _handle_payload(self, payload: bytes, device: BLEDevice) -> None:
        self._last_packet_monotonic = time.monotonic()
        LOGGER.debug("RAW BYTES: %s", payload.hex(" "))

        samples, corruption = self._parser.feed(
            payload,
            device_id=self.config.device_address,
            device_name=device.name or "WitMotion",
        )

        if corruption > 0:
            self.state.mark_data_corruption()
            LOGGER.debug(
                "[%s] protocol mismatch bytes=%d payload_len=%d",
                self.config.device_address,
                corruption,
                len(payload),
            )

        if samples:
            self.state.mark_producing()
            self.state.clear_error_if_any()
            for sample in samples:
                if self._sample_is_finite(sample):
                    self.state.buffer.push(sample)
                else:
                    self.state.mark_data_corruption()

    def _check_timeout(self) -> None:
        connected, _, _ = self.state.snapshot()
        if not connected:
            return

        if self._last_packet_monotonic <= 0.0:
            return

        delta = time.monotonic() - self._last_packet_monotonic
        if delta >= self.service_cfg.no_data_timeout_sec:
            self.state.mark_timeout()

    @staticmethod
    def _sample_is_finite(sample: SensorSample) -> bool:
        return all(
            math.isfinite(v)
            for v in [
                sample.accX,
                sample.accY,
                sample.accZ,
                sample.gyroX,
                sample.gyroY,
                sample.gyroZ,
                sample.roll,
                sample.pitch,
                sample.yaw,
            ]
        )


class S1SensorService:
    """IF-S1-S2 provider: s1.sensor.read() and s1.sensor.status()."""

    def __init__(self, sensors: list[SensorConfig], service_cfg: ServiceConfig) -> None:
        if not sensors:
            raise ValueError("At least one sensor config is required.")

        self._states: dict[str, SensorState] = {sensor.device_address: SensorState() for sensor in sensors}
        self._workers = [
            SensorWorker(config=sensor, state=self._states[sensor.device_address], service_cfg=service_cfg)
            for sensor in sensors
        ]
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        if self._tasks:
            return
        self._tasks = [
            asyncio.create_task(self._guard_worker(worker), name=f"sensor-{i}")
            for i, worker in enumerate(self._workers, start=1)
        ]

    async def wait_forever(self) -> None:
        if not self._tasks:
            await self.start()
        await asyncio.gather(*self._tasks)

    async def stop(self) -> None:
        for worker in self._workers:
            worker.request_stop()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()

    def read(self) -> list[SensorSample]:
        """s1.sensor.read(): drain and return all samples since last call."""
        all_samples: list[SensorSample] = []
        for state in self._states.values():
            all_samples.extend(state.buffer.drain())

        all_samples.sort(key=lambda s: (s.timestamp, s.deviceId))
        return all_samples

    def status(self) -> SensorStatus:
        """s1.sensor.status(): aggregated status across configured sensors."""
        snapshots = [state.snapshot() for state in self._states.values()]
        connected = all(is_conn and is_prod for is_conn, is_prod, _ in snapshots)

        errors = [err for _, _, err in snapshots if err is not None]
        error = min(errors, key=lambda err: ERROR_PRIORITY.get(err, 99)) if errors else None

        return SensorStatus(connected=connected, errorMessage=error)

    async def _guard_worker(self, worker: SensorWorker) -> None:
        try:
            await worker.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.exception("Worker terminated: %s", exc)


class S1Module:
    """Convenience wrapper so S2 can call s1.sensor.read()/status()."""

    def __init__(self, sensor_service: S1SensorService) -> None:
        self.sensor = sensor_service


def create_s1_module(sensors: list[SensorConfig], service_cfg: Optional[ServiceConfig] = None) -> S1Module:
    """Build the IF-S1-S2 provider with the exact shape consumed by S2."""

    return S1Module(S1SensorService(sensors=sensors, service_cfg=service_cfg or ServiceConfig()))


def _parse_sensor_item(item: str) -> SensorConfig:
    parts = [p.strip() for p in item.split(",")]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"Invalid --sensor: {item!r}; expected '<device_address>,<notify_char_uuid>'.")
    return SensorConfig(device_address=parts[0], notify_char_uuid=parts[1])


def _load_sensors_from_json(path: str) -> list[SensorConfig]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("--sensors-json must be a JSON array.")

    sensors: list[SensorConfig] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError("Each sensor entry must be an object.")
        device_address = str(entry.get("device_address", "")).strip()
        notify_uuid = str(entry.get("notify_char_uuid", "")).strip()
        if not device_address or not notify_uuid:
            raise ValueError("Each sensor entry requires device_address and notify_char_uuid.")
        sensors.append(SensorConfig(device_address=device_address, notify_char_uuid=notify_uuid))
    return sensors


def _merge_sensor_configs(args: argparse.Namespace) -> list[SensorConfig]:
    sensors = [_parse_sensor_item(item) for item in args.sensor]
    if args.sensors_json:
        sensors.extend(_load_sensors_from_json(args.sensors_json))

    if not sensors:
        raise ValueError("Provide sensors via --sensor and/or --sensors-json.")

    dedup: dict[tuple[str, str], SensorConfig] = {}
    for sensor in sensors:
        dedup[(sensor.device_address.lower(), sensor.notify_char_uuid.lower())] = sensor
    return list(dedup.values())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DSD-S1 IF-S1-S2 sensor provider")
    parser.add_argument(
        "--sensor",
        action="append",
        default=[],
        help="Repeatable '<device_address>,<notify_char_uuid>' sensor definition",
    )
    parser.add_argument(
        "--sensors-json",
        type=str,
        help="JSON file containing list of {device_address, notify_char_uuid}",
    )
    parser.add_argument("--scan-timeout-sec", type=float, default=8.0)
    parser.add_argument("--reconnect-delay-sec", type=float, default=2.0)
    parser.add_argument("--no-data-timeout-sec", type=float, default=2.0)
    parser.add_argument("--status-log-interval-sec", type=float, default=2.0)
    parser.add_argument(
        "--csv-output",
        type=str,
        default="output/sensor_samples.csv",
        help="CSV file used to persist drained sensor samples",
    )
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    sensors = _merge_sensor_configs(args)
    if len(sensors) != 6:
        LOGGER.warning("Configured %d sensor(s). DSD-S1 target is 6.", len(sensors))

    service = S1SensorService(
        sensors=sensors,
        service_cfg=ServiceConfig(
            scan_timeout_sec=args.scan_timeout_sec,
            reconnect_delay_sec=args.reconnect_delay_sec,
            no_data_timeout_sec=args.no_data_timeout_sec,
        ),
    )
    s1 = S1Module(service)
    csv_writer = CsvSampleWriter(args.csv_output)

    await s1.sensor.start()

    try:
        while True:
            await asyncio.sleep(args.status_log_interval_sec)
            st = s1.sensor.status()
            LOGGER.info("status connected=%s error=%s", st.connected, st.errorMessage)
            samples = s1.sensor.read()
            csv_writer.write_samples(samples)
            if samples:
                LOGGER.info("wrote %d sample(s) to %s", len(samples), csv_writer.path)
    finally:
        await s1.sensor.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
