# Limb Motion Recognition & Assistant — App Side (S1 + S2 + M1)

## 1. Project Overview

This is the **App-side** implementation of the "Limb Motion Recognition and Assistant" rehabilitation training platform (DSD 2025-2026, UTAD x Jilin University). Three modules run on the same Windows device:

- **S1 (Sensor)**: Connects to WitMotion BLE IMU sensors, collects raw motion data.
- **S2 (Data Acquisition & Processing)**: Validates sensor data, computes joint angles from paired sensors, buffers output.
- **M1 (App Frontend)**: Web UI for user login, session control, real-time data display, and data upload to the V2 backend server.

**Data flow**: S1 (BLE sensors) → S2 (processing) → M1 (display + upload to V2)

---

## 2. Project Structure

```
.
├── main.py                         # Entry point — starts all modules and the web server
├── config/
│   └── sensors.json                # (Optional) Pre-configured BLE sensor addresses
├── log/                            # Angle log files (CSV), auto-generated per session
├── src/
│   ├── s1/                         # S1 module (BLE sensor interface)
│   │   ├── __init__.py
│   │   ├── ble_tunnel.py           # BLE communication, IMU packet parsing, SensorSample/SensorStatus
│   │   └── service.py              # S1Module facade: s1.sensor.read/status, s1.session.start/stop
│   ├── s2/                         # S2 module (data acquisition & processing)
│   │   ├── __init__.py
│   │   ├── data_classes.py         # IS-defined data classes: FormatData, TargetAngle, ErrorEvent, etc.
│   │   ├── simulator.py            # S2-01: Simulated sensor data generator (for testing without hardware)
│   │   ├── core.py                 # S2-02: Data validation, joint angle computation, error detection
│   │   ├── async_buffer.py         # S2-03: Thread-safe async buffer for decoupling S2 from M1
│   │   └── session_service.py      # S2Module facade: s2.session.start/stop, s2.data.read
│   └── m1/                         # M1 module (web frontend)
│       ├── __init__.py
│       ├── api_client.py           # HTTP client for V2 backend (auth, sessions, measurements, etc.)
│       ├── app.py                  # Flask web application (routes, API endpoints)
│       └── templates/
│           └── index.html          # Single-page web UI
```

### Key Files

| File | Role |
|------|------|
| `main.py` | Entry point. Creates S1 (simulator + optional real BLE), S2, M1 modules and starts Flask server. |
| `src/s1/ble_tunnel.py` | BLE connection, WitMotion IMU data parsing. Defines `SensorSample`, `SensorStatus`. |
| `src/s1/service.py` | Wraps S1's async BLE operations into sync calls. Provides `s1.sensor.read()`, `s1.sensor.status()`, `s1.session.start()`, `s1.session.stop()`. |
| `src/s2/data_classes.py` | All data structures defined by the Interface Specification: `StartResult`, `SessionSummary`, `FormatData`, `SessionContext`, `TargetAngle`, `ErrorEvent`. |
| `src/s2/simulator.py` | Generates fake IMU data (sinusoidal motion) for testing the full pipeline without physical sensors. |
| `src/s2/core.py` | Core processing: sample validation, joint angle computation (three binding modes), temporal alignment of asynchronous sensor pairs. |
| `src/s2/async_buffer.py` | Thread-safe buffer with per-consumer cursors, allowing both the auto-upload thread and the UI polling to read data independently. |
| `src/s2/session_service.py` | S2 facade. Parses `payloadStatus` for binding mode, orchestrates S1 session, S2 core, and async buffer lifecycle. |
| `src/m1/api_client.py` | HTTP client for all V2 backend endpoints (auth, sessions, measurements, recommendations, schedule). Includes `format_data_to_measurement_payload()` for converting S2 output to V2 upload format. |
| `src/m1/app.py` | Flask application. Provides internal API routes for the web UI (login, session control, data reading, measurement upload, sensor configuration, mode switching). |
| `src/m1/templates/index.html` | Single-page web UI with login/register, data source & binding mode selection, BLE sensor configuration, session control, real-time data display (sensor table, angle cards, line chart, error table), and AI recommendations. |

