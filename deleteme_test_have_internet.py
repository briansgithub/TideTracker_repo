import os
import RPi.GPIO as GPIO
import subprocess
import sys

wifi_libdir = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'forked_wifi-connect-headless-rpi','src')
if os.path.exists(wifi_libdir):
    sys.path.append(wifi_libdir)
import netman


        
try: 
    if netman.have_active_internet_connection():
        print("Yes, netman says there's internet\n")
    else: 
        print("No, netman says no internet\n")

except netman.InternetConnectionError as e:
    print(f"Internet connection error: {e}")
except Exception as e:
    print(f"An unexpected error occurred: {e}")

finally:
    # Cleanup GPIO settings
    # close the hotspot and dns mask?
