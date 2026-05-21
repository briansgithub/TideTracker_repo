# Start a local hotspot using NetworkManager.

import NetworkManager
import uuid, os, sys, time, socket, json, threading

# This is needed to work with NetworkManager 1.30.6 and python-networkmanager 2.2      
from dbus.mainloop.glib import DBusGMainLoop
DBusGMainLoop(set_as_default = True)

# Global lock to synchronize NetworkManager DBus calls across threads.
NM_LOCK = threading.Lock()

# Global Debug Flag
DEBUG_MODE = False

def dprint(*args, **kwargs):
    if DEBUG_MODE:
        if 'flush' not in kwargs:
            kwargs['flush'] = True
        print(*args, **kwargs)

HOTSPOT_CONNECTION_NAME = 'hotspot'
GENERIC_CONNECTION_NAME = 'python-wifi-connect'


#------------------------------------------------------------------------------
# Persistent data path for saved WiFi credentials.
PERSISTENT_DATA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))),
    'tidetracker_persistent_data.json'
)


#------------------------------------------------------------------------------
# Save the successful credentials to a persistent file.
def save_last_successful_credentials(ssid, password=None, username=None, conn_type=None):
    data = {}
    if os.path.exists(PERSISTENT_DATA_PATH):
        try:
            with open(PERSISTENT_DATA_PATH, 'r') as f:
                data = json.load(f)
        except Exception:
            pass
    
    data['wifi_ssid'] = ssid
    data['wifi_password'] = password
    data['wifi_username'] = username
    data['wifi_conn_type'] = conn_type
    
    try:
        with open(PERSISTENT_DATA_PATH, 'w') as f:
            json.dump(data, f)
        print(f'Saved successful WiFi credentials for "{ssid}" to {PERSISTENT_DATA_PATH}')
    except Exception as e:
        print(f'Error saving credentials: {e}')


#------------------------------------------------------------------------------
# Load the last successful credentials from the persistent file.
def load_last_successful_credentials():
    if os.path.exists(PERSISTENT_DATA_PATH):
        try:
            with open(PERSISTENT_DATA_PATH, 'r') as f:
                data = json.load(f)
            return {
                'ssid': data.get('wifi_ssid'),
                'password': data.get('wifi_password'),
                'username': data.get('wifi_username'),
                'conn_type': data.get('wifi_conn_type')
            }
        except Exception:
            pass
    return None


#------------------------------------------------------------------------------
# Attempt to reconnect to the last known working WiFi.
def reconnect_to_last_wifi():
    # Optimization: If we already have internet, don't do anything.
    if have_active_internet_connection():
        dprint("Already have internet connection. Skipping redundant fallback.")
        return True

    creds = load_last_successful_credentials()
    if creds and creds.get('ssid'):
        # Check if we are already associated with this SSID
        current_ssid = get_connected_ssid()
        if current_ssid == creds['ssid']:
            print(f"Associated with '{current_ssid}' but no internet. Re-initializing connection...", flush=True)

        print(f'Attempting to reconnect to last working WiFi: {creds["ssid"]}', flush=True)
        return connect_to_AP(
            conn_type=creds.get('conn_type', CONN_TYPE_SEC_NONE),
            ssid=creds.get('ssid'),
            username=creds.get('username'),
            password=creds.get('password')
        )
    else:
        dprint('No saved WiFi credentials found for reconnection.')
        return False


