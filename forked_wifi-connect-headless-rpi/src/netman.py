# Start a local hotspot using NetworkManager.

# You must use https://developer.gnome.org/NetworkManager/1.2/spec.html
# to see the DBUS API that the python-NetworkManager module is communicating
# over (the module documentation is scant).

import NetworkManager
import uuid, os, sys, time, socket

# This is needed to work with NetworkManager 1.30.6 and python-networkmanager 2.2      
from dbus.mainloop.glib import DBusGMainLoop
DBusGMainLoop(set_as_default = True)

HOTSPOT_CONNECTION_NAME = 'hotspot'
GENERIC_CONNECTION_NAME = 'python-wifi-connect'


#------------------------------------------------------------------------------
# Returns True if we are connected to the internet, False otherwise.
def have_active_internet_connection(host="8.8.8.8", port=53, timeout=2):
   """
   Host: 8.8.8.8 (google-public-dns-a.google.com)
   OpenPort: 53/tcp
   Service: domain (DNS/TCP)
   """
   try:
     s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
     s.settimeout(timeout)
     s.connect((host, port))
     s.close()
     return True
   except Exception as e:
     #print(f"Exception: {e}")
     return False


#------------------------------------------------------------------------------
# Returns the SSID of the currently active WiFi client connection, or None.
def get_connected_ssid():
    try:
        for active in NetworkManager.NetworkManager.ActiveConnections:
            try:
                settings = active.Connection.GetSettings()
                conn_type = settings.get('connection', {}).get('type', '')
                wifi_settings = settings.get('802-11-wireless', {})
                mode = wifi_settings.get('mode', '')
                # Only report infrastructure (client) connections, not our own hotspot
                if conn_type == '802-11-wireless' and mode == 'infrastructure':
                    ssid = wifi_settings.get('ssid', None)
                    if ssid:
                        return ssid
            except Exception:
                pass
    except Exception as e:
        print(f'get_connected_ssid() error: {e}')
    return None


#------------------------------------------------------------------------------
# Remove ALL wifi connections - to start clean or before running the hotspot.
def delete_all_wifi_connections():
    # Get all known connections
    connections = NetworkManager.Settings.ListConnections()

    # Delete the '802-11-wireless' connections
    for connection in connections:
        if connection.GetSettings()["connection"]["type"] == "802-11-wireless":
            print("Deleting connection "
                + connection.GetSettings()["connection"]["id"]
            )
            connection.Delete()
    time.sleep(2)


#------------------------------------------------------------------------------
# Stop and delete the hotspot.
# Returns True for success or False (for hotspot not found or error).
def stop_hotspot():
    return stop_connection(HOTSPOT_CONNECTION_NAME)


#------------------------------------------------------------------------------
# Generic connection stopper / deleter.
# Deactivates any active instance first, then deletes ALL matching profiles.
def stop_connection(conn_name=GENERIC_CONNECTION_NAME):
    found_any = False

    # 1. Deactivate any active connection with this name
    try:
        active_connections = NetworkManager.NetworkManager.ActiveConnections
        for active in active_connections:
            try:
                if active.Connection.GetSettings()['connection']['id'] == conn_name:
                    print(f'Deactivating active connection: {conn_name}')
                    NetworkManager.NetworkManager.DeactivateConnection(active)
                    found_any = True
            except Exception:
                pass  # Active connection may have gone away
    except Exception as e:
        print(f'Error listing active connections: {e}')

    # 2. Delete ALL saved connection profiles with this name (handles duplicates)
    try:
        connections = NetworkManager.Settings.ListConnections()
        for conn in connections:
            try:
                if conn.GetSettings()['connection']['id'] == conn_name:
                    print(f'Deleting connection profile: {conn_name}')
                    conn.Delete()
                    found_any = True
            except Exception:
                pass  # Connection may have been removed already
    except Exception as e:
        print(f'Error listing connections: {e}')

    # 3. Wait for wifi device to reach a ready state
    if found_any:
        _wait_for_wifi_device_ready()

    return found_any


