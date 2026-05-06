"""
S2-03: Asynchronous sending buffer.
Thread-safe triple buffer for sensorData, targetAngles, errors.
S2-02 writes, M1 reads via s2.data.read().

Supports multiple independent consumers: each consumer gets its own
drain cursor so that data is not lost when both the auto-upload thread
and the UI poll concurrently.
"""

from __future__ import annotations

import threading
from typing import Dict, List

from src.s1.ble_tunnel import SensorSample
from src.s2.data_classes import (
    ErrorEvent,
    FormatData,
    SessionContext,
    TargetAngle,
)


class AsyncBuffer:
    """Thread-safe triple buffer that decouples S2-02 (producer) from M1 (consumer).

    Accumulates sensorData, targetAngles, and errors independently.
    drain() returns all accumulated data since the last drain() call
    for the given consumer_id (defaults to "default" for s2.data.read()).
    """

    def __init__(self, session_context: SessionContext) -> None:
        self._session_context = session_context
        self._lock = threading.Lock()
        self._sensor_data: List[SensorSample] = []
        self._target_angles: List[TargetAngle] = []
        self._errors: List[ErrorEvent] = []
        # Per-consumer cursors: track how far each consumer has read
        self._cursors: Dict[str, tuple] = {}  # {consumer_id: (sd_idx, ta_idx, err_idx)}

    def push_samples(self, samples: List[SensorSample]) -> None:
        with self._lock:
            self._sensor_data.extend(samples)

    def push_angles(self, angles: List[TargetAngle]) -> None:
        with self._lock:
            self._target_angles.extend(angles)

    def push_error(self, error: ErrorEvent) -> None:
        with self._lock:
            self._errors.append(error)

    def drain(self, consumer_id: str = "default") -> FormatData:
        """Return all data accumulated since this consumer's last drain().

        Each consumer_id tracks its own read position independently,
        so multiple callers (auto-upload, UI polling) each get all data
        without stealing from each other.
        """
        with self._lock:
            sd_start, ta_start, err_start = self._cursors.get(
                consumer_id, (0, 0, 0)
            )

            data = FormatData(
                sessionContext=self._session_context,
                sensorData=list(self._sensor_data[sd_start:]),
                targetAngles=list(self._target_angles[ta_start:]),
                errors=list(self._errors[err_start:]),
            )

            self._cursors[consumer_id] = (
                len(self._sensor_data),
                len(self._target_angles),
                len(self._errors),
            )

            self._maybe_compact()
            return data

    def _maybe_compact(self) -> None:
        """Reclaim memory once all consumers have read past old data."""
        if not self._cursors:
            return

        min_sd = min(c[0] for c in self._cursors.values())
        min_ta = min(c[1] for c in self._cursors.values())
        min_err = min(c[2] for c in self._cursors.values())

        if min_sd > 0:
            del self._sensor_data[:min_sd]
            for cid in self._cursors:
                s, t, e = self._cursors[cid]
                self._cursors[cid] = (s - min_sd, t, e)

        if min_ta > 0:
            del self._target_angles[:min_ta]
            for cid in self._cursors:
                s, t, e = self._cursors[cid]
                self._cursors[cid] = (s, t - min_ta, e)

        if min_err > 0:
            del self._errors[:min_err]
            for cid in self._cursors:
                s, t, e = self._cursors[cid]
                self._cursors[cid] = (s, t, e - min_err)

    @property
    def session_context(self) -> SessionContext:
        return self._session_context
