# Project Restructuring and Refactoring Plan

Restructure the TideTracker project from an amateur "scripts-based" organization to a professional, modular Python package. This will involve cleaning up the bloated WiFi library, consolidating core logic, and improving the overall architecture for efficiency and elegance.

## User Review Required

> [!IMPORTANT]
> **Proposed Folder Structure Change**: I plan to rename many files and consolidate folders. This will break any existing external scripts that rely on specific file paths (like old cron jobs), unless they are updated to point to the new entry point.

- **WiFi Library Consolidation**: I propose moving the essential `src` and `ui` parts of the WiFi library into the main project and deleting the bloated metadata/docs from the original fork.
- **Project Name**: I suggest renaming the root folder or core package to `tidetracker` for a professional namespace.

## [UPDATED] Proposed Changes

### 1. System Integration & Performance (2-Minute Window)
- **Lazy Module Loading**: The `main.py` entry point will only import `app.core`. `app.core` will check the 'run' pin **before** importing the high-overhead `app.display.plotter` (matplotlib/numpy) or `app.network.portal`.
- **Power Safety**: The TPL5110 `DONE` pulse logic will be the highest priority in the `finally:` block of the main entry point to ensure hard-power-off always occurs within the 2-minute window.
- **Root Escalation**: Maintain the instant `os.execvp("sudo", ...)` logic at the very top of `main.py` for zero-latency permission handling.

### 2. File Reorganization
I will relocate files into a professional hierarchy while maintaining compatibility with `sh_setup.sh`.

- **`sh_setup.sh`**: Update to recursively find and `chmod +x` scripts in the new `app/` subdirectories.
- **`script_to_run_on_boot.sh`**: Update to call `python3 main.py`.
- **Legacy Support**: Keep a temporary `0_boot_sense.py` that simply executes `main.py` to prevent breaking existing cron jobs.

### 3. WiFi Library Integration
- **Extract functional core**: Only move `http_server.py`, `netman.py`, and `dnsmasq.py` into `app/network/`.
- **UI Assets**: Move the `ui/` folder to the project root.
- **Cleanup**: Delete the remainder of the `forked_wifi...` folder (docs, install scripts, metadata) to remove bloat.

---

## Verification Plan

### Automated Tests
- I will implement a basic "dry-run" mode for the core logic to verify modularity without needing real hardware for every test.
- Check path resolution consistency across all modules.

### Manual Verification
- Verify the script still runs as root (self-escalation check).
- Verify the Captive Portal still launches correctly with its UI assets.
- Verify the Tide Plotter still generates the `plot_image.bmp` correctly.
