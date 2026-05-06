"""
S2-02: Data acquisition core.
- Periodically reads from data source (S1 real or S2-01 simulator)
- Validates sensor samples
- Computes joint angles from paired sensors with temporal alignment
- Generates error events
"""

from __future__ import annotations

import csv
import logging
import math
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from src.s1.ble_tunnel import SensorSample, SensorStatus
from src.s2.data_classes import TargetAngle, ErrorEvent

LOGGER = logging.getLogger("s2.core")

# Temporal alignment threshold: max time difference (ms) between two sensors
# to consider them as a matching pair for angle computation
PAIR_TIME_THRESHOLD_MS = 100

# Polling interval for reading sensor data
POLL_INTERVAL_SEC = 0.02  # 50 Hz


def _parse_joint_pairs(sensor_joint_mapping: dict) -> dict:
    """Parse sensorJointMapping into joint pairs.

    The mapping format per IS §2.3.1:
        {sensorId: joint_name}
        e.g. {"sensorA": "left_knee", "sensorB": "left_knee",
              "sensorC": "right_knee", "sensorD": "right_knee"}

    Two sensors mapped to the SAME joint name form a pair.
    The first sensor encountered becomes sensor_a, the second becomes sensor_b.
    A joint with only one sensor cannot compute an angle.

    Returns: {joint_name: (sensor_id_a, sensor_id_b)}
    """
    joint_sensors: dict[str, list[str]] = defaultdict(list)

    for sensor_id, joint_name in sensor_joint_mapping.items():
        joint_sensors[str(joint_name)].append(sensor_id)

    pairs = {}
    for joint_name, sensor_ids in joint_sensors.items():
        if len(sensor_ids) >= 2:
            pairs[joint_name] = (sensor_ids[0], sensor_ids[1])
            if len(sensor_ids) > 2:
                LOGGER.warning(
                    "Joint '%s' has %d sensors, using first two: %s, %s",
                    joint_name, len(sensor_ids), sensor_ids[0], sensor_ids[1],
                )
        else:
            LOGGER.warning(
                "Joint '%s' has only one sensor (%s), cannot compute angle",
                joint_name, sensor_ids[0],
            )

    return pairs


# Binding mode constants
BINDMODE_PORT = "port"      # charging port normal (long edge) parallel to leg, uses roll+pitch+yaw
BINDMODE_SCREEN = "screen"  # screen normal parallel to leg, uses roll+pitch
BINDMODE_BACK = "back"      # back against leg, angle between screen normals, uses roll+pitch


def _screen_normal_world(sample: SensorSample) -> tuple:
    """Compute the world-space direction of the screen normal (Z axis).

    Z direction = Ry(pitch) · Rx(roll) · [0, 0, 1]  (yaw is irrelevant).
    Robust to sensor rotating around the screen normal (which only changes yaw).

    Uses only roll + pitch.
    """
    r = math.radians(sample.roll)
    p = math.radians(sample.pitch)

    sr, cr = math.sin(r), math.cos(r)
    sp, cp = math.sin(p), math.cos(p)

    wx = sp * cr
    wy = -sr
    wz = cp * cr

    return (wx, wy, wz)


def _long_edge_world(sample: SensorSample) -> tuple:
    """Compute the world-space direction of the long edge (Y axis).

    Y direction = Rz(yaw) · Ry(pitch) · Rx(roll) · [0, 1, 0]  (second column of R).
    Robust to sensor rotating around the long edge, but requires all three angles
    including yaw (which may drift).

    Uses roll + pitch + yaw.
    """
    r = math.radians(sample.roll)
    p = math.radians(sample.pitch)
    y = math.radians(sample.yaw)

    sr, cr = math.sin(r), math.cos(r)
    sp, cp = math.sin(p), math.cos(p)
    sy, cy = math.sin(y), math.cos(y)

    wx = cy * sp * sr - sy * cr
    wy = sy * sp * sr + cy * cr
    wz = cp * sr

    return (wx, wy, wz)


