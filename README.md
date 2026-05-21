# TideTracker Project

## Technical Implementation: Threading and Synchronization

### Why Threads are Used
This project uses the `threading` module to manage the transition between Access Point (AP) mode and Station (client) mode during WiFi configuration.

WiFi management is a **blocking and destructive** process. When a user submits new credentials via the web UI:
1. **Asynchronous Response**: The HTTP server must send a "TESTING" response to the browser immediately so the user sees a confirmation.
2. **Avoiding Connection Cutoff**: If this were done synchronously, the server would call `stop_hotspot()` before sending the response, instantly killing the connection and leaving the user with a browser network error.
3. **Background Processing**: By spawning a **background thread**, the server can respond to the user first, then proceed to tear down the hotspot and test the new credentials.

### The NM_LOCK (Thread Safety)
Because the HTTP server remains active (listening for status polls from the browser) while the background thread interacts with the WiFi hardware, there is a risk of deadlocks or race conditions within the NetworkManager DBus interface.

The `NM_LOCK` in `netman.py` ensures that only one thread can communicate with NetworkManager at a time. This synchronizes:
* **Background tests**: Connecting, deactivating, and falling back.
* **UI Status updates**: Periodic scanning and connection status checks.

### Persistent Fallback and Resilience
To ensure the device remains accessible even after failed configuration attempts:
* **Auto-Capture**: On startup, the script attempts to capture and save the current working WiFi connection as a persistent fallback point in `tidetracker_persistent_data.json`.
* **Automatic Reversion**: If a new connection attempt fails (due to incorrect credentials or lack of internet), the background thread automatically attempts to restore the previous working connection before deciding to relaunch the hotspot.
