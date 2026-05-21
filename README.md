# TideTracker

A professional, modular Raspberry Pi Zero W application for tracking tides with an e-ink display. Optimized for low-power operation and rapid boot cycles.

## 🏗 Project Architecture

The project is structured as a modular Python package for maximum efficiency and maintainability:

- **`main.py`**: The primary entry point. Handles automatic root escalation and lazy loading of heavy modules.
- **`app/`**: Core application logic.
  - **`core.py`**: Orchestrates the boot sequence, mode detection (RUN vs SETUP), and power management.
  - **`display/`**: Tide plotting and e-ink driver wrappers.
  - **`network/`**: WiFi manager and captive portal for headless configuration.
  - **`utils/`**: Centralized configuration, pathing, and station data handling.
- **`data/`**: Persistent storage for station IDs and WiFi credentials.
- **`ui/`**: Web assets for the WiFi configuration portal.
- **`resources/`**: Fonts and images.
- **`scratch/`**: Deprecated scripts and test utilities.

## ⚡ Technical Implementation

### Power & Timing (The 2-Minute Window)
To maximize battery life, the Pi boots every ~2 hours and must complete its task within a **2-minute window** before being hard-powered off by a TPL5110 timer.
- **Lazy Loading**: Heavy libraries like `matplotlib` and `numpy` are only imported when actually plotting, allowing the WiFi setup mode to launch instantly.
- **TPL5110 Integration**: The script pulses the `DONE` pin as its highest priority in the final cleanup phase to signal a successful cycle.

### Threading & Synchronization
The project uses the `threading` module to allow the web server to respond to users *before* tearing down the WiFi hotspot to test new credentials. A global `NM_LOCK` prevents deadlocks when multiple modules interact with NetworkManager simultaneously.

### Persistence & Fallback
WiFi credentials and station IDs are stored in `data/persistence.json`. On startup, the system automatically "primes" its fallback by remembering the current working connection, ensuring it can always revert if new credentials fail.

## 🛠 Setup & Utilities

- **`sh_setup.sh`**: The master installer. Configures the Pi for high-speed booting, installs all dependencies, and sets up the Cron job.
- **`a.sh`**: A quick utility script for developers to pull updates, fix permissions, and run the project manually.

## 🚀 Getting Started

1. Clone the repo to `/home/pi/TideTracker_repo`.
2. Run `sudo bash sh_setup.sh`.
3. The Pi will reboot and either plot tides (RUN mode) or launch the `Rpi-hostname` hotspot (SETUP mode) depending on the hardware pin state.