def _compute_angle_between_segments(
    sample_a: SensorSample, sample_b: SensorSample, bind_mode: str
) -> float:
    """Compute joint angle from two IMU sensors mounted on adjacent limb segments.

    bind_mode selects which axis is parallel to the leg:
      - "screen": screen normal (Z axis), uses roll + pitch only
      - "port":   long edge / charging port normal (Y axis), uses roll + pitch + yaw

    Returns the angle between the two direction vectors (0° = straight,
    increases as joint bends).
    """
    if bind_mode == BINDMODE_PORT:
        va = _long_edge_world(sample_a)
        vb = _long_edge_world(sample_b)
    else:
        # BINDMODE_SCREEN and BINDMODE_BACK both use screen normal (Z axis)
        va = _screen_normal_world(sample_a)
        vb = _screen_normal_world(sample_b)

    dot = va[0] * vb[0] + va[1] * vb[1] + va[2] * vb[2]
    dot = max(-1.0, min(1.0, dot))

    angle_deg = math.degrees(math.acos(dot))
    return round(angle_deg, 2)


class JointAngleComputer:
    """Computes joint angles from asynchronous sensor data streams.

    For each joint, maintains recent samples from both sensors and
    finds temporally close pairs to compute angles.
    """

    def __init__(
        self,
        joint_pairs: dict,
        bind_mode: str = BINDMODE_PORT,
        time_threshold_ms: int = PAIR_TIME_THRESHOLD_MS,
    ) -> None:
        self._joint_pairs = joint_pairs  # {joint_name: (upper_id, lower_id)}
        self._bind_mode = bind_mode
        self._threshold = time_threshold_ms
        # Per-sensor sample cache: {sensor_id: [(timestamp, SensorSample), ...]}
        self._cache: dict[str, list[tuple[int, SensorSample]]] = defaultdict(list)
        # Track which samples have been consumed to avoid duplicate angles
        self._consumed: dict[str, set[int]] = defaultdict(set)

    def feed_samples(self, samples: List[SensorSample]) -> List[TargetAngle]:
        """Feed new samples and compute any possible joint angles."""
        for s in samples:
            self._cache[s.deviceId].append((s.timestamp, s))

        angles = []
        for joint_name, (upper_id, lower_id) in self._joint_pairs.items():
            new_angles = self._match_and_compute(joint_name, upper_id, lower_id)
            angles.extend(new_angles)

        self._prune_old_samples()
        return angles

    def _match_and_compute(
        self, joint_name: str, upper_id: str, lower_id: str
    ) -> List[TargetAngle]:
        upper_samples = self._cache.get(upper_id, [])
        lower_samples = self._cache.get(lower_id, [])

        if not upper_samples or not lower_samples:
            return []

        results = []
        used_lower: set[int] = set()

        for u_idx, (u_ts, u_sample) in enumerate(upper_samples):
            # Skip already consumed
            u_key = (upper_id, u_ts)
            if u_ts in self._consumed.get(upper_id, set()):
                continue

            best_match: Optional[Tuple[int, int, SensorSample]] = None
            best_diff = self._threshold + 1

            for l_idx, (l_ts, l_sample) in enumerate(lower_samples):
                if l_idx in used_lower:
                    continue
                if l_ts in self._consumed.get(lower_id, set()):
                    continue

                diff = abs(u_ts - l_ts)
                if diff <= self._threshold and diff < best_diff:
                    best_diff = diff
                    best_match = (l_idx, l_ts, l_sample)

            if best_match is not None:
                l_idx, l_ts, l_sample = best_match
                used_lower.add(l_idx)

                angle = _compute_angle_between_segments(u_sample, l_sample, self._bind_mode)
                avg_ts = (u_ts + l_ts) // 2

                results.append(
                    TargetAngle(
                        timestamp=avg_ts,
                        angleID=joint_name,
                        angle=angle,
                    )
                )

                self._consumed[upper_id].add(u_ts)
                self._consumed[lower_id].add(l_ts)

        return results

    def _prune_old_samples(self, max_age_ms: int = 2000) -> None:
        """Remove samples older than max_age_ms to prevent memory growth."""
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - max_age_ms

        for sensor_id in list(self._cache.keys()):
            self._cache[sensor_id] = [
                (ts, s) for ts, s in self._cache[sensor_id] if ts > cutoff
            ]
            self._consumed[sensor_id] = {
                ts for ts in self._consumed[sensor_id] if ts > cutoff
            }


