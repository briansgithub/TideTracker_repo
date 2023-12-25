import os
import RPi.GPIO as GPIO
import subprocess
import time
import requests

#!/usr/bin/python
# -*- coding:utf-8 -*-
import sys
import os
libdir = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'e-ink_lib')
maindir = os.path.dirname(os.path.realpath(__file__))
if os.path.exists(libdir):
    sys.path.append(libdir)

import logging
from waveshare_epd import epd7in5_V2
import time
from PIL import Image,ImageDraw,ImageFont
import traceback


import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import datetime as dt
import requests
from PIL import Image
import csv
import pytz
from timezonefinder import TimezoneFinder
import ephem
from matplotlib.ticker import FuncFormatter
import os
from scipy.signal import find_peaks
import numpy as np


print("BEGINNING")
DISPLAY_PLOT = True

YEXTEND = 0.175 # y-axis addition to move labels and extend ylim0 and ylim1 to make room for labels and present data

PERIOD = 2 #hours between TPL5110 reloads
STATIC_TIMEZONE = True #used to set timezone to Fort Myers so get_timezone is averted
# The data and axes will always be in local time, 
# but the sunrise/sunset and the present run of data
# are affected by the timezoneyou apply

def get_timezone(latitude, longitude):
    if(STATIC_TIMEZONE):
        return pytz.timezone('US/Eastern') #Fort Myers
    else:
        time.sleep(15)
        print(f"-------- Running the wifi script located at:\n\t{auto_run_wifi_path} ---------")
        subprocess.run(['sudo', 'bash', auto_run_wifi_path], check=True)
        time.sleep(1)
        print(f"--------- \nRunning the tides script located at:\n\t{plot_tides_path} ---------")
        subprocess.run(['sudo', 'python3', plot_tides_path], check=True)

    finally:
        # Cleanup GPIO settings
        GPIO.cleanup()