#------------------------------------------------------------------------------
# Try to capture and save the currently active WiFi connection's credentials.
def capture_and_save_current_connection():
    try:
        with NM_LOCK:
            active_connections = list(NetworkManager.NetworkManager.ActiveConnections)
            for active in active_connections:
                try:
                    settings = active.Connection.GetSettings()
                    conn_settings = settings.get('connection', {})
                    if conn_settings.get('type') != '802-11-wireless':
                        continue
                    
                    wifi_settings = settings.get('802-11-wireless', {})
                    if wifi_settings.get('mode') != 'infrastructure':
                        continue
                        
                    ssid = wifi_settings.get('ssid')
                    if not ssid:
                        continue
                    
                    # Convert byte array/list to string if necessary
                    if isinstance(ssid, (bytes, bytearray)):
                        ssid = ssid.decode('utf-8', errors='replace')
                    elif isinstance(ssid, list):
                        ssid = "".join(map(chr, ssid))
                        
                    try:
                        secrets = active.Connection.GetSecrets('802-11-wireless-security')
                        wifi_sec = secrets.get('802-11-wireless-security', {})
                    except Exception:
                        wifi_sec = settings.get('802-11-wireless-security', {})

                    password = wifi_sec.get('psk')
                    
                    conn_type = CONN_TYPE_SEC_NONE
                    if password:
                        conn_type = CONN_TYPE_SEC_PASSWORD
                    
                    if '802-1x' in settings:
                        conn_type = CONN_TYPE_SEC_ENTERPRISE
                        try:
                            ex_secrets = active.Connection.GetSecrets('802-1x')
                            password = ex_secrets.get('802-1x', {}).get('password', password)
                        except:
                            pass
                    
                    if have_active_internet_connection():
                        dprint(f"Auto-saving currently active connection '{ssid}' as fallback.")
                        save_last_successful_credentials(
                            ssid=ssid,
                            password=password,
                            username=settings.get('802-1x', {}).get('identity'),
                            conn_type=conn_type
                        )
                        return True
                except Exception:
                    pass
    except Exception as e:
        dprint(f"Error capturing current connection: {e}")
    return False


#------------------------------------------------------------------------------
# Returns True if we are connected to the internet, False otherwise.
def have_active_internet_connection(host="8.8.8.8", port=53, timeout=3, retries=2):
   """
   Robust internet check with retries for slow hardware.
   """
   for i in range(retries + 1):
       try:
         s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
         s.settimeout(timeout)
         s.connect((host, port))
         s.close()
         return True
       except Exception:
         if i < retries:
             time.sleep(2) # wait 2s before retry
         continue
   return False


#------------------------------------------------------------------------------
# Returns the SSID of the currently active WiFi client connection, or None.
def get_connected_ssid():
    try:
        with NM_LOCK:
            active_connections = list(NetworkManager.NetworkManager.ActiveConnections)
            for active in active_connections:
                try:
                    settings = active.Connection.GetSettings()
                    conn_type = settings.get('connection', {}).get('type', '')
                    wifi_settings = settings.get('802-11-wireless', {})
                    mode = wifi_settings.get('mode', '')
                    if conn_type == '802-11-wireless' and mode == 'infrastructure':
                        ssid = wifi_settings.get('ssid', None)
                        if ssid:
                            if isinstance(ssid, (bytes, bytearray)):
                                return ssid.decode('utf-8', errors='replace')
                            elif isinstance(ssid, list):
                                return "".join(map(chr, ssid))
                            return str(ssid)
                except Exception:
                    pass
    except Exception as e:
        dprint(f'get_connected_ssid() error: {e}')
    return None


#------------------------------------------------------------------------------
# Remove ALL wifi connections - to start clean or before running the hotspot.
def delete_all_wifi_connections():
    with NM_LOCK:
        connections = NetworkManager.Settings.ListConnections()
        for connection in connections:
            if connection.GetSettings()["connection"]["type"] == "802-11-wireless":
                print("Deleting connection " + connection.GetSettings()["connection"]["id"])
                connection.Delete()
    time.sleep(2)


#------------------------------------------------------------------------------
# Stop and delete the hotspot.
def stop_hotspot():
    return stop_connection(HOTSPOT_CONNECTION_NAME)


#------------------------------------------------------------------------------
# Generic connection stopper / deleter.
def stop_connection(conn_name=GENERIC_CONNECTION_NAME):
    found_any = False
    dprint(f"DEBUG: stop_connection('{conn_name}') starting...")

    with NM_LOCK:
        try:
            active_conns = list(NetworkManager.NetworkManager.ActiveConnections)
            for active in active_conns:
                try:
                    settings = active.Connection.GetSettings()
                    if settings['connection']['id'] == conn_name:
                        print(f'Deactivating active connection: {conn_name}', flush=True)
                        NetworkManager.NetworkManager.DeactivateConnection(active)
                        found_any = True
                except Exception:
                    pass
        except Exception as e:
            dprint(f'Error listing active connections: {e}')

        try:
            all_conns = list(NetworkManager.Settings.ListConnections())
            for conn in all_conns:
                try:
                    settings = conn.GetSettings()
                    if settings['connection']['id'] == conn_name:
                        print(f'Deleting connection profile: {conn_name}', flush=True)
                        conn.Delete()
                        found_any = True
                except Exception:
                    pass
        except Exception as e:
            dprint(f'Error listing connections: {e}')

    if found_any:
        dprint(f"DEBUG: stop_connection('{conn_name}') waiting for device ready...")
        _wait_for_wifi_device_ready()

    dprint(f"DEBUG: stop_connection('{conn_name}') finished.")
    return found_any


