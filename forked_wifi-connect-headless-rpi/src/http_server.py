# Our main wifi-connect application, which is based around an HTTP server.

import os, getopt, sys, json, atexit
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import parse_qs
from io import BytesIO

# Local modules
import netman
import dnsmasq

# Defaults
ADDRESS = '192.168.42.1'
PORT = 80
UI_PATH = '../ui'


#------------------------------------------------------------------------------
# called at exit
def cleanup():
    print("Cleaning up prior to exit.")
    dnsmasq.stop()
    netman.stop_hotspot()


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
def RequestHandlerClassFactory(address, ssids, rcode, status_snapshot):

    class MyHTTPReqHandler(SimpleHTTPRequestHandler):

        def __init__(self, *args, **kwargs):
            # We must set our custom class properties first, since __init__() of
            # our super class will call do_GET().
            self.address = address
            self.ssids = ssids
            self.rcode = rcode
            self.status_snapshot = status_snapshot
            super(MyHTTPReqHandler, self).__init__(*args, **kwargs)

        # See if this is a specific request, otherwise let the server handle it.
        def do_GET(self):

            print(f'do_GET {self.path}')

            # Handle the hotspot starting and a computer connecting to it,
            # we have to return a redirect to the gateway to get the 
            # captured portal to show up.
            if '/hotspot-detect.html' == self.path:
                self.send_response(301) # redirect
                new_path = f'http://{self.address}/'
                print(f'redirecting to {new_path}')
                self.send_header('Location', new_path)
                self.end_headers()

            if '/generate_204' == self.path:
                self.send_response(301) # redirect
                new_path = f'http://{self.address}/'
                print(f'redirecting to {new_path}')
                self.send_header('Location', new_path)
                self.end_headers()

            # Handle a REST API request to return the connection status snapshot
            if '/status' == self.path:
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                
                # Check if there was a recent connection failure
                failed_ssid = None
                if os.path.exists('/tmp/wifi_failed.txt'):
                    with open('/tmp/wifi_failed.txt', 'r') as f:
                        failed_ssid = f.read().strip()
                
                response_data = self.status_snapshot.copy()
                if failed_ssid:
                    response_data['failed_ssid'] = failed_ssid

                self.wfile.write(json.dumps(response_data).encode('utf-8'))
                return

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
                self.send_response(200)
                self.end_headers()
                response = BytesIO()
                ssids = self.ssids # passed in to the class factory
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

            # Not sure if this is just OSX hitting the captured portal,
            # but we need to exit if we get it.
            if '/bag' == self.path:
                sys.exit()

            # All other requests are handled by the server which vends files 
            # from the ui_path we were initialized with.
            super().do_GET()


        def do_POST(self):
            content_length = int(self.headers['Content-Length'])
            body = self.rfile.read(content_length)
            self.send_response(200)
            self.end_headers()
            response = BytesIO()
            fields = parse_qs(body.decode('utf-8'))

            print(f'do_POST {self.path} fields: {fields.keys()}')

            if self.path == '/exit':
                print('Exit request received. Stopping hotspot and exiting.')
                response.write(b'OK\n')
                self.wfile.write(response.getvalue())
                sys.exit()

            # NOAA Station ID field name
            FORM_STATION = 'station'

            if self.path == '/update_station':
                if FORM_STATION not in fields:
                    print(f'Error: /update_station missing {FORM_STATION} field.')
                    return
                
                station_id = fields[FORM_STATION][0]
                submitted_station_save_path = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))),
                    'tidetracker_persistent_data.json'
                )
                with open(submitted_station_save_path, 'w') as json_file:
                    json.dump({'station_id': station_id}, json_file)
                print(f"Station ID ({station_id}) saved to {submitted_station_save_path}")
                response.write(b'OK\n')
                self.wfile.write(response.getvalue())
                return

            if self.path == '/connect':
                FORM_SSID = 'ssid'
                FORM_HIDDEN_SSID = 'hidden-ssid'
                FORM_USERNAME = 'identity'
                FORM_PASSWORD = 'passphrase'

                if FORM_SSID not in fields:
                    print(f'Error: /connect missing {FORM_SSID} field.')
                    return

                ssid = fields[FORM_SSID][0]
                password = fields[FORM_PASSWORD][0] if FORM_PASSWORD in fields else None
                username = fields[FORM_USERNAME][0] if FORM_USERNAME in fields else None
                
                if FORM_HIDDEN_SSID in fields:
                    ssid = fields[FORM_HIDDEN_SSID][0]

                # Look up the ssid in the list we sent, to find out its security type
                conn_type = netman.CONN_TYPE_SEC_NONE
                if FORM_HIDDEN_SSID in fields: 
                    conn_type = netman.CONN_TYPE_SEC_PASSWORD

                for s in self.ssids:
                    if 'ssid' in s and ssid == s['ssid']:
                        if s['security'] == "ENTERPRISE":
                            conn_type = netman.CONN_TYPE_SEC_ENTERPRISE
                        elif s['security'] == "NONE":
                            conn_type = netman.CONN_TYPE_SEC_NONE 
                        else:
                            conn_type = netman.CONN_TYPE_SEC_PASSWORD
                        break

                # Send response immediately so the webpage doesn't hang or show error
                response.write(b'OK\n')
                self.wfile.write(response.getvalue())
                
                def test_wifi():
                    print(f'Testing connection to {ssid}...')
                    # Stop the hotspot and connect to the user's selected AP
                    netman.stop_hotspot()
                    success = netman.connect_to_AP(conn_type=conn_type, ssid=ssid, \
                            username=username, password=password)

                    if success:
                        print(f'Connected! Exiting app.')
                        if os.path.exists('/tmp/wifi_failed.txt'):
                            os.remove('/tmp/wifi_failed.txt')
                        os._exit(0)
                    else:
                        print(f'Connection failed, restarting the hotspot.')
                        with open('/tmp/wifi_failed.txt', 'w') as f:
                            f.write(ssid)
                        global_ssids = netman.get_list_of_access_points()
                        self.ssids.clear()
                        self.ssids.extend(global_ssids)
                        netman.start_hotspot()

                import threading
                t = threading.Thread(target=test_wifi)
                t.start()
                return

    return MyHTTPReqHandler


