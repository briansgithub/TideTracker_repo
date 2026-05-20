# Our main wifi-connect application, which is based around an HTTP server.

import os, getopt, sys, json, atexit, subprocess, time, threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import parse_qs
from io import BytesIO

# Local modules
import netman
import dnsmasq

# Defaults
ADDRESS = '0.0.0.0'
GATEWAY_ADDRESS = '192.168.42.1'
PORT = 80
UI_PATH = '../ui'


#------------------------------------------------------------------------------
# called at exit
def cleanup():
    print("Cleaning up prior to exit.")
    dnsmasq.stop()
    netman.stop_hotspot()


#------------------------------------------------------------------------------
# Kill any previous http_server / dnsmasq processes from a prior run.
# This ensures a completely fresh setup every time, even if the previous
# instance was launched with sudo.
def kill_previous_setup_processes(port=80):
    killed_something = False

    # --- 1. Kill anything bound to our HTTP port (e.g. a stale http_server) ---
    #     fuser -k sends SIGKILL to every process using the port.
    try:
        result = subprocess.run(
            ['fuser', '-k', f'{port}/tcp'],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            print(f'Killed process(es) on port {port} via fuser')
            killed_something = True
    except FileNotFoundError:
        # fuser not installed — fall back to lsof
        try:
            result = subprocess.run(
                ['lsof', '-ti', f':{port}'],
                capture_output=True, text=True
            )
            pids = result.stdout.strip().split('\n')
            for pid in pids:
                pid = pid.strip()
                if pid and pid != str(os.getpid()):
                    subprocess.run(['kill', '-9', pid], capture_output=True)
                    print(f'Killed PID {pid} on port {port} via lsof')
                    killed_something = True
        except Exception as e:
            print(f'Note: lsof fallback failed: {e}')
    except Exception as e:
        print(f'Note: fuser failed: {e}')

    # --- 2. Belt-and-suspenders: kill any lingering http_server.py process ---
    #     Catches cases where the process exists but somehow isn't bound yet.
    #     We use pgrep + manual kill (not pkill) to exclude our own PID.
    try:
        result = subprocess.run(
            ['pgrep', '-f', 'http_server.py'],
            capture_output=True, text=True
        )
        for pid in result.stdout.strip().split('\n'):
            pid = pid.strip()
            if pid and pid != str(os.getpid()):
                subprocess.run(['kill', '-9', pid], capture_output=True)
                print(f'Killed lingering http_server.py process PID {pid}')
                killed_something = True
    except Exception:
        pass

    # --- 3. Kill any lingering dnsmasq from a previous run ---
    dnsmasq.stop()

    if killed_something:
        time.sleep(1)  # Give OS time to release sockets


#------------------------------------------------------------------------------
# Consistently launch the hotspot, scan for networks, and update shared state.
def launch_ap_sequence(ssids_list, status_dict):
    """
    Modularized sequence to ensure the AP is launched identically every time.
    1. Print the 'Waiting...' message.
    2. Stop existing hotspot/dnsmasq.
    3. Scan for available SSIDs (Must be done while in client mode).
    4. Snapshot current connection status.
    5. Start Hotspot and DNS services.
    """
    print(f'\n\033[91mWaiting for a connection to our hotspot {netman.get_hotspot_SSID()} ...\033[0m')
    
    # Ensure a clean slate
    dnsmasq.stop()
    netman.seop_hotspot()
    
    # Wait for hardware to settle before scanning
    time.sleep(1)
    
    # Refresh SSID list
    print("DEBUG: Scanning for available WiFi networks...")
    ssids_list.clear()
    ssids_list.extend(netman.get_list_of_access_points())
    
    # Refresh status snapshot
    print("DEBUG: Capturing connection status snapshot...")
    status_dict.update({
        'ssid': netman.get_connected_ssid(),
        'has_internet': netman.have_active_internet_connection(),
        'testing': False
    })
    
    # Start the hotspot
    if not netman.start_hotspot():
        print('CRITICAL: Error starting hotspot!')
        return False
        
    # Start dnsmasq
    dnsmasq.start()
    print("DEBUG: AP relaunch sequence complete.\n")
    return True


#------------------------------------------------------------------------------
# A custom http server class in which we can set the default path it serves
# when it gets a GET request.
class MyHTTPServer(HTTPServer):
    allow_reuse_address = True  # Prevent "Address already in use" errors

    def __init__(self, base_path, server_address, RequestHandlerClass):
        self.base_path = base_path
        HTTPServer.__init__(self, server_address, RequestHandlerClass)


#------------------------------------------------------------------------------
# A custom http request handler class factory.
# Handle the GET and POST requests from the UI form and JS.
# The class factory allows us to pass custom arguments to the handler.
def RequestHandlerClassFactory(address, ssids, rcode, pre_status=None):

    class MyHTTPReqHandler(SimpleHTTPRequestHandler):

        def __init__(self, *args, **kwargs):
            # We must set our custom class properties first, since __init__() of
            # our super class will call do_GET().
            self.address = address
            self.ssids = ssids
            self.rcode = rcode
            self.pre_status = pre_status  # Snapshot taken BEFORE hotspot started
            super(MyHTTPReqHandler, self).__init__(*args, **kwargs)

        # See if this is a specific request, otherwise let the server handle it.
        def do_GET(self):

            print(f'DEBUG: do_GET request path: {self.path}')

            # Handle the hotspot starting and a computer connecting to it,
            # we have to return a redirect to the gateway to get the 
            # captured portal to show up.
            if '/hotspot-detect.html' == self.path:
                self.send_response(301) # redirect
                new_path = f'http://{GATEWAY_ADDRESS}/'
                print(f'redirecting to {new_path}')
                self.send_header('Location', new_path)
                self.end_headers()

            if '/generate_204' == self.path:
                self.send_response(301) # redirect
                new_path = f'http://{GATEWAY_ADDRESS}/'
                print(f'redirecting to {new_path}')
                self.send_header('Location', new_path)
                self.end_headers()

            # Handle a REST API request to return the device registration code
            if '/regcode' == self.path:
                self.send_response(200)
                self.end_headers()
                response = BytesIO()
                response.write(self.rcode.encode('utf-8'))
                print(f'GET {self.path} returning: {response.getvalue()}')
                self.wfile.write(response.getvalue())
                return

            # Handle a REST API request to return the list of SSIDs
            if '/networks' == self.path:
                print(f'DEBUG: Handling /networks request')
                self.send_response(200)
                self.end_headers()
                response = BytesIO()
                ssids = self.ssids # passed in to the class factory
                print(f'DEBUG: Serving {len(ssids)} SSIDs: {ssids}')
                """ map whatever we get from net man to our constants:
                Security:
                    NONE         
                    HIDDEN         
                    WEP         
                    WPA        
                    WPA2      
                    ENTERPRISE
                Required user input (from UI form):
                    NONE                   - No input requried.
                    HIDDEN, WEP, WPA, WPA2 - Need password.
                    ENTERPRISE             - Need username and password.
                """
                response.write(json.dumps(ssids).encode('utf-8'))
                print(f'GET {self.path} returning: {response.getvalue()}')
                self.wfile.write(response.getvalue())
                return

            # Handle a REST API request for current connection status.
            # We serve the pre-captured snapshot taken BEFORE the hotspot
            # was started, since the radio is now in AP mode and can no
            # longer report a client connection.
            if '/status' == self.path:
                self.send_response(200)
                self.end_headers()
                response = BytesIO()
                status = self.pre_status if self.pre_status else {'ssid': None, 'has_internet': False}
                response.write(json.dumps(status).encode('utf-8'))
                print(f'GET /status returning: {status}')
                self.wfile.write(response.getvalue())
                return

            # Not sure if this is just OSX hitting the captured portal,
            # but we need to exit if we get it.
            if '/bag' == self.path:
                sys.exit()

            # All other requests are handled by the server which vends files 
            # from the ui_path we were initialized with.
            super().do_GET()


        def do_POST(self):
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            print(f'DEBUG: do_POST request path: {self.path}')
            print(f'DEBUG: do_POST body: {body.decode("utf-8")}')
            
            self.send_response(200)
            self.end_headers()
            response = BytesIO()
            fields = parse_qs(body.decode('utf-8'))

            persistent_data_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))),
                'tidetracker_persistent_data.json'
            )

            # ----------------------------------------------------------
            # /update_station — Save NOAA station ID (merge with existing data)
            # ----------------------------------------------------------
            if self.path == '/update_station':
                print(f'DEBUG: Handling /update_station')
                FORM_STATION = 'station'
                if FORM_STATION in fields:
                    station_id = fields[FORM_STATION][0]
                    print(f'DEBUG: Received station_id: {station_id}')
                    # Read existing data and merge
                    existing_data = {}
                    if os.path.exists(persistent_data_path):
                        try:
                            with open(persistent_data_path, 'r') as f:
                                existing_data = json.load(f)
                            print(f'DEBUG: Loaded existing data: {existing_data}')
                        except Exception as e:
                            print(f'DEBUG: Error loading existing data: {e}')
                    existing_data['station_id'] = station_id
                    try:
                        with open(persistent_data_path, 'w') as json_file:
                            json.dump(existing_data, json_file)
                        print(f"DEBUG: Station ID ({station_id}) saved successfully to {persistent_data_path}")
                    except Exception as e:
                        print(f'DEBUG: Error saving station_id: {e}')
                    print(f"\nStation ID ({station_id}) has been saved to {persistent_data_path}\n")
                    response.write(b'OK\n')
                    self.wfile.write(response.getvalue())

                    # Requirement: relaunch hotspot after station update
                    def _relaunch_hotspot():
                        launch_ap_sequence(self.ssids, self.pre_status)
                    
                    threading.Thread(target=_relaunch_hotspot, daemon=True).start()
                    return
                else:
                    print(f'DEBUG: Error - Missing station in fields: {fields}')
                    response.write(b'ERROR: Missing station\n')
                self.wfile.write(response.getvalue())
                return

            # ----------------------------------------------------------
            # /connect — Save WiFi credentials (does NOT stop the hotspot)
            # ----------------------------------------------------------
            if self.path == '/connect':
                print(f'DEBUG: Handling /connect')
                FORM_SSID = 'ssid'
                FORM_HIDDEN_SSID = 'hidden-ssid'
                FORM_USERNAME = 'identity'
                FORM_PASSWORD = 'passphrase'

                if FORM_SSID not in fields:
                    print(f'DEBUG: Error - POST /connect is missing {FORM_SSID} field. Fields: {fields}')
                    response.write(b'ERROR: Missing ssid\n')
                    self.wfile.write(response.getvalue())
                    return

                ssid = fields[FORM_SSID][0]
                print(f'DEBUG: SSID from form: {ssid}')
                password = None
                username = None

                if FORM_HIDDEN_SSID in fields:
                    ssid = fields[FORM_HIDDEN_SSID][0]
                if FORM_USERNAME in fields:
                    username = fields[FORM_USERNAME][0]
                if FORM_PASSWORD in fields:
                    password = fields[FORM_PASSWORD][0]

                # Determine connection type from scanned SSIDs
                conn_type = netman.CONN_TYPE_SEC_NONE
                if FORM_HIDDEN_SSID in fields:
                    conn_type = netman.CONN_TYPE_SEC_PASSWORD

                for s in self.ssids:
                    if FORM_SSID in s and ssid == s[FORM_SSID]:
                        if s['security'] == "ENTERPRISE":
                            conn_type = netman.CONN_TYPE_SEC_ENTERPRISE
                        elif s['security'] == "NONE":
                            conn_type = netman.CONN_TYPE_SEC_NONE
                        else:
                            conn_type = netman.CONN_TYPE_SEC_PASSWORD
                        break

                # Read existing data and merge
                existing_data = {}
                if os.path.exists(persistent_data_path):
                    try:
                        with open(persistent_data_path, 'r') as f:
                            existing_data = json.load(f)
                        print(f'DEBUG: Loaded existing data for connect: {existing_data}')
                    except Exception as e:
                        print(f'DEBUG: Error loading existing data for connect: {e}')
                existing_data['wifi_ssid'] = ssid
                existing_data['wifi_password'] = password
                existing_data['wifi_username'] = username
                existing_data['wifi_conn_type'] = conn_type
                
                try:
                    with open(persistent_data_path, 'w') as json_file:
                        json.dump(existing_data, json_file)
                    print(f"DEBUG: WiFi credentials for '{ssid}' saved successfully to {persistent_data_path}")
                except Exception as e:
                    print(f'DEBUG: Error saving WiFi credentials: {e}')

                print(f"\nWiFi credentials for '{ssid}' saved to {persistent_data_path}\n")

                # Mark status as 'testing' immediately so the UI can update
                pre_status.update({'ssid': ssid, 'has_internet': None, 'testing': True})

                # Respond to the client before the hotspot is torn down
                response.write(b'TESTING\n')
                self.wfile.write(response.getvalue())

                # Run the connection test in the background so the HTTP response
                # is delivered before the hotspot goes down.
                def _run_wifi_test():
                    print(f"\n[WiFi Test] Stopping hotspot and dnsmasq to test credentials for '{ssid}'...")
                    dnsmasq.stop()
                    netman.stop_hotspot()

                    # Attempt to connect with the submitted credentials
                    connected = netman.connect_to_AP(
                        conn_type=conn_type, ssid=ssid,
                        username=username, password=password
                    )

                    has_internet = False
                    if connected:
                        has_internet = netman.have_active_internet_connection()
                        print(f"[WiFi Test] Connected. Has internet: {has_internet}")
                        
                        if has_internet:
                            print(f"\033[92m[WiFi Test] Internet connection successful! Keeping connection active and NOT restarting hotspot.\033[0m")
                            # Update the shared status dict before returning
                            pre_status.update({'ssid': ssid, 'has_internet': True, 'testing': False})
                            # Force the entire process to exit so control returns to the terminal/boot script
                            os._exit(0)
                            
                        # If we have no internet, tear down the test connection so we can restart the hotspot
                        print(f"[WiFi Test] Connection successful but NO INTERNET. Tearing down...")
                        netman.stop_connection(netman.GENERIC_CONNECTION_NAME)
                    else:
                        print(f"[WiFi Test] Could not connect to '{ssid}'. Tearing down failing connection...")
                        netman.stop_connection(netman.GENERIC_CONNECTION_NAME)

                    # Restart the hotspot using the unified sequence
                    launch_ap_sequence(self.ssids, self.pre_status)
                    print(f"[WiFi Test] Done. Status: {pre_status}")

                threading.Thread(target=_run_wifi_test, daemon=True).start()
                return

            # ----------------------------------------------------------
            # /exit — Stop hotspot, connect to saved WiFi, re-launch AP on failure
            # ----------------------------------------------------------
            if self.path == '/exit':
                # Read saved WiFi credentials
                saved_data = {}
                if os.path.exists(persistent_data_path):
                    try:
                        with open(persistent_data_path, 'r') as f:
                            saved_data = json.load(f)
                    except Exception:
                        pass

                ssid = saved_data.get('wifi_ssid')
                password = saved_data.get('wifi_password')
                username = saved_data.get('wifi_username')
                conn_type = saved_data.get('wifi_conn_type', netman.CONN_TYPE_SEC_NONE)

                # Send response before tearing down the hotspot (client will disconnect)
                response.write(b'OK\n')
                self.wfile.write(response.getvalue())

                if ssid:
                    print(f"\nExiting setup: stopping hotspot and connecting to '{ssid}'...")

                    # Stop the hotspot
                    netman.stop_hotspot()

                    # Attempt to connect with saved credentials
                    success = netman.connect_to_AP(conn_type=conn_type, ssid=ssid,
                            username=username, password=password)

                    if success:
                        print(f'Connected to {ssid}! Exiting setup.')
                        sys.exit()
                    else:
                        print(f'Connection to {ssid} failed, restarting the hotspot using unified sequence.')
                        launch_ap_sequence(self.ssids, self.pre_status)
                else:
                    print(f"\nNo saved WiFi credentials found. Keeping hotspot active.")
                    response.write(b'No WiFi credentials saved\n')

                return

    return  MyHTTPReqHandler # the class our factory just created.