def validate_sample(sample: SensorSample) -> Optional[str]:
    """Validate a single sensor sample. Returns error message if invalid, None if OK."""
    for attr in ("accX", "accY", "accZ", "gyroX", "gyroY", "gyroZ", "roll", "pitch", "yaw"):
        val = getattr(sample, attr)
        if not math.isfinite(val):
            return f"Non-finite value in {attr}: {val}"

    if sample.timestamp <= 0:
        return f"Invalid timestamp: {sample.timestamp}"

    return None


class DataAcqCore:
    """S2-02: Data acquisition core.

    Runs a background thread that:
    1. Polls the data source (S1 or simulator) for new samples
    2. Validates samples
    3. Computes joint angles
    4. Pushes results to the async buffer (S2-03)
    """

    def __init__(
        self,
        data_source,  # object with read() -> List[SensorSample] and status() -> SensorStatus
        sensor_joint_mapping: dict,
        bind_mode: str = BINDMODE_PORT,
        on_samples=None,       # callback(List[SensorSample])
        on_angles=None,        # callback(List[TargetAngle])
        on_error=None,         # callback(ErrorEvent)
        poll_interval: float = POLL_INTERVAL_SEC,
    ) -> None:
        self._source = data_source
        self._mapping = sensor_joint_mapping
        self._on_samples = on_samples
        self._on_angles = on_angles
        self._on_error = on_error
        self._poll_interval = poll_interval

        joint_pairs = _parse_joint_pairs(sensor_joint_mapping)
        self._angle_computer = JointAngleComputer(joint_pairs, bind_mode=bind_mode)

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._sample_count = 0
        self._error_count = 0

        # Angle log file
        self._angle_log = self._init_angle_log()

    @property
    def sample_count(self) -> int:
        return self._sample_count

    @property
    def error_count(self) -> int:
        return self._error_count

    def _init_angle_log(self):
        """Create a CSV log file for angle data in log/ directory."""
        log_dir = Path("log")
        log_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = log_dir / f"angles_{ts}.csv"
        fh = open(log_path, "w", encoding="utf-8", newline="")
        writer = csv.writer(fh)
        writer.writerow(["timestamp_ms", "timestamp_iso", "angleID", "angle_deg"])
        LOGGER.info("Angle log file: %s", log_path)
        return {"fh": fh, "writer": writer, "path": log_path}

    def _log_angles(self, angles: List[TargetAngle]) -> None:
        """Write angle data points to the CSV log."""
        if not self._angle_log:
            return
        writer = self._angle_log["writer"]
        for a in angles:
            iso = datetime.fromtimestamp(
                a.timestamp / 1000, tz=timezone.utc
            ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            writer.writerow([a.timestamp, iso, a.angleID, a.angle])
        self._angle_log["fh"].flush()

    def _close_angle_log(self) -> None:
        if self._angle_log and self._angle_log["fh"]:
            self._angle_log["fh"].close()
            LOGGER.info("Angle log closed: %s", self._angle_log["path"])
            self._angle_log = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="s2-core-poll"
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None
        self._close_angle_log()

    def _poll_loop(self) -> None:
        while self._running:
            try:
                self._poll_once()
            except Exception:
                LOGGER.exception("Error in S2 core poll loop")
            time.sleep(self._poll_interval)

    def _poll_once(self) -> None:
        # Check sensor status
        status: SensorStatus = self._source.status()
        if not status.connected and status.errorMessage:
            self._emit_error(
                ErrorEvent(
                    timestamp=int(time.time() * 1000),
                    sensorId=None,
                    errorType=status.errorMessage,
                    message=f"Sensor status: {status.errorMessage}",
                )
            )

        # Read samples
        raw_samples: List[SensorSample] = self._source.read()
        if not raw_samples:
            return

        valid_samples = []
        for sample in raw_samples:
            err = validate_sample(sample)
            if err is None:
                valid_samples.append(sample)
                self._sample_count += 1
            else:
                self._error_count += 1
                self._emit_error(
                    ErrorEvent(
                        timestamp=sample.timestamp,
                        sensorId=sample.deviceId,
                        errorType="validation_failure",
                        message=err,
                    )
                )

        # Push validated samples
        if valid_samples and self._on_samples:
            self._on_samples(valid_samples)

        # Compute angles
        angles = self._angle_computer.feed_samples(valid_samples)
        if angles:
            self._log_angles(angles)
            if self._on_angles:
                self._on_angles(angles)

    def _emit_error(self, event: ErrorEvent) -> None:
        if self._on_error:
            self._on_error(event)