---

## 3. Prerequisites

- **Python 3.10+**
- **pip packages**: `flask`, `requests`, `bleak` (BLE, required for real sensors only)
- **WitMotion BLE IMU sensors** (e.g. WT901BLE, BWT901CL) for real data collection
- **WitMotion PC software** (e.g. "WitMotion Bluetooth 5.0 Tool") for viewing sensor MAC addresses and calibration

Install dependencies:

```bash
pip install flask requests bleak
```

---

## 4. Running the Application

```bash
python main.py
```

No command-line arguments needed. The application starts a web server on `http://127.0.0.1:5000`.

Open a browser and navigate to:

```
http://127.0.0.1:5000
```

---

## 5. Sensor Terminology

Throughout this guide, we use the following terms to describe the WitMotion IMU sensor (a flat rectangular device):

```
        ┌─────────────────────┐
        │                     │  ← Top edge
        │                     │
        │    Component face   │  ← The face with printed text / visible components
        │    (front face)     │
        │                     │
        │                     │
        └────────┬────────────┘
                 │ USB-C          ← Connector edge (has USB-C / charging port)
```

| Term | Description |
|------|-------------|
| **Component face** (front) | The flat face with printed text and visible electronic components. |
| **Mounting face** (back) | The opposite flat face, typically smooth. Used for attaching to the body. |
| **Long axis** | The longer dimension of the sensor (from connector edge to the opposite top edge). |
| **Short axis** | The shorter dimension (left to right when viewed from the front). |
| **Connector edge** | The narrow edge with the USB-C charging port. |
| **Component-face normal** | The direction perpendicular to the component face, pointing outward from it. |
| **Long-axis direction** | The direction along the long axis, from the connector edge toward the top edge. |

### IMU Coordinate System (WitMotion)

| Axis | Physical direction | Euler angle (rotation around this axis) |
|------|-------------------|----------------------------------------|
| X | Along the **short axis** | **Roll** (Angle X) — range ±180° |
| Y | Along the **long axis** | **Pitch** (Angle Y) — range ±90° |
| Z | **Component-face normal** | **Yaw** (Angle Z) — range ±180° |

---

## 6. Sensor Binding Methods

The system supports three sensor binding methods. The binding method determines how the sensor is physically attached to the limb and which algorithm is used to compute joint angles.

### 6.1 Back-mount mode (`back`) — Recommended

**Physical setup**: Attach the sensor with the **mounting face (back) against the skin/strap** on the front or top surface of the limb. The **component face points outward**, away from the limb. The **long axis is parallel** to the limb bone (connector edge toward the foot for lower leg, toward the knee for upper leg).

**Algorithm**: Computes the angle between the **component-face normals** (Z axis) of the two sensors.
- Uses: **Roll + Pitch** only
- Invariant to: Yaw changes (yaw drift does not affect the result)
- Sensitive to: Sensor rotating around the limb surface (e.g., sliding from front of leg to the side)

**Best for**: General use. Stable against yaw drift. Easy to strap tightly because the flat back conforms to the limb surface.

### 6.2 Connector-mount mode (`port`)

**Physical setup**: Attach the sensor so that the **long-axis direction (connector edge → top edge) is aligned along the limb bone**. For the **upper leg**, the connector edge points toward the knee; for the **lower leg**, the connector edge points toward the foot. This ensures both sensors' long-axis directions point upward along the leg when standing. The component face may point in any direction — the sensor may rotate around the limb without affecting results.

**Algorithm**: Computes the angle between the **long-axis directions** (Y axis) of the two sensors.
- Uses: **Roll + Pitch + Yaw** (all three)
- Invariant to: Sensor rotating around the limb (strap looseness)
- Sensitive to: Yaw drift (yaw value may shift after rapid movement)

**Best for**: Situations where the strap is loose and the sensor may rotate around the limb. Requires stable yaw.

### 6.3 Screen-mount mode (`screen`)

**Physical setup**: Attach the sensor so that the **component-face normal points along the limb bone** — i.e., the component face itself faces toward the knee or away from it. The sensor lies with its flat face tangent to the leg-bone direction.

