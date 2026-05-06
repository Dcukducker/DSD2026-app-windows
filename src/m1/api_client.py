"""
M1 HTTP client for V2 backend.
Implements IF-M1-V2 and IF-V2-M1 per IS §3.
Base URL: http://113.44.220.94:3000
Request: camelCase, Response: snake_case, Timestamps: ISO 8601.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

LOGGER = logging.getLogger("m1.api_client")

BASE_URL = "http://113.44.220.94:3000"


class V2ApiClient:
    """HTTP client for all M1 <-> V2 communication."""

    def __init__(self, base_url: str = BASE_URL) -> None:
        self._base = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})

    def _url(self, path: str) -> str:
        return f"{self._base}{path}"

    def _auth_header(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    # ── Authentication (IS §3.1) ──────────────────────────────────

    def register(
        self, name: str, email: str, password: str, role: str = "patient"
    ) -> dict:
        """POST /auth/register"""
        resp = self._session.post(
            self._url("/auth/register"),
            json={"name": name, "email": email, "password": password, "role": role},
        )
        resp.raise_for_status()
        return resp.json()

    def login(self, email: str, password: str) -> dict:
        """POST /auth/login"""
        resp = self._session.post(
            self._url("/auth/login"),
            json={"email": email, "password": password},
        )
        resp.raise_for_status()
        return resp.json()

    def get_me(self, token: str) -> dict:
        """GET /auth/me"""
        resp = self._session.get(
            self._url("/auth/me"),
            headers=self._auth_header(token),
        )
        resp.raise_for_status()
        return resp.json()

    # ── Users (IS §3.2) ──────────────────────────────────────────

    def get_user(self, user_id: int, token: str) -> dict:
        """GET /users/:id"""
        resp = self._session.get(
            self._url(f"/users/{user_id}"),
            headers=self._auth_header(token),
        )
        resp.raise_for_status()
        return resp.json()

    # ── Sessions (IS §3.3) ───────────────────────────────────────

    def create_session(self, user_id: int, token: str) -> dict:
        """POST /sessions"""
        resp = self._session.post(
            self._url("/sessions"),
            json={"userId": user_id},
            headers=self._auth_header(token),
        )
        resp.raise_for_status()
        return resp.json()

    def get_session(self, session_id: int, token: str) -> dict:
        """GET /sessions/:id"""
        resp = self._session.get(
            self._url(f"/sessions/{session_id}"),
            headers=self._auth_header(token),
        )
        resp.raise_for_status()
        return resp.json()

    def end_session(self, session_id: int, token: str) -> dict:
        """PATCH /sessions/:id/end"""
        resp = self._session.patch(
            self._url(f"/sessions/{session_id}/end"),
            headers=self._auth_header(token),
        )
        resp.raise_for_status()
        return resp.json()

    # ── Measurements (IS §3.4) ───────────────────────────────────

    def upload_measurement(
        self,
        session_id: int,
        target_angles: List[dict],
        errors: List[dict],
        sensor_data: List[dict],
        token: str,
    ) -> dict:
        """POST /measurements

        Payload format per IS §3.4.1 (negotiated S2 format):
        {
            "sessionId": int,
            "targetAngles": [{timestamp, angleID, angle}],
            "errors": [{timestamp, sensorId, errorType, message}],
            "sensorData": [{timestamp, sensorId, accX, ...}]
        }
        """
        payload = {
            "sessionId": session_id,
            "targetAngles": target_angles,
            "errors": errors,
            "sensorData": sensor_data,
        }
        resp = self._session.post(
            self._url("/measurements"),
            json=payload,
            headers=self._auth_header(token),
        )
        resp.raise_for_status()
        return resp.json()

    def upload_measurement_batch(
        self,
        session_id: int,
        measurements: List[dict],
        token: str,
    ) -> dict:
        """POST /measurements/batch"""
        payload = {
            "sessionId": session_id,
            "measurements": measurements,
        }
        resp = self._session.post(
            self._url("/measurements/batch"),
            json=payload,
            headers=self._auth_header(token),
        )
        resp.raise_for_status()
        return resp.json()

    def get_measurements(self, session_id: int, token: str) -> list:
        """GET /measurements/:sessionId"""
        resp = self._session.get(
            self._url(f"/measurements/{session_id}"),
            headers=self._auth_header(token),
        )
        resp.raise_for_status()
        return resp.json()

    # ── Recommendations (IS §3.5) ────────────────────────────────

    def get_session_recommendations(self, session_id: int, token: str) -> list:
        """GET /recommendations/session/:sessionId"""
        resp = self._session.get(
            self._url(f"/recommendations/session/{session_id}"),
            headers=self._auth_header(token),
        )
        resp.raise_for_status()
        return resp.json()

    def get_engine_recommendations(self, user_id: int, token: str) -> dict:
        """GET /recommendations/engine/:userId"""
        resp = self._session.get(
            self._url(f"/recommendations/engine/{user_id}"),
            headers=self._auth_header(token),
        )
        resp.raise_for_status()
        return resp.json()

    # ── Schedule (IS §3.6) ───────────────────────────────────────

    def get_schedule(self, user_id: int, token: str) -> list:
        """GET /schedule/:userId"""
        resp = self._session.get(
            self._url(f"/schedule/{user_id}"),
            headers=self._auth_header(token),
        )
        resp.raise_for_status()
        return resp.json()

    def update_schedule(self, schedule_id: int, status: str, token: str) -> dict:
        """PATCH /schedule/:id"""
        resp = self._session.patch(
            self._url(f"/schedule/{schedule_id}"),
            json={"status": status},
            headers=self._auth_header(token),
        )
        resp.raise_for_status()
        return resp.json()

    # ── Push Notifications (IS §3.7) ─────────────────────────────

    def register_push_token(
        self, user_id: int, device_token: str, platform: str, token: str
    ) -> dict:
        """POST /push/register"""
        resp = self._session.post(
            self._url("/push/register"),
            json={"userId": user_id, "token": device_token, "platform": platform},
            headers=self._auth_header(token),
        )
        resp.raise_for_status()
        return resp.json()


def format_data_to_measurement_payload(format_data) -> dict:
    """Convert FormatData from S2 into V2 measurement upload payload.

    Handles the mapping:
    - SensorSample.timestamp (int ms) -> ISO 8601 string
    - SensorSample.deviceId -> sensorId
    - TargetAngle.timestamp (int ms) -> ISO 8601 string
    - ErrorEvent.timestamp (int ms) -> ISO 8601 string
    """
    def ms_to_iso(ts_ms: int) -> str:
        return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")

    target_angles = []
    for ta in format_data.targetAngles:
        target_angles.append({
            "timestamp": ms_to_iso(ta.timestamp),
            "angleID": ta.angleID,
            "angle": ta.angle,
        })

    errors = []
    for err in format_data.errors:
        errors.append({
            "timestamp": ms_to_iso(err.timestamp),
            "sensorId": err.sensorId,
            "errorType": err.errorType,
            "message": err.message,
        })

    sensor_data = []
    for sd in format_data.sensorData:
        sensor_data.append({
            "timestamp": ms_to_iso(sd.timestamp),
            "sensorId": sd.deviceId,
            "accX": sd.accX,
            "accY": sd.accY,
            "accZ": sd.accZ,
            "gyroX": sd.gyroX,
            "gyroY": sd.gyroY,
            "gyroZ": sd.gyroZ,
            "roll": sd.roll,
            "pitch": sd.pitch,
            "yaw": sd.yaw,
        })

    return {
        "sessionId": format_data.sessionContext.sessionId,
        "targetAngles": target_angles,
        "errors": errors,
        "sensorData": sensor_data,
    }
