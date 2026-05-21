# wifi-connect-headless-rpi
An application written in python that displays a wifi configuration UI for the reaspberry pi zero devices.   The installation program is written to work even when you only have a headless (wifi) connection to the reaspberry pi zero.    

Please see the complete writeup at the [www.surfncircuits.com Simplifying WiFi connections for Raspberry Pi Zero W projects ](https://surfncircuits.com/?p=5953) blog: 

Inspired by [wifi-connect](https://github.com/balena-io/wifi-connect) project written by [balena.io](https://www.balena.io/) and forked from the [python-wifi-connect](https://github.com/OpenAgricultureFoundation/python-wifi-connect) written by [OpenAgricultureFoundation](https://github.com/OpenAgricultureFoundation) .

# Install and Run

Please read the [INSTALL.md](INSTALL.md) then the [RUN.md](RUN.md) files.

# How it works
![How it works](./docs/images/how-it-works.png?raw=true)

WiFi Connect interacts with NetworkManager, which should be the active network manager on the device's host OS.

### 1. Error:  No valid WiFi network

At boot, a valid WiFi network is not found

### 2. Advertise: Device Creates Access Point

WiFi Connect detects available WiFi networks and opens an access point with a captive portal. Connecting to this access point with a mobile phone or laptop allows new WiFi credentials to be configured.

### 3. Connect: User Connects Phone to Device Access Point

Connect to the opened access point on the device from your mobile phone or laptop. The access point SSID is, by default, `Rpi-hostname` where hostname if the device name. 

### 4. Portal: Phone Shows Portal to User in Web Browser

After connecting to the access point from a mobile phone, it will detect the captive portal and open its web page. Opening any web page will redirect to the captive portal as well.  The default IP address is 192.168.42.1

### 5. Credentials: User Enters Local WiFi Network Credentials on Phone

The captive portal provides the option to select a WiFi SSID from a list with detected WiFi networks and enter a passphrase for the desired network.

### 6. Connected!: Device Connects to Local WiFi Network

When the network credentials have been entered, WiFi Connect will disable the access point and try to connect to the network. If the connection fails, it will enable the access point for another attempt. If it succeeds, the configuration will be saved by NetworkManager.

# Technical Implementation: Threading and Synchronization

### Why Threads are Used
This application uses the `threading` module to manage the transition between Access Point (AP) mode and Station (client) mode. 

WiFi management is a **blocking and destructive** process. When a user submits new credentials via the web UI:
1. The HTTP server must send a "TESTING" response to the browser immediately so the user sees a confirmation.
2. If this were done synchronously, the server would call `stop_hotspot()` before sending the response, instantly killing the connection and leaving the user with a browser error.
3. By spawning a **background thread**, the server can respond to the user first, and then proceed to tear down the hotspot and test the new credentials.

### The NM_LOCK (Thread Safety)
Because the HTTP server remains active (listening for status polls) while the background thread interacts with the WiFi hardware, there is a risk of **deadlocks or race conditions** within the NetworkManager DBus interface.

The `NM_LOCK` in `netman.py` ensures that only one thread can communicate with NetworkManager at a time. This synchronizes:
* **Background tests**: Connecting, deactivating, and falling back.
* **UI Status updates**: Periodic scanning and connection status checks.

### Persistent Fallback Mechanism
To improve reliability, the system maintains a "last known working" WiFi profile in `tidetracker_persistent_data.json`.
* **Auto-Capture**: On startup, the script attempts to "remember" the current working WiFi connection.
* **Automatic Fallback**: If a new connection attempt fails (due to an incorrect password or lack of internet), the background thread will automatically attempt to restore the previous working connection before deciding whether to relaunch the hotspot.