#------------------------------------------------------------------------------
# Return a list of available SSIDs and their security type.
def get_list_of_access_points():
    NM_SECURITY_NONE       = 0x0
    NM_SECURITY_WEP        = 0x1
    NM_SECURITY_WPA        = 0x2
    NM_SECURITY_WPA2       = 0x4
    NM_SECURITY_ENTERPRISE = 0x8
   
    ssids = []
    try:
        with NM_LOCK:
            devices = list(NetworkManager.NetworkManager.GetDevices())
            for dev in devices:
                if dev.DeviceType != NetworkManager.NM_DEVICE_TYPE_WIFI:
                    continue
                
                aps = dev.GetAccessPoints()
                for ap in aps:
                    security = NM_SECURITY_NONE
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

                    security_str = 'NONE'
                    if security & NM_SECURITY_WEP: security_str = 'WEP'
                    if security & NM_SECURITY_WPA: security_str = 'WPA'
                    if security & NM_SECURITY_WPA2: security_str = 'WPA2'
                    if security & NM_SECURITY_ENTERPRISE: security_str = 'ENTERPRISE'

                    raw_ssid = ap.Ssid
                    if isinstance(raw_ssid, (bytes, bytearray)):
                        raw_ssid = raw_ssid.decode('utf-8', errors='replace')
                    elif isinstance(raw_ssid, list):
                        raw_ssid = "".join(map(chr, raw_ssid))

                    entry = {"ssid": raw_ssid, "security": security_str}
                    if ssids.__contains__(entry): continue
                    if raw_ssid.startswith('Rpi-'+os.uname()[1]): continue
                    ssids.append(entry)
    except Exception as e:
        dprint(f'Error getting access points: {e}')

    ssids.append({"ssid": "Enter a hidden WiFi name", "security": "HIDDEN"})
    return ssids


#------------------------------------------------------------------------------
# Get hotspot SSID name.
def get_hotspot_SSID():
    return os.uname()[1]


#------------------------------------------------------------------------------
# Wait for the wifi device to reach a ready state.
def _wait_for_wifi_device_ready(timeout=10):
    READY_STATES = (
        NetworkManager.NM_DEVICE_STATE_DISCONNECTED,
        NetworkManager.NM_DEVICE_STATE_UNKNOWN,
        NetworkManager.NM_DEVICE_STATE_UNMANAGED,
        NetworkManager.NM_DEVICE_STATE_UNAVAILABLE,
    )
    try:
        with NM_LOCK:
            devices = list(NetworkManager.NetworkManager.GetDevices())
            
        for dev in devices:
            if dev.DeviceType == NetworkManager.NM_DEVICE_TYPE_WIFI:
                elapsed = 0
                poll_interval = 0.25
                while dev.State not in READY_STATES and elapsed < timeout:
                    if dev.State == 120:
                        try:
                            with NM_LOCK: dev.Disconnect()
                        except: pass
                    time.sleep(poll_interval)
                    elapsed += poll_interval
                if elapsed > 0: time.sleep(0.5)
                return
    except Exception as e:
        dprint(f'Error waiting for wifi device: {e}')


#------------------------------------------------------------------------------
# Start a local hotspot.
def start_hotspot():
    stop_hotspot()
    _wait_for_wifi_device_ready()
    return connect_to_AP(CONN_TYPE_HOTSPOT, HOTSPOT_CONNECTION_NAME, get_hotspot_SSID())