#------------------------------------------------------------------------------
# Create the hotspot, start dnsmasq, start the HTTP server.
def main(address, port, ui_path, rcode, delete_connections, force_setup):

    # Kill any lingering http_server, dnsmasq, or other setup processes from a
    # previous run.  This is the direct fix for "Address already in use" errors.
    kill_previous_setup_processes(port)

    # See if caller wants to delete all existing connections first
    if delete_connections:
        netman.delete_all_wifi_connections()

    # Check if we are already connected, if so we are done.
    if not force_setup and netman.have_active_internet_connection():
        print('Already connected to the internet, nothing to do, exiting.')
        sys.exit()

    # Shared mutable containers for the request handler
    ssids = []
    pre_status = {}

    # Launch the hotspot for the first time using the unified sequence
    if not launch_ap_sequence(ssids, pre_status):
        sys.exit(1)

    # Find the ui directory which is up one from where this file is located.
    web_dir = os.path.join(os.path.dirname(__file__), ui_path)
    print(f'HTTP serving directory: {web_dir} on {address}:{port}')

    # Change to this directory so the HTTPServer returns the index.html in it 
    # by default when it gets a GET.
    os.chdir(web_dir)

    # Host:Port our HTTP server listens on
    server_address = (address, port)

    # Custom request handler class (so we can pass in our own args)
    MyRequestHandlerClass = RequestHandlerClassFactory(address, ssids, rcode, pre_status)

    # Start an HTTP server to serve the content in the ui dir and handle the 
    # POST request in the handler class.
    httpd = MyHTTPServer(web_dir, server_address, MyRequestHandlerClass)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        dnsmasq.stop()
        netman.stop_hotspot()
        httpd.server_close()


