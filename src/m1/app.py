"""
M1 Flask Web Application — Patient Rehabilitation Frontend.
Provides login/register, session control, real-time data display,
and measurement upload to V2.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from flask import Flask, render_template, request, jsonify

from src.m1.api_client import V2ApiClient, format_data_to_measurement_payload

LOGGER = logging.getLogger("m1.app")


def create_app(s2_module, api_client: Optional[V2ApiClient] = None) -> Flask:
    """Create and configure the Flask application.

    Args:
        s2_module: S2Module instance for session control and data reading.
        api_client: V2ApiClient instance. Created with default base URL if None.
    """
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )
    app.secret_key = "dsd-s2-m1-dev-key"

    if api_client is None:
        api_client = V2ApiClient()

    app.config["S2_MODULE"] = s2_module
    app.config["API_CLIENT"] = api_client

    _upload_thread: Optional[threading.Thread] = None
    _upload_running = False

    def get_s2():
        return app.config["S2_MODULE"]

    def get_api():
        return app.config["API_CLIENT"]

    # ── Pages ─────────────────────────────────────────────────────

    @app.route("/")
    def index():
        return render_template("index.html")

    # ── Auth API ──────────────────────────────────────────────────

    @app.route("/api/register", methods=["POST"])
    def api_register():
        data = request.get_json()
        try:
            result = get_api().register(
                name=data["name"],
                email=data["email"],
                password=data["password"],
                role=data.get("role", "patient"),
            )
            return jsonify(result), 201
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/login", methods=["POST"])
    def api_login():
        data = request.get_json()
        try:
            result = get_api().login(
                email=data["email"], password=data["password"]
            )
            return jsonify(result), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    # ── Session Control API ───────────────────────────────────────

    # ── Data Source Mode API ─────────────────────────────────────

    @app.route("/api/mode", methods=["GET"])
    def api_get_mode():
        """Get current data source mode and availability."""
        s2 = get_s2()
        real_available = s2.session._s1_real is not None
        return jsonify({
            "mode": "simulator" if s2.session.use_simulator else "real",
            "realAvailable": real_available,
        }), 200

    @app.route("/api/mode", methods=["POST"])
    def api_set_mode():
        """Switch data source mode: {"mode": "simulator"} or {"mode": "real"}."""
        data = request.get_json()
        mode = data.get("mode", "")
        try:
            if mode == "simulator":
                get_s2().session.set_mode(True)
            elif mode == "real":
                get_s2().session.set_mode(False)
            else:
                return jsonify({"error": "mode must be 'simulator' or 'real'"}), 400
            return jsonify({"mode": mode}), 200
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 400

    # WitMotion BLE IMU default notify characteristic UUID
    WITMOTION_NOTIFY_UUID = "0000ffe4-0000-1000-8000-00805f9a34fb"

    @app.route("/api/sensors/configure", methods=["POST"])
    def api_configure_sensors():
        """Configure real BLE sensors at runtime.

        Expects: {"addresses": ["AA:BB:CC:DD:EE:F1", "AA:BB:CC:DD:EE:F2"]}
        The notify_char_uuid uses the WitMotion default automatically.
        Creates a real S1 module and makes the 'real' mode available.
        """
        data = request.get_json()
        addresses = data.get("addresses", [])
        if not addresses:
            return jsonify({"error": "addresses list is empty"}), 400

        try:
            from src.s1.ble_tunnel import SensorConfig, ServiceConfig
            from src.s1.service import create_s1_module

            configs = [
                SensorConfig(
                    device_address=addr.strip(),
                    notify_char_uuid=WITMOTION_NOTIFY_UUID,
                )
                for addr in addresses
                if addr.strip()
            ]
            if not configs:
                return jsonify({"error": "no valid addresses provided"}), 400

            s1_real = create_s1_module(configs, ServiceConfig())
            get_s2().session.set_real_s1(s1_real)
            return jsonify({
                "message": f"Configured {len(configs)} BLE sensor(s)",
                "realAvailable": True,
            }), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    # ── Session Control API ───────────────────────────────────────

    @app.route("/api/session/create", methods=["POST"])
    def api_session_create():
        """Create a session on V2, returns server-generated session id."""
        data = request.get_json()
        try:
            result = get_api().create_session(
                user_id=data["userId"], token=data["token"]
            )
            return jsonify(result), 201
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/session/start", methods=["POST"])
    def api_session_start():
        """Start data acquisition on S2.

        Expects: {sessionId, userId, sensorJointMapping, payloadStatus}
        """
        data = request.get_json()
        try:
            result = get_s2().session.start(
                sessionId=data["sessionId"],
                userId=data["userId"],
                sensorJointMapping=data.get("sensorJointMapping", {}),
                payloadStatus=data.get("payloadStatus", ""),
            )
            return jsonify({
                "success": result.success,
                "errorMessage": result.errorMessage,
            }), 200
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/session/stop", methods=["POST"])
    def api_session_stop():
        """Stop data acquisition on S2 and end session on V2."""
        data = request.get_json()
        try:
            s2_session = get_s2().session
            core_log_data = s2_session._core._json_log_data if s2_session._core else None
            
            summary = s2_session.stop()

            if data.get("token") and data.get("sessionId"):
                try:
                    if core_log_data:
                        get_api().upload_measurement(
                            session_id=data["sessionId"],
                            target_angles=core_log_data.get("targetAngles", []),
                            errors=core_log_data.get("errors", []),
                            sensor_data=core_log_data.get("sensorData", []),
                            token=data["token"]
                        )
                        LOGGER.info("Successfully uploaded full session data to V2 measurements raw endpoint.")
                        
                    get_api().end_session(
                        session_id=data["sessionId"], token=data["token"]
                    )
                except Exception as v2_err:
                    LOGGER.warning("Failed to end V2 session or upload data wrapper errored: %s", v2_err)

            return jsonify({
                "sessionId": summary.sessionId,
                "sampleCount": summary.sampleCount,
                "errorCount": summary.errorCount,
                "startTime": summary.startTime,
                "endTime": summary.endTime,
            }), 200
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── Data Reading API ──────────────────────────────────────────

    @app.route("/api/data/read", methods=["GET"])
    def api_data_read():
        """Read current FormatData from S2."""
        try:
            format_data = get_s2().data.read()
            return jsonify({
                "sessionContext": {
                    "sessionId": format_data.sessionContext.sessionId,
                    "userId": format_data.sessionContext.userId,
                    "sensorJointMapping": format_data.sessionContext.sensorJointMapping,
                    "payloadStatus": format_data.sessionContext.payloadStatus,
                },
                "sensorData": [
                    {
                        "timestamp": s.timestamp,
                        "deviceId": s.deviceId,
                        "deviceName": s.deviceName,
                        "accX": s.accX, "accY": s.accY, "accZ": s.accZ,
                        "gyroX": s.gyroX, "gyroY": s.gyroY, "gyroZ": s.gyroZ,
                        "roll": s.roll, "pitch": s.pitch, "yaw": s.yaw,
                    }
                    for s in format_data.sensorData
                ],
                "targetAngles": [
                    {"timestamp": ta.timestamp, "angleID": ta.angleID, "angle": ta.angle}
                    for ta in format_data.targetAngles
                ],
                "errors": [
                    {
                        "timestamp": e.timestamp,
                        "sensorId": e.sensorId,
                        "errorType": e.errorType,
                        "message": e.message,
                    }
                    for e in format_data.errors
                ],
            }), 200
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 400

    # ── Measurement Upload API ────────────────────────────────────

    @app.route("/api/measurement/upload", methods=["POST"])
    def api_measurement_upload():
        """Read data from S2 and upload to V2 as measurement."""
        data = request.get_json()
        token = data.get("token")
        session_id = data.get("sessionId")

        if not token or not session_id:
            return jsonify({"error": "token and sessionId required"}), 400

        try:
            format_data = get_s2().data.read()
            if not format_data.sensorData and not format_data.targetAngles:
                return jsonify({"message": "No new data to upload"}), 200

            payload = format_data_to_measurement_payload(format_data)
            result = get_api().upload_measurement(
                session_id=session_id,
                target_angles=payload["targetAngles"],
                errors=payload["errors"],
                sensor_data=payload["sensorData"],
                token=token,
            )
            return jsonify(result), 201
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/measurement/auto-upload/start", methods=["POST"])
    def api_auto_upload_start():
        """Start background auto-upload: periodically read S2 data and upload to V2."""
        nonlocal _upload_thread, _upload_running

        data = request.get_json()
        token = data.get("token")
        session_id = data.get("sessionId")
        interval = data.get("interval", 2.0)

        if not token or not session_id:
            return jsonify({"error": "token and sessionId required"}), 400

        if _upload_running:
            return jsonify({"message": "Auto-upload already running"}), 200

        _upload_running = True

        def upload_loop():
            nonlocal _upload_running
            while _upload_running:
                try:
                    if not get_s2().session.is_active:
                        break
                    buf = get_s2().session.buffer
                    if buf is None:
                        break
                    format_data = buf.drain("auto_upload")
                    if format_data.sensorData or format_data.targetAngles:
                        payload = format_data_to_measurement_payload(format_data)
                        get_api().upload_measurement(
                            session_id=session_id,
                            target_angles=payload["targetAngles"],
                            errors=payload["errors"],
                            sensor_data=payload["sensorData"],
                            token=token,
                        )
                        LOGGER.debug(
                            "Auto-uploaded %d samples, %d angles",
                            len(format_data.sensorData),
                            len(format_data.targetAngles),
                        )
                except Exception:
                    LOGGER.exception("Auto-upload error")
                time.sleep(interval)
            _upload_running = False

        _upload_thread = threading.Thread(
            target=upload_loop, daemon=True, name="m1-auto-upload"
        )
        _upload_thread.start()
        return jsonify({"message": "Auto-upload started", "interval": interval}), 200

    @app.route("/api/measurement/auto-upload/stop", methods=["POST"])
    def api_auto_upload_stop():
        nonlocal _upload_running
        _upload_running = False
        return jsonify({"message": "Auto-upload stopped"}), 200

    # ── V2 Query APIs ─────────────────────────────────────────────

    @app.route("/api/session/<int:session_id>", methods=["GET"])
    def api_get_session(session_id):
        token = request.args.get("token", "")
        try:
            result = get_api().get_session(session_id, token)
            return jsonify(result), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/recommendations/session/<int:session_id>", methods=["GET"])
    def api_get_recommendations(session_id):
        token = request.args.get("token", "")
        try:
            result = get_api().get_session_recommendations(session_id, token)
            return jsonify(result), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/recommendations/engine/<int:user_id>", methods=["GET"])
    def api_get_engine_recommendations(user_id):
        token = request.args.get("token", "")
        try:
            result = get_api().get_engine_recommendations(user_id, token)
            return jsonify(result), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/schedule/<int:user_id>", methods=["GET"])
    def api_get_schedule(user_id):
        token = request.args.get("token", "")
        try:
            result = get_api().get_schedule(user_id, token)
            return jsonify(result), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    return app