#------------------------------------------------------------------------------
# Supported connection types.
CONN_TYPE_HOTSPOT        = 'hotspot'
CONN_TYPE_SEC_NONE       = 'NONE'
CONN_TYPE_SEC_PASSWORD   = 'PASSWORD'
CONN_TYPE_SEC_ENTERPRISE = 'ENTERPRISE'


#------------------------------------------------------------------------------
# Generic connect to the user selected AP function.
def connect_to_AP(conn_type=None, conn_name=GENERIC_CONNECTION_NAME, ssid=None, username=None, password=None):
    if conn_type is None or ssid is None:
        print(f'connect_to_AP() Error: Missing args conn_type or ssid')
        return False

    try:
        hotspot_dict = {
            '802-11-wireless': {'band': 'bg', 'mode': 'ap', 'ssid': ssid},
            'connection': {'autoconnect': False, 'id': conn_name, 'interface-name': 'wlan0', 'type': '802-11-wireless', 'uuid': str(uuid.uuid4())},
            'ipv4': {'address-data': [{'address': '192.168.42.1', 'prefix': 24}], 'addresses': [['192.168.42.1', 24, '0.0.0.0']], 'method': 'manual'},
            'ipv6': {'method': 'auto'}
        }
        enterprise_dict = {
            '802-11-wireless': {'mode': 'infrastructure', 'security': '802-11-wireless-security', 'ssid': ssid},
            '802-11-wireless-security': {'auth-alg': 'open', 'key-mgmt': 'wpa-eap'},
            '802-1x': {'eap': ['peap'], 'identity': username, 'password': password, 'phase2-auth': 'mschapv2'},
            'connection': {'id': conn_name, 'type': '802-11-wireless', 'uuid': str(uuid.uuid4())},
            'ipv4': {'method': 'auto'}, 'ipv6': {'method': 'auto'}
        }
        none_dict = {
            '802-11-wireless': {'mode': 'infrastructure', 'ssid': ssid},
            'connection': {'id': conn_name, 'type': '802-11-wireless', 'uuid': str(uuid.uuid4())},
            'ipv4': {'method': 'auto'}, 'ipv6': {'method': 'auto'}
        }
        passwd_dict = {
            '802-11-wireless': {'mode': 'infrastructure', 'security': '802-11-wireless-security', 'ssid': ssid},
            '802-11-wireless-security': {'key-mgmt': 'wpa-psk', 'psk': password},
            'connection': {'id': conn_name, 'type': '802-11-wireless', 'uuid': str(uuid.uuid4())},
            'ipv4': {'method': 'auto'}, 'ipv6': {'method': 'auto'}
        }

        conn_dict = None
        if conn_type == CONN_TYPE_HOTSPOT: conn_dict = hotspot_dict
        if conn_type == CONN_TYPE_SEC_NONE: conn_dict = none_dict 
        if conn_type == CONN_TYPE_SEC_PASSWORD: conn_dict = passwd_dict 
        if conn_type == CONN_TYPE_SEC_ENTERPRISE: conn_dict = enterprise_dict 

        if conn_dict is None: return False

        if conn_type != CONN_TYPE_HOTSPOT:
            stop_connection(conn_name)

        with NM_LOCK:
            NetworkManager.Settings.AddConnection(conn_dict)
            connections = NetworkManager.Settings.ListConnections()
            connections = dict([(x.GetSettings()['connection']['id'], x) for x in connections])
            conn = connections[conn_name]
            devices = list(NetworkManager.NetworkManager.GetDevices())

        for dev in devices:
            if dev.DeviceType == NetworkManager.NM_DEVICE_TYPE_WIFI:
                break
        else:
            return False

        with NM_LOCK:
            NetworkManager.NetworkManager.ActivateConnection(conn, dev, "/")

        elapsed = 0
        poll_interval = 0.25
        timeout = 15
        while elapsed < timeout:
            with NM_LOCK: state = dev.State
            if state == NetworkManager.NM_DEVICE_STATE_ACTIVATED: return True
            if state == 120: break
            time.sleep(poll_interval)
            elapsed += poll_interval

        try:
            with NM_LOCK: dev.Disconnect()
        except: pass

    except Exception as e:
        print(f'Connection error: {e}')

    return False