#------------------------------------------------------------------------------
# Util to convert a string to an int, or provide a default.
def string_to_int(s, default):
    try:
        return int(s)
    except ValueError:
        return default


#------------------------------------------------------------------------------
# Entry point and command line argument processing.
if __name__ == "__main__":
    atexit.register(cleanup)

    address = ADDRESS
    port = PORT
    ui_path = UI_PATH
    delete_connections = False
    force_setup = False
    rcode = ''

    usage = ''\
f'Command line args: \n'\
f'  -a <HTTP server address>     Default: {address} \n'\
f'  -p <HTTP server port>        Default: {port} \n'\
f'  -u <UI directory to serve>   Default: "{ui_path}" \n'\
f'  -d Delete Connections First  Default: {delete_connections} \n'\
f'  -f Force Setup Mode          Default: {force_setup} \n'\
f'  -r Device Registration Code  Default: "" \n'\
f'  -h Show help.\n'

    try:
        opts, args = getopt.getopt(sys.argv[1:], "a:p:u:r:dfh")
    except getopt.GetoptError:
        print(usage)
        sys.exit(2)

    for opt, arg in opts:
        if opt == '-h':
            print(usage)
            sys.exit()

        elif opt in ("-d"):
           delete_connections = True

        elif opt in ("-f"):
           force_setup = True

        elif opt in ("-r"):
            rcode = arg

        elif opt in ("-a"):
            address = arg

        elif opt in ("-p"):
            port = string_to_int(arg, port)

        elif opt in ("-u"):
            ui_path = arg

    print(f'Address={address}')
    print(f'Port={port}')
    print(f'UI path={ui_path}')
    print(f'Device registration code={rcode}')
    print(f'Delete Connections={delete_connections}')
    print(f'Force Setup={force_setup}')
    main(address, port, ui_path, rcode, delete_connections, force_setup)