**Algorithm**: Same as back-mount — angle between component-face normals (Z axis).
- Uses: **Roll + Pitch** only
- Invariant to: Yaw changes
- Sensitive to: Sensor rotating around the component-face normal

**Best for**: Special mounting configurations. Difficult to strap stably in practice.

### Summary

| Mode | Strap method | Computed axis | Angles used | Robust to strap rotation | Robust to yaw drift |
|------|-------------|---------------|-------------|--------------------------|---------------------|
| `back` | Mounting face on limb | Z (component-face normal) | Roll + Pitch | No | Yes |
| `port` | Connector along limb | Y (long axis) | Roll + Pitch + Yaw | Yes | No |
| `screen` | Component face along limb | Z (component-face normal) | Roll + Pitch | No | Yes |

---

## 7. Step-by-Step Operation Guide

### Step 1: Obtain sensor MAC addresses

1. Power on your WitMotion BLE IMU sensors.
2. Open the **WitMotion Bluetooth Tool** (or similar BLE scanner software) on your PC.
3. Scan for nearby BLE devices. Identify your sensors by name (e.g., "WT901BLE68").
4. Note down the **MAC addresses** of each sensor. Example:
   ```
   d2:26:08:77:94:1b
   d9:bc:b5:1e:39:35
   ```

### Step 2: (Recommended) Calibrate sensors

For accurate results, both sensors should be calibrated to the same reference:

1. Place both sensors **flat on a level surface**, component face up.
2. In the WitMotion software, perform **Accelerometer Calibration** for each sensor.
3. This ensures both sensors report consistent roll/pitch/yaw values for the same physical orientation.

### Step 3: Start the application

```bash
python main.py
```

Open `http://127.0.0.1:5000` in your browser.

### Step 4: Log in or register

On the web page, you will see the **Authentication** section.

- **To register a new account**: Fill in Name, Email, Password on the right side, click **Register**.
- **To log in with an existing account**: Fill in Email, Password on the left side, click **Login**.

After successful login, the status bar shows: `Logged in as <name> (ID: <id>)`.

The **Session Control** and **AI Recommendations** sections will appear.

### Step 5: Configure BLE sensors

In the **Session Control** section:

1. Click the **"Configure BLE sensors..."** link to expand the configuration panel.
2. Enter the MAC addresses of your sensors, **one per line**:
   ```
   d2:26:08:77:94:1b
   d9:bc:b5:1e:39:35
   ```
3. Click **Apply**. You should see "Configured 2 BLE sensor(s)".

### Step 6: Select data source

- Click **Real BLE Sensors** to use real hardware (the button is now available after configuring sensors).
- Click **Simulator** to use simulated data (for testing without hardware).

The currently active mode is highlighted.

### Step 7: Select binding method

In the **Sensor Binding Method** dropdown, choose one of:

- **Back-mount: mounting face on limb, component face outward (roll+pitch)** — recommended, see Section 6.1
- **Connector-mount: connector edge along limb (roll+pitch+yaw)** — see Section 6.2
- **Screen-mount: component-face normal along limb (roll+pitch)** — see Section 6.3

### Step 8: Set exercise type

In the **Exercise Type** field, enter the exercise identifier. Example:

```
bend_knee_10
```

This value is included in the session metadata sent to the V2 backend.

### Step 9: Configure sensor-joint mapping

In the **Sensor-Joint Mapping** text area, enter a JSON object mapping each sensor's MAC address to a joint name. **Two sensors mapped to the same joint name form a pair** for angle computation.

Example for left knee measurement:

```json
{
  "d2:26:08:77:94:1b": "left_knee",
  "d9:bc:b5:1e:39:35": "left_knee"
}
```

Example for both knees (requires 4 sensors):

```json
{
  "aa:bb:cc:dd:ee:01": "left_knee",
  "aa:bb:cc:dd:ee:02": "left_knee",
  "aa:bb:cc:dd:ee:03": "right_knee",
  "aa:bb:cc:dd:ee:04": "right_knee"
}
```

### Step 10: Attach sensors to the body

