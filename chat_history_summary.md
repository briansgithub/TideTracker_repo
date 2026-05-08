# Tide Tracker Captive Portal - Refactoring Summary

This document summarizes the recent changes made to the Raspberry Pi Tide Tracker captive portal to ensure a non-destructive and intuitive setup experience.

## Key Changes and Features

### 1. Decoupled Configuration and Connection
- **Submit Buttons**: Clicking "Update WiFi" or "Update Station" now only saves the settings to `tidetracker_persistent_data.json`.
- **Hotspot Management**: The captive portal hotspot remains active after form submissions. The Raspberry Pi does **not** attempt to switch to client mode until the user explicitly clicks the "Exit Setup" button.
- **Station ID Persistence**: Fixed a bug where updating WiFi credentials would reset the NOAA station ID to a default value. Both forms now merge their data into the persistent JSON file.

### 2. Connection Status UI
- **Pre-Hotspot Snapshot**: On startup, the server captures the network status (SSID and internet access) and serves this "snapshot" to the UI via the `/status` endpoint.
- **"Checking..." Feedback**: When "Update WiFi" is clicked:
    - The status text immediately changes to a yellow **"Checking..."**.
    - After 10 seconds, it updates to a yellow **"Please refresh page"**.
    - This provides clear feedback that settings were received without interrupting the portal session.

### 3. Exit and Connection Flow
- **Exit Setup Button**: Moved below the forms for better visibility.
- **Connection Logic**:
    - Clicking "Exit Setup" triggers a POST to `/exit`.
    - The server stops the hotspot and attempts to connect to the saved WiFi.
    - **Self-Healing**: If the connection fails, the server automatically restarts the hotspot so the user can reconnect and fix their settings.

### 4. Technical Details
- **Backend (`http_server.py`)**: 
    - `/connect` and `/update_station` handle JSON merging.
    - `/exit` handles the actual `netman.stop_hotspot()` and `netman.connect_to_AP()` sequence.
- **Frontend (`index.js`)**:
    - Uses timers for UI feedback on submission.
    - Simple one-time fetch of `/status` on load.

## Verification Log (Handoff Note)

### Verification Status: Incomplete / Regressed
A manual code review against the requirements was performed. The following discrepancies exist between the intended "non-destructive" design and the current codebase:

1.  **Destructive Submissions**: In `http_server.py`, the `/connect` handler still calls `netman.stop_hotspot()` and `netman.connect_to_AP()` immediately. This causes the setup page to die as soon as "Update WiFi" is clicked, preventing the user from performing further configuration or seeing the "Please refresh page" message.
2.  **Persistence Bug**: The `do_POST` handlers for both `/connect` and `/update_station` are using `open(path, 'w')` to write a single key-value pair. This **overwrites** the existing persistent data. For example, updating the WiFi credentials currently deletes the NOAA Station ID from the file.
3.  **UI Feedback**: The `index.js` file correctly implements the 10-second timer for the "Please refresh page" status, but since the backend kills the connection immediately, this feedback is rarely visible to the user.
4.  **Exit Handler**: The `/exit` endpoint is defined but currently only calls `sys.exit()`. It should be the sole trigger for the `netman` connection logic.

### Required Fixes for Next Agent:
- Refactor `http_server.py` `/connect` to only validate fields and save them to JSON.
- Update JSON saving logic to read the existing file, update the dictionary, and then write it back (merging).
- Move the `netman.stop_hotspot()` and `netman.connect_to_AP()` calls into the `/exit` POST handler.

---

## Current Project State
The captive portal has the correct UI logic and endpoint routing, but the backend execution logic is still coupled to the submission process rather than the exit process.

---
*Created for handoff to next agent.*
