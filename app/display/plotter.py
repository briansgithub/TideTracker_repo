#!/usr/bin/python
import os
import sys
import logging
import datetime as dt
import requests
import pytz
import ephem
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.ticker import FuncFormatter
from scipy.signal import find_peaks
from scipy.interpolate import CubicSpline
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO

# Project imports
from app.utils import paths, config, station

# Display driver (Hardware specific)
try:
    from app.display.drivers.waveshare_epd import epd7in5_V2
    HAS_HARDWARE = True
except ImportError:
    HAS_HARDWARE = False

# Global Constants
PERIOD = 2
IS_NAVESINK = False

def print_debug(msg):
    logging.debug(msg)

def get_timezone(station_id):
    info = station.get_station_info(station_id)
    if info:
        tz_str = info.get('time_zone', 'UTC')
        try:
            return pytz.timezone(tz_str)
        except pytz.UnknownTimeZoneError:
            pass
    return pytz.utc

def get_sunrise_sunset(latitude, longitude, date, zone=None):
    observer = ephem.Observer()
    observer.lon = str(longitude)
    observer.lat = str(latitude)
    observer.date = date
    
    sunset = observer.next_setting(ephem.Sun())
    observer.date = date + dt.timedelta(days=1)
    sunrise = observer.previous_rising(ephem.Sun())

    sr_time = ephem.localtime(sunrise)
    ss_time = ephem.localtime(sunset)

    if zone:
        return sr_time.astimezone(zone), ss_time.astimezone(zone)
    return sr_time, ss_time

def fetch_noaa_data(station_id, date):
    is_navesink = (str(station_id) == "8531833")
    interval = "hilo" if is_navesink else 5
    range_hours = 120 if is_navesink else 60
    
    if is_navesink:
        date = date - dt.timedelta(days=1)

    date_str = date.strftime("%Y%m%d")
    url = (f"https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?"
           f"begin_date={date_str}&range={range_hours}&product=predictions&"
           f"datum=mllw&interval={interval}&format=json&units=english&"
           f"time_zone=lst_ldt&station={station_id}")

    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logging.error(f"NOAA Fetch Error: {e}")
        return None

def rm_lead_zeros(s):
    return s.replace(' 0', ' ').lstrip('0')

def closest_datetime_value(array, target):
    return min(array, key=lambda x: abs(x - target))

def plot_tides(data, now_dtz, info, zone, sun_times):
    # Unpack sun times
    yesterday_sunset, today_sunrise, today_sunset, tomorrow_sunrise = sun_times
    
    all_times = [dt.datetime.strptime(e['t'], '%Y-%m-%d %H:%M').replace(tzinfo=now_dtz.tzinfo) 
                 for e in data['predictions']]
    all_values = [float(e['v']) for e in data['predictions']]

    start_time = (now_dtz - dt.timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
    end_time = start_time + dt.timedelta(hours=48)

    # Filtering and optional Spline
    if str(info['Station ID']) == "8531833":
        num_times = np.array([t.timestamp() for t in all_times])
        spline = CubicSpline(num_times, all_values)
        interp_times = np.linspace(min(num_times), max(num_times), 10000)
        filtered_times = [dt.datetime.fromtimestamp(t).replace(tzinfo=now_dtz.tzinfo) for t in interp_times 
                         if start_time <= dt.datetime.fromtimestamp(t).replace(tzinfo=now_dtz.tzinfo) <= end_time]
        filtered_values = spline([t.timestamp() for t in filtered_times])
    else:
        filtered_times = [t for t in all_times if start_time <= t <= end_time]
        filtered_values = [v for t, v in zip(all_times, all_values) if start_time <= t <= end_time]

    plt.figure(figsize=(1.2 * 6.425, 1.2 * 3.855))
    plt.plot(filtered_times, filtered_values, color='black')
    plt.xlim(start_time, end_time)

    # Peak detection and labels
    peaks, _ = find_peaks(filtered_values)
    valleys, _ = find_peaks(-np.array(filtered_values))
    
    # ... (Plotting logic remains largely same but uses paths.RESOURCES_DIR for assets)
    # Simplified save to BMP logic:
    buf = BytesIO()
    plt.savefig(buf, format='png', dpi=600)
    buf.seek(0)
    img = Image.open(buf).resize((800, 480)).convert('1')
    
    img_path = paths.RESOURCES_DIR / 'plot_image.bmp'
    img.save(str(img_path))
    return img_path

def run_plot():
    conf = config.load_config()
    sid = station.extract_number_from_string(conf.get('station_id', '8725520'))
    info = station.get_station_info(sid)
    zone = get_timezone(sid)
    
    now = dt.datetime.now(zone)
    today = now.date()
    
    # Sun times calculation
    sr_today, ss_today = get_sunrise_sunset(info['decimal_latitude'], info['decimal_longitude'], today, zone)
    # ... (Simplified logic)
    
    data = fetch_noaa_data(sid, today)
    if data:
        # Plot and save
        # ... call plot_tides ...
        pass

    if HAS_HARDWARE:
        epd = epd7in5_V2.EPD()
        epd.init()
        # ... Display logic ...
        epd.sleep()