Attach the sensors to the limb according to the selected binding method (see Section 6). For the recommended **back-mount mode**:

1. Strap the first sensor to the **upper leg (thigh)** with the mounting face against the limb, component face outward, connector edge pointing toward the knee.
2. Strap the second sensor to the **lower leg (shin)** with the same orientation, connector edge pointing toward the foot.
3. Ensure the long axis of each sensor is parallel to the corresponding limb bone.

### Step 11: Start the session

Click **Start Session**. The system will:

1. Create a session on the V2 backend server (obtaining a server-generated session ID).
2. Start data acquisition on S2 (which starts S1's BLE connection to the sensors).
3. Begin automatic periodic upload of measurement data to V2.

The status bar shows: `Session <id> active`.

The **Real-time Sensor Data** section appears with live data.

### Step 12: Monitor real-time data

The data section shows:

| Area | Description |
|------|-------------|
| **Summary cards** (Samples / Angles / Errors) | Running totals for the current session. |
| **Joint angle cards** | Current angle value for each joint, color-coded. |
| **Joint Angle History** (line chart) | Full session history of joint angles over time (X axis = seconds since start). |
| **Recent Sensor Samples** (table) | Latest raw IMU readings: timestamp (ms precision), sensor name, Roll, Pitch, Yaw, AccX/Y/Z, GyroX/Y/Z. |
| **Target Angles Log** (table) | Recent computed joint angles with timestamps. |
| **Errors** (table) | Error events: sensor disconnection, validation failures, timeouts. |

### Step 13: Stop the session

Click **Stop Session**. The system will:

1. Stop data acquisition.
2. End the session on the V2 backend.
3. Display a summary: total samples collected and error count.

### Step 14: View recommendations

In the **AI Recommendations** section, click **Load Recommendations** to fetch AI-generated exercise analysis from the V2 backend. This shows per-joint accuracy and improvement suggestions based on past sessions.

---

## 8. Angle Log Files

Each session automatically generates a CSV log file in the `log/` directory:

```
log/angles_20260506_165038.csv
```

Format:

```csv
timestamp_ms,timestamp_iso,angleID,angle_deg
1746538238549,2026-05-06T16:50:38.549Z,left_knee,42.35
1746538238612,2026-05-06T16:50:38.612Z,left_knee,43.10
```

| Column | Description |
|--------|-------------|
| `timestamp_ms` | Unix timestamp in milliseconds |
| `timestamp_iso` | ISO 8601 formatted timestamp |
| `angleID` | Joint name (from sensor-joint mapping) |
| `angle_deg` | Computed joint angle in degrees |

---

## 9. Angle Computation Algorithm

### 9.1 Temporal Alignment

The two sensors in a pair sample independently at different times. Before computing an angle, the system finds matching sample pairs:

1. For each sample from sensor A, find the closest-in-time sample from sensor B.
2. If the time difference is within 100ms, the pair is used for angle computation.
3. The output timestamp is the average of the two sample timestamps.
4. Each sample is used at most once (consumed after pairing).
5. Samples older than 2 seconds are discarded.

### 9.2 Direction Vector Computation

**Back-mount / Screen-mount mode** (Z axis, component-face normal):

```
Z_world = Ry(pitch) · Rx(roll) · [0, 0, 1]

wx =  sin(pitch) · cos(roll)
wy = -sin(roll)
wz =  cos(pitch) · cos(roll)
```

Uses Roll and Pitch only. Yaw does not appear. Invariant to yaw drift.

**Connector-mount mode** (Y axis, long-axis direction):

```
Y_world = Rz(yaw) · Ry(pitch) · Rx(roll) · [0, 1, 0]

wx = cos(yaw) · sin(pitch) · sin(roll) - sin(yaw) · cos(roll)
wy = sin(yaw) · sin(pitch) · sin(roll) + cos(yaw) · cos(roll)
wz = cos(pitch) · sin(roll)
```

Uses all three angles. Invariant to rotation around the long axis (strap looseness).

### 9.3 Joint Angle

The joint angle is the angle between the two direction vectors:

```
angle = arccos( V_a · V_b )
```

where `V_a · V_b` is the dot product, clamped to [-1, 1] for numerical safety.

- **0°** = limb segments parallel (leg straight)
- **90°** = limb segments perpendicular (knee bent 90°)
- **180°** = limb segments antiparallel

---

## 10. V2 Backend Communication

M1 communicates with the V2 backend server via HTTP REST API.

**Base URL**: `https://dsd2026-teamv2-production.up.railway.app`

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/auth/register` | POST | Register new user account |
| `/auth/login` | POST | Login, obtain JWT token |
| `/auth/me` | GET | Get current user info |
| `/sessions` | POST | Create a new rehabilitation session |
| `/sessions/:id` | GET | Get session details |
| `/sessions/:id/end` | PATCH | End an active session |
| `/measurements` | POST | Upload sensor measurement data |
| `/measurements/batch` | POST | Upload multiple measurements at once |
| `/recommendations/session/:id` | GET | Get AI recommendations for a session |
| `/recommendations/engine/:userId` | GET | Get auto-analysis across recent sessions |
| `/users/:id` | GET | Get user info |
| `/measurements/:sessionId` | GET | Get all measurements for a session |
| `/schedule/:userId` | GET | Get rehabilitation schedule |
| `/schedule/:id` | PATCH | Update schedule status |
| `/push/register` | POST | Register device push token |

Authentication: JWT token via `Authorization: Bearer <token>` header.

Measurement upload format:

```json
{
  "sessionId": 1,
  "targetAngles": [
    {"timestamp": "2026-05-06T16:50:38.549Z", "angleID": "left_knee", "angle": 42.35}
  ],
  "errors": [],
  "sensorData": [
    {
      "timestamp": "2026-05-06T16:50:38.549Z",
      "sensorId": "d2:26:08:77:94:1b",
      "accX": 0.23, "accY": 0.29, "accZ": 0.97,
      "gyroX": 0.0, "gyroY": 3.11, "gyroZ": -0.73,
      "roll": 15.57, "pitch": -13.78, "yaw": -144.01
    }
  ]
}
```

---

## 11. Interface Compliance

All cross-team interfaces strictly follow the **Total Interface Specification v1.0** (IS). No interfaces are added, removed, or modified.

### App-side interfaces (function calls)

| Interface | Direction | Function | Data |
|-----------|-----------|----------|------|
| IF-S1-S2 | S1 → S2 | `s1.sensor.read()` → `List[SensorSample]` | Raw IMU samples |
| IF-S1-S2 | S1 → S2 | `s1.sensor.status()` → `SensorStatus` | Connection status |
| IF-S2-S1 | S2 → S1 | `s1.session.start(sessionMetaData)` | Start data collection |
| IF-S2-S1 | S2 → S1 | `s1.session.stop()` | Stop data collection |
| IF-M1-S2 | M1 → S2 | `s2.session.start(sessionId, userId, sensorJointMapping, payloadStatus)` → `StartResult` | Start acquisition session |
| IF-M1-S2 | M1 → S2 | `s2.session.stop()` → `SessionSummary` | Stop session |
| IF-S2-M1 | S2 → M1 | `s2.data.read()` → `FormatData` | Formatted sensor data |

### Data classes

All field names and types match the IS exactly:

- `SensorSample`: timestamp(int), deviceId(str), deviceName(str), accX/Y/Z(float), gyroX/Y/Z(float), roll/pitch/yaw(float)
- `SensorStatus`: connected(bool), errorMessage(str|None)
- `StartResult`: success(bool), errorMessage(str|None)
- `SessionSummary`: sessionId(int), sampleCount(int), errorCount(int), startTime(str), endTime(str)
- `FormatData`: sessionContext(SessionContext), sensorData(List[SensorSample]), targetAngles(List[TargetAngle]), errors(List[ErrorEvent])
- `SessionContext`: sessionId(int), userId(int), sensorJointMapping(dict), payloadStatus(str)
- `TargetAngle`: timestamp(int), angleID(str), angle(float)
- `ErrorEvent`: timestamp(int), sensorId(str|None), errorType(str), message(str)