#------------------------------------------------------------------------------
# Return a list of available SSIDs and their security type, 
# or [] for none available or error.
def get_list_of_access_points():
    print(f'DEBUG: Entering get_list_of_access_points()')
    # bit flags we use when decoding what we get back from NetMan for each AP
    NM_SECURITY_NONE       = 0x0
    NM_SECURITY_WEP        = 0x1
    NM_SECURITY_WPA        = 0x2
    NM_SECURITY_WPA2       = 0x4
    NM_SECURITY_ENTERPRISE = 0x8
   
    

    ssids = [] # list we return

    for dev in NetworkManager.NetworkManager.GetDevices():
        print(f'DEBUG: Checking device: {dev.Interface} (Type: {dev.DeviceType})')
        if dev.DeviceType != NetworkManager.NM_DEVICE_TYPE_WIFI:
            continue
        
        aps = dev.GetAccessPoints()
        print(f'DEBUG: Found {len(aps)} access points on {dev.Interface}')
        for ap in aps:

            # Get Flags, WpaFlags and RsnFlags, all are bit OR'd combinations 
            # of the NM_802_11_AP_SEC_* bit flags.
            # https://developer.gnome.org/NetworkManager/1.2/nm-dbus-types.html#NM80211ApSecurityFlags

            security = NM_SECURITY_NONE

            # Based on a subset of the flag settings we can determine which
            # type of security this AP uses.  
            # We can also determine what input we need from the user to connect to
            # any given AP (required for our dynamic UI form).
            if ap.Flags & NetworkManager.NM_802_11_AP_FLAGS_PRIVACY and \
                    ap.WpaFlags == NetworkManager.NM_802_11_AP_SEC_NONE and \
                    ap.RsnFlags == NetworkManager.NM_802_11_AP_SEC_NONE:
                security = NM_SECURITY_WEP

            if ap.WpaFlags != NetworkManager.NM_802_11_AP_SEC_NONE:
                security = NM_SECURITY_WPA

            if ap.RsnFlags != NetworkManager.NM_802_11_AP_SEC_NONE:
                security = NM_SECURITY_WPA2

            if ap.WpaFlags & NetworkManager.NM_802_11_AP_SEC_KEY_MGMT_802_1X or \
                    ap.RsnFlags & NetworkManager.NM_802_11_AP_SEC_KEY_MGMT_802_1X:
                security = NM_SECURITY_ENTERPRISE

            #print(f'{ap.Ssid:15} Flags=0x{ap.Flags:X} WpaFlags=0x{ap.WpaFlags:X} RsnFlags=0x{ap.RsnFlags:X}')

            # Decode our flag into a display string
            security_str = ''
            if security == NM_SECURITY_NONE:
                security_str = 'NONE'
    
            if security & NM_SECURITY_WEP:
                security_str = 'WEP'
    
            if security & NM_SECURITY_WPA:
                security_str = 'WPA'
    
            if security & NM_SECURITY_WPA2:
                security_str = 'WPA2'
    
            if security & NM_SECURITY_ENTERPRISE:
                security_str = 'ENTERPRISE'

            entry = {"ssid": ap.Ssid, "security": security_str}
            print(f'DEBUG: AP Found - SSID: "{ap.Ssid}", Security: {security_str}')

            # Don't add duplicates to the list, issue #8
            if ssids.__contains__(entry):
                continue

            # Don't add other PFC's to the list!
            if ap.Ssid.startswith('Rpi-'+os.uname()[1]):
                continue

            ssids.append(entry)

    # always add a hidden place holder
    ssids.append({"ssid": "Enter a hidden WiFi name", "security": "HIDDEN"})

    print(f'DEBUG: Returning available SSIDs: {ssids}')
    return ssids


#------------------------------------------------------------------------------
# Get hotspot SSID name.
def get_hotspot_SSID():
    return os.uname()[1]


#------------------------------------------------------------------------------
# Wait for the wifi device to reach a DISCONNECTED (or equivalent ready) state.
# NM device states: 0=UNKNOWN, 10=UNMANAGED, 20=UNAVAILABLE, 30=DISCONNECTED,
#   40=PREPARE, 50=CONFIG, 60=NEED_AUTH, 70=IP_CONFIG, 80=IP_CHECK,
#   90=SECONDARIES, 100=ACTIVATED, 110=DEACTIVATING, 120=FAILED
def _wait_for_wifi_device_ready(timeout=20):
    # States where the device is idle enough to accept a new connection.
    # Includes FAILED (120) because after deactivation the device often
    # transitions DEACTIVATING(110) -> FAILED(120) -> DISCONNECTED(30),
    # and FAILED is a stable/idle state from which we can activate.
    READY_STATES = (
        NetworkManager.NM_DEVICE_STATE_DISCONNECTED,  # 30
        NetworkManager.NM_DEVICE_STATE_UNKNOWN,        # 0
        NetworkManager.NM_DEVICE_STATE_UNMANAGED,      # 10
        NetworkManager.NM_DEVICE_STATE_UNAVAILABLE,    # 20
        120,  # NM_DEVICE_STATE_FAILED — not always in python-networkmanager
    )
    try:
        for dev in NetworkManager.NetworkManager.GetDevices():
            if dev.DeviceType == NetworkManager.NM_DEVICE_TYPE_WIFI:
                elapsed = 0
                poll_interval = 0.25 # Poll every 250ms for responsiveness
                while dev.State not in READY_STATES and elapsed < timeout:
                    if int(elapsed * 4) % 4 == 0: # Print once per second to keep log clean
                        print(f'Waiting for wlan0 to become ready (state={dev.State}, elapsed={elapsed:.1f}s)...')
                    time.sleep(poll_interval)
                    elapsed += poll_interval
                if elapsed > 0:
                    time.sleep(0.5)  # reduced settle time from 2.0 to 0.5
                print(f'wlan0 device state: {dev.State} (waited {elapsed:.1f}s)')
                return
    except Exception as e:
        print(f'Error waiting for wifi device: {e}')