#------------------------------------------------------------------------------
# Create the hotspot, start dnsmasq, start the HTTP server.
def main(address, port, ui_path, rcode, delete_connections, force_setup):

    # See if caller wants to delete all existing connections first
    if delete_connections:
        netman.delete_all_wifi_connections()

    # Check if we are already connected, if so we are done.
    if not force_setup and netman.have_active_internet_connection():
        print('Already connected to the internet, nothing to do, exiting.')
        sys.exit()

    # Capture WiFi status BEFORE starting the hotspot
    status_snapshot = {
        'ssid': netman.get_connected_ssid() or 'None',
        'internet': netman.have_active_internet_connection()
    }
    print(f'Pre-hotspot status snapshot: {status_snapshot}')

    # Get list of available AP from net man.  
    ssids = netman.get_list_of_access_points()

    # Start the hotspot
    if not netman.start_hotspot():
        print('Error starting hotspot, exiting.')
        sys.exit(1)

    # Start dnsmasq
    dnsmasq.start()

    # Find the ui directory
    web_dir = os.path.join(os.path.dirname(__file__), ui_path)
    os.chdir(web_dir)

    server_address = (address, port)

    # Custom request handler class
    MyRequestHandlerClass = RequestHandlerClassFactory(address, ssids, rcode, status_snapshot)

    # Start an HTTP server
    print(f'\033[91mWaiting for a connection to our hotspot {netman.get_hotspot_SSID()} ...\033[0m')
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
nections={delete_connections}')
    print(f'Force Setup={force_setup}')
    main(address, port, ui_path, rcode, delete_connections, force_setup)
