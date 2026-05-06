"""
Main entry point for the Limb Motion Recognition App.
Wires S1, S2, M1 modules together and starts the Flask web server.

Both simulated and real BLE data sources are always available.
The user selects the mode and configures sensors from the web UI.
No command-line arguments needed — just run: python main.py
"""

from __future__ import annotations

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
LOGGER = logging.getLogger("main")

# Default config file path for sensor configuration
SENSORS_CONFIG_PATH = "config/sensors.json"
HOST = "127.0.0.1"
PORT = 5000


def create_sim_s1_module():
    """Create simulated S1 module (always available)."""
    from src.s2.simulator import SimulatedS1Module

    return SimulatedS1Module(
        sensor_ids=[
            {"deviceId": "SIM_SENSOR_A", "deviceName": "SimUpperLeg"},
            {"deviceId": "SIM_SENSOR_B", "deviceName": "SimLowerLeg"},
        ],
        sample_rate_hz=50.0,
    )


WITMOTION_NOTIFY_UUID = "0000ffe4-0000-1000-8000-00805f9a34fb"


def load_real_s1_module():
    """Try to create real BLE S1 module from config/sensors.json.

    The config file can be either:
    - A list of MAC address strings: ["AA:BB:CC:DD:EE:F1", "AA:BB:CC:DD:EE:F2"]
    - A list of objects: [{"device_address": "...", "notify_char_uuid": "..."}]

    Returns None if config doesn't exist or is invalid.
    The user can also configure sensors from the web UI at runtime.
    """
    import json
    from pathlib import Path

    config_path = Path(SENSORS_CONFIG_PATH)
    if not config_path.exists():
        LOGGER.info("No sensor config at %s — real BLE configurable from UI", SENSORS_CONFIG_PATH)
        return None

    try:
        from src.s1.ble_tunnel import SensorConfig, ServiceConfig
        from src.s1.service import create_s1_module

        raw = json.loads(config_path.read_text(encoding="utf-8"))
        sensors = []
        for entry in raw:
            if isinstance(entry, str):
                # Simple MAC address string
                sensors.append(SensorConfig(
                    device_address=entry.strip(),
                    notify_char_uuid=WITMOTION_NOTIFY_UUID,
                ))
            elif isinstance(entry, dict):
                sensors.append(SensorConfig(
                    device_address=entry["device_address"],
                    notify_char_uuid=entry.get("notify_char_uuid", WITMOTION_NOTIFY_UUID),
                ))
        LOGGER.info("Real BLE sensors loaded from config: %d device(s)", len(sensors))
        return create_s1_module(sensors, ServiceConfig())
    except Exception as exc:
        LOGGER.warning("Failed to load sensor config: %s", exc)
        return None


def main() -> None:
    # 1. Create both S1 modules
    s1_sim = create_sim_s1_module()
    LOGGER.info("Simulated S1 module initialized")

    s1_real = load_real_s1_module()

    # 2. Create S2 module with both data sources
    from src.s2.session_service import S2Module

    s2 = S2Module(s1_real_module=s1_real, s1_sim_module=s1_sim)
    LOGGER.info(
        "S2 module initialized (real BLE %s)",
        "available" if s1_real else "not configured — use UI to configure",
    )

    # 3. Create and start M1 Flask app
    from src.m1.app import create_app
    from src.m1.api_client import V2ApiClient

    api_client = V2ApiClient()
    app = create_app(s2, api_client)

    LOGGER.info("Starting M1 web server on %s:%d", HOST, PORT)
    LOGGER.info("Open http://%s:%d in your browser", HOST, PORT)

    app.run(host=HOST, port=PORT)


if __name__ == "__main__":
    main()