#------------------------------------------------------------------------------
# Start a local hotspot on the wifi interface.
# Returns True for success, False for error.
def start_hotspot():
    stop_hotspot()  # Remove any stale hotspot connection from previous runs

    # If the wifi device is still ACTIVATED (state=100) — e.g. the previous
    # run's atexit deleted the 'hotspot' profile but the device stayed up —
    # we must force it down before we can create a new hotspot.
    try:
        for dev in NetworkManager.NetworkManager.GetDevices():
            if dev.DeviceType == NetworkManager.NM_DEVICE_TYPE_WIFI:
                if dev.State == 100:  # NM_DEVICE_STATE_ACTIVATED
                    print(f'wlan0 still ACTIVATED — disconnecting all active wifi connections')
                    # Deactivate ALL active wifi connections
                    try:
                        for ac in NetworkManager.NetworkManager.ActiveConnections:
                            try:
                                for ac_dev in ac.Devices:
                                    if ac_dev.DeviceType == NetworkManager.NM_DEVICE_TYPE_WIFI:
                                        print(f'  Deactivating: {ac.Connection.GetSettings()["connection"]["id"]}')
                                        NetworkManager.NetworkManager.DeactivateConnection(ac)
                                        break
                            except Exception:
                                pass
                    except Exception as e:
                        print(f'  Error deactivating wifi connections: {e}')
                    # Also try to disconnect the device directly
                    try:
                        dev.Disconnect()
                    except Exception:
                        pass
                break
    except Exception as e:
        print(f'Error checking wifi device state: {e}')

    _wait_for_wifi_device_ready()
    return connect_to_AP(CONN_TYPE_HOTSPOT, HOTSPOT_CONNECTION_NAME, \
            get_hotspot_SSID())


#------------------------------------------------------------------------------
# Supported connection types for the function below.
CONN_TYPE_HOTSPOT        = 'hotspot'
CONN_TYPE_SEC_NONE       = 'NONE' # MIT
CONN_TYPE_SEC_PASSWORD   = 'PASSWORD' # WPA, WPA2 and WEP
CONN_TYPE_SEC_ENTERPRISE = 'ENTERPRISE' # MIT SECURE


#------------------------------------------------------------------------------
# Generic connect to the user selected AP function.
# Returns True for success, or False.
def connect_to_AP(conn_type=None, conn_name=GENERIC_CONNECTION_NAME, \
        ssid=None, username=None, password=None):

    #print(f"connect_to_AP conn_type={conn_type} conn_name={conn_name} ssid={ssid} username={username} password={password}")

    if conn_type is None or ssid is None:
        print(f'connect_to_AP() Error: Missing args conn_type or ssid')
        return False

    try:
        # This is the hotspot that we turn on, on the RPI so we can show our
        # captured portal to let the user select an AP and provide credentials.
        hotspot_dict = {
            '802-11-wireless': {'band': 'bg',
                                'mode': 'ap',
                                'ssid': ssid},
            'connection': {'autoconnect': False,
                           'id': conn_name,
                           'interface-name': 'wlan0',
                           'type': '802-11-wireless',
                           'uuid': str(uuid.uuid4())},
            'ipv4': {'address-data': 
                        [{'address': '192.168.42.1', 'prefix': 24}],
                     'addresses': [['192.168.42.1', 24, '0.0.0.0']],
                     'method': 'manual'},
            'ipv6': {'method': 'auto'}
        }

#debugrob: is this realy a generic ENTERPRISE config, need another?
#debugrob: how do we handle connecting to a captured portal?

        # This is what we use for "MIT SECURE" network.
        enterprise_dict = {
            '802-11-wireless': {'mode': 'infrastructure',
                                'security': '802-11-wireless-security',
                                'ssid': ssid},
            '802-11-wireless-security': 
                {'auth-alg': 'open', 'key-mgmt': 'wpa-eap'},
            '802-1x': {'eap': ['peap'],
                       'identity': username,
                       'password': password,
                       'phase2-auth': 'mschapv2'},
            'connection': {'id': conn_name,
                           'type': '802-11-wireless',
                           'uuid': str(uuid.uuid4())},
            'ipv4': {'method': 'auto'},
            'ipv6': {'method': 'auto'}
        }

        # No auth, 'open' connection.
        none_dict = {
            '802-11-wireless': {'mode': 'infrastructure',
                                'ssid': ssid},
            'connection': {'id': conn_name,
                           'type': '802-11-wireless',
                           'uuid': str(uuid.uuid4())},
            'ipv4': {'method': 'auto'},
            'ipv6': {'method': 'auto'}
        }

        # Hidden, WEP, WPA, WPA2, password required.
        passwd_dict = {
            '802-11-wireless': {'mode': 'infrastructure',
                                'security': '802-11-wireless-security',
                                'ssid': ssid},
            '802-11-wireless-security': 
                {'key-mgmt': 'wpa-psk', 'psk': password},
            'connection': {'id': conn_name,
                        'type': '802-11-wireless',
                        'uuid': str(uuid.uuid4())},
            'ipv4': {'method': 'auto'},
            'ipv6': {'method': 'auto'}
        }

        conn_dict = None
        conn_str = ''
        if conn_type == CONN_TYPE_HOTSPOT:
            conn_dict = hotspot_dict
            conn_str = 'HOTSPOT'

        if conn_type == CONN_TYPE_SEC_NONE:
            conn_dict = none_dict 
            conn_str = 'OPEN'

        if conn_type == CONN_TYPE_SEC_PASSWORD:
            conn_dict = passwd_dict 
            conn_str = 'WEP/WPA/WPA2'

        if conn_type == CONN_TYPE_SEC_ENTERPRISE:
            conn_dict = enterprise_dict 
            conn_str = 'ENTERPRISE'

        if conn_dict is None:
            print(f'connect_to_AP() Error: Invalid conn_type="{conn_type}"')
            return False

        #print(f"new connection {conn_dict} type={conn_str}")

        # Delete any stale profile with the same name before adding a new one.
        # This prevents duplicate profiles from accumulating across setup runs
        # and ensures credentials are always fresh from the explicit submit.
        if conn_type != CONN_TYPE_HOTSPOT:
            stop_connection(conn_name)

        NetworkManager.Settings.AddConnection(conn_dict)
        print(f"Added connection {conn_name} of type {conn_str}")

        # Now find this connection and its device
        connections = NetworkManager.Settings.ListConnections()
        connections = dict([(x.GetSettings()['connection']['id'], x) for x in connections])
        conn = connections[conn_name]

        # Find a suitable device
        ctype = conn.GetSettings()['connection']['type']
        dtype = {'802-11-wireless': NetworkManager.NM_DEVICE_TYPE_WIFI}.get(ctype,ctype)
        devices = NetworkManager.NetworkManager.GetDevices()

        for dev in devices:
            if dev.DeviceType == dtype:
                break
        else:
            print(f"connect_to_AP() Error: No suitable and available {ctype} device found.")
            return False

        # And connect
        NetworkManager.NetworkManager.ActivateConnection(conn, dev, "/")
        print(f"Activated connection={conn_name}.")

        # Wait for ADDRCONF(NETDEV_CHANGE): wlan0: link becomes ready
        print(f'Waiting for connection to become active...')
        elapsed = 0
        poll_interval = 0.25  # Poll every 250ms for responsiveness
        timeout = 10          # Max wait 10s for connection to stabilize
        while dev.State != NetworkManager.NM_DEVICE_STATE_ACTIVATED and elapsed < timeout:
            if dev.State == 120:  # NM_DEVICE_STATE_FAILED
                print(f"Connection failed (state={dev.State})")
                break
            time.sleep(poll_interval)
            elapsed += poll_interval

        if dev.State == NetworkManager.NM_DEVICE_STATE_ACTIVATED:
            print(f'Connection {conn_name} is live.')
            return True

    except Exception as e:
        print(f'Connection error {e}')

    print(f'Connection {conn_name} failed.')
    return False




