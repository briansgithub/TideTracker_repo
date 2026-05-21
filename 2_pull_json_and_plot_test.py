#!/usr/bin/python
# -*- coding:utf-8 -*-
import sys
import os
import gc

from PIL import Image, ImageDraw, ImageFont

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import datetime as dt
import requests
import csv
import pytz
import ephem
from matplotlib.ticker import FuncFormatter
from scipy.signal import find_peaks
import numpy as np
from scipy.interpolate import CubicSpline

from pathlib import Path
import re
import json

def print_debug(message):
    print(f"DEBUG: {message}")

def is_raspberry_pi():
    CPUINFO_PATH = Path("/proc/cpuinfo")
    if not CPUINFO_PATH.exists():
        return False
    with open(CPUINFO_PATH) as f:
        cpuinfo = f.read()
    return re.search(r"^Model\s*:\s*Raspberry Pi", cpuinfo, flags=re.M) is not None

IS_RPI = is_raspberry_pi()

font_name_bold = "Ubuntu-Bold.ttf"
font_name_regular = "Ubuntu-Regular.ttf"

if IS_RPI:
    libdir = '/home/pi/TideTracker_repo/e-ink_lib'
    maindir = '/home/pi/TideTracker_repo'
    if os.path.exists(libdir):
        sys.path.append(libdir)

    from waveshare_epd import epd7in5_V2

    font18 = ImageFont.truetype(f'/home/pi/TideTracker_repo/{font_name_bold}', 18)
    font14 = ImageFont.truetype(f'/home/pi/TideTracker_repo/{font_name_regular}', 14)
    sun_rise_icon_path = '/home/pi/TideTracker_repo/sun_rise.png'
    sun_set_icon_path = '/home/pi/TideTracker_repo/sun_set.png'

else:
    libdir = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'e-ink_lib')
    maindir = os.path.dirname(os.path.realpath(__file__))
    if os.path.exists(libdir):
        sys.path.append(libdir)
    font18 = ImageFont.truetype(os.path.join(maindir, font_name_bold), 18)
    font14 = ImageFont.truetype(os.path.join(maindir, font_name_regular), 14)
    sun_rise_icon_path = os.path.join(maindir, "sun_rise.png")
    sun_set_icon_path = os.path.join(maindir, "sun_set.png")
    

DISPLAY_PLOT = True
PERIOD = 2  # hours between TPL5110 reloads
STATIC_TIMEZONE = True  # used to set timezone to Fort Myers so get_timezone is averted
IS_NAVESINK = False

def get_timezone(station_id):
    csv_path = os.path.join(maindir, 'stations.csv')
    with open(csv_path, 'r') as csvfile:
        csv_reader = csv.DictReader(csvfile)
        for row in csv_reader:
            if row['Station ID'] == station_id:
                time_zone_str = row['time_zone']
                try:
                    return pytz.timezone(time_zone_str)
                except pytz.UnknownTimeZoneError:
                    return f"Unknown time zone: {time_zone_str}"
    return pytz.utc

def get_sunrise_sunset(latitude, longitude, date, zone=None):
    observer = ephem.Observer()
    observer.lon = str(longitude)
    observer.lat = str(latitude)
    observer.date = date
    sunset = observer.next_setting(ephem.Sun())
    observer.date = date + dt.timedelta(days=1)
    sunrise = observer.previous_rising(ephem.Sun())
    sunrise_time = ephem.localtime(sunrise)
    sunset_time = ephem.localtime(sunset)
    if zone:
        return sunrise_time.astimezone(zone), sunset_time.astimezone(zone)
    return sunrise_time, sunset_time

def get_station_info(station_id):
    CSV_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'stations.csv')
    with open(CSV_PATH, newline="", encoding="utf-8") as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            if row["Station ID"] == station_id:
                return row["City"], row["State"], float(row["decimal_latitude"]), float(row["decimal_longitude"])

def fetch_NOAA_data(station_id, date):
    INTERVAL_MINUTES = 5
    RANGE_HOURS = 60
    DATUM = "mllw"

    if IS_NAVESINK:
        date = date - dt.timedelta(days=1)
        INTERVAL_MINUTES = "hilo"
        RANGE_HOURS = 120
        
    yesterday_date_string = date.strftime("%Y%m%d")
    try:
        url = f"https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?begin_date={yesterday_date_string}&range={RANGE_HOURS}&product=predictions&datum={DATUM}&interval={INTERVAL_MINUTES}&format=json&units=english&time_zone=lst_ldt&station={station_id}"
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print_debug(f"Error fetching NOAA data: {e}")
        return None

def rm_lead_zeros(time_string):
    return (time_string.replace('01:', '1:').replace('02:', '2:').replace('03:', '3:')
            .replace('04:', '4:').replace('05:', '5:').replace('06:', '6:')
            .replace('07:', '7:').replace('08:', '8:').replace('09:', '9:'))

def closest_datetime_value(array, target):
    closest = array[0]
    min_diff = abs((array[0] - target).total_seconds())
    for d in array:
        diff = abs((d - target).total_seconds())
        if diff < min_diff:
            min_diff = diff
            closest = d
    return closest

def plot_data(data, now_dtz):
    print_debug("Plotting data...")
    all_times = [dt.datetime.strptime(entry['t'], '%Y-%m-%d %H:%M').replace(tzinfo=now_dtz.tzinfo) for entry in data['predictions']]
    all_values = [float(entry['v']) for entry in data['predictions']]

    start_time = (now_dtz - dt.timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
    end_time = start_time + dt.timedelta(hours=48)
    
    hilo_times = [t for t in all_times if t >= start_time]
    hilo_values = [v for t, v in zip(all_times, all_values) if t >= start_time]

    if IS_NAVESINK:
        numeric_times = np.array([t.timestamp() for t in all_times])
        numeric_values = np.array(all_values)
        sorted_indices = np.argsort(numeric_times)
        sorted_times = numeric_times[sorted_indices]
        sorted_values = numeric_values[sorted_indices]
        spline_interpolator = CubicSpline(sorted_times, sorted_values)
        interpolation_times = np.linspace(min(sorted_times), max(sorted_times), 10000)
        interpolated_values = spline_interpolator(interpolation_times)
        interpolated_times = [dt.datetime.fromtimestamp(t).replace(tzinfo=now_dtz.tzinfo) for t in interpolation_times]
        filtered_times = [t for t in interpolated_times if start_time <= t <= end_time]
        filtered_values = [v for t, v in zip(interpolated_times, interpolated_values) if start_time <= t <= end_time]
    else:
        filtered_times = hilo_times
        filtered_values = hilo_values

    plt.figure(figsize=(1.2 * 6.425, 1.2 * 3.855))
    plt.plot(filtered_times, filtered_values, label='v vs t', color='black')
    plt.xlim(start_time, filtered_times[-1] if not IS_NAVESINK else end_time)

    peaks, _ = find_peaks(filtered_values)
    valleys, _ = find_peaks(-np.array(filtered_values))

    approx_label_width = dt.timedelta(hours=4.5)
    ylim0, ylim1 = plt.ylim()
    deadzone_height = .05 * (ylim1 - ylim0)
    YEXTEND = 1.5 * deadzone_height

    for peak_index in peaks:
        x_coord, y_coord = filtered_times[peak_index], filtered_values[peak_index]
        delta_x = dt.timedelta(hours=0)
        delta_y = 0
        if x_coord - approx_label_width / 2 < start_time: delta_x += approx_label_width / 2
        if x_coord + approx_label_width / 2 > filtered_times[-1]: delta_x -= approx_label_width / 2
        text_center = y_coord + YEXTEND
        if 0 < text_center <= deadzone_height/2: delta_y += deadzone_height/2
        if -(deadzone_height/2) <= text_center <= 0: delta_y += deadzone_height
        if IS_NAVESINK: x_coord = closest_datetime_value(hilo_times, x_coord)
        plt.annotate(rm_lead_zeros(f'{x_coord:%I:%M %p}'), xy=(x_coord, y_coord),
                     xytext=(x_coord + delta_x, text_center + delta_y), ha='center', va='center', fontsize=8, weight='bold')

    for valley_index in valleys:
        x_coord, y_coord = filtered_times[valley_index], filtered_values[valley_index]
        delta_x = dt.timedelta(hours=0)
        delta_y = 0
        if x_coord - approx_label_width / 2 < start_time: delta_x += approx_label_width / 2
        if x_coord + approx_label_width / 2 > filtered_times[-1]: delta_x -= approx_label_width / 2
        text_center = y_coord - YEXTEND
        if 0 <= text_center <= deadzone_height/2: delta_y -= deadzone_height
        if -(deadzone_height/2) <= text_center < 0: delta_y -= deadzone_height/2
        if IS_NAVESINK: x_coord = closest_datetime_value(hilo_times, x_coord)
        plt.annotate(rm_lead_zeros(f'{x_coord:%I:%M %p}'), xy=(x_coord, y_coord),
                     xytext=(x_coord + delta_x, text_center + delta_y), ha='center', va='center', fontsize=8, weight='bold')

    plt.title(f'Tide Predictions for\n{city}, {state}', weight='bold')
    plt.ylabel('Tide Height (ft)\nAbove Chart Depth', weight='bold')
    plt.gca().yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{round(v, 1)} ft."))
    
    def custom_x_axis_major_label_format(value, _):
        value_datetime = mdates.num2date(value, zone)
        fmt = '%b. %d\n%I:%M %p' if (value_datetime.hour in [0, 12] and value_datetime.minute == 0) else '%I:%M %p'
        return rm_lead_zeros(value_datetime.strftime(fmt))

    plt.gca().xaxis.set_major_formatter(FuncFormatter(custom_x_axis_major_label_format))
    plt.gca().xaxis.set_major_locator(mdates.HourLocator(interval=6))
    plt.gca().xaxis.set_minor_locator(mdates.HourLocator(interval=2))
    plt.gcf().autofmt_xdate(rotation=45)

    two_hours_later = now_dtz + dt.timedelta(hours=PERIOD)
    present_times = [t for t in filtered_times if now_dtz <= t <= two_hours_later]
    present_values = [v for t, v in zip(filtered_times, filtered_values) if now_dtz <= t <= two_hours_later]
    plt.plot(present_times, present_values, color='black', linewidth=12)
    plt.axhline(y=0, color='black', linewidth=2)
    plt.grid(True)

    ylim0, ylim1 = plt.ylim()
    fudge = deadzone_height/5
    plt.annotate(rm_lead_zeros(f'{today_sunrise:%I:%M %p}'), xy=(today_sunrise, ylim1 + 2*YEXTEND - deadzone_height- fudge), ha='center', va='center', fontsize=10, weight='bold')
    plt.annotate(rm_lead_zeros(f'{today_sunset:%I:%M %p}'), xy=(today_sunset, ylim1 + 2*YEXTEND - deadzone_height - fudge), ha='center', va='center', fontsize=10, weight='bold')
    plt.ylim(ylim0 - YEXTEND, ylim1 + 2*YEXTEND)

    plt.fill_betweenx(y=plt.ylim(), x1=yesterday_sunset, x2=today_sunrise, facecolor='gray', alpha=0.3)
    plt.fill_betweenx(y=plt.ylim(), x1=today_sunset, x2=tomorrow_sunrise, facecolor='gray', alpha=0.3)
    plt.tight_layout()

    from io import BytesIO
    buffer = BytesIO()
    plt.savefig(buffer, format='png', dpi=600)
    plt.close('all') # Critical: Free matplotlib memory
    gc.collect()

    buffer.seek(0)
    img = Image.open(buffer).resize((800, 480)).convert('1')
    sun_icon = Image.open(sun_rise_icon_path).convert('RGB').resize((40, 40))
    img.paste(sun_icon, (585, 5))

    draw = ImageDraw.Draw(img)
    draw.text((19, 8), 'Last Refresh:', font=font14, fill=0)
    draw.text((111, 8), f'{now_dtz:%I:%M %p}\n{now_dtz:%m/%d/%Y}', font=font14, fill=0)
    draw.text((630, 6), f'Rise:   {today_sunrise:%I:%M %p}\nSet:     {today_sunset:%I:%M %p}', font=font18, fill=0)
    
    img.save(os.path.join(maindir, 'plot_image.bmp'))
    if DISPLAY_PLOT and not IS_RPI: img.show()
    img.close()
    buffer.close()

def extract_number_from_string(input_string):
    match = re.match(r'^(\d+)', input_string)
    return int(match.group(1)) if match else 8725520

if __name__ == "__main__":
    json_file_path = os.path.join(maindir, 'tidetracker_persistent_data.json')
    with open(json_file_path, 'r') as file:
        data = json.load(file)

    station_id = str(extract_number_from_string(data.get('station_id')))
    if station_id == "8531833": IS_NAVESINK = True

    city, state, lat, long = get_station_info(station_id)
    zone = get_timezone(station_id)

    now_dtz = dt.datetime.now(zone)
    today_d = now_dtz.date()
    yesterday_d = today_d - dt.timedelta(days=1)
    tomorrow_d = today_d + dt.timedelta(days=1)

    _, yesterday_sunset = get_sunrise_sunset(lat, long, yesterday_d, zone)
    today_sunrise, today_sunset = get_sunrise_sunset(lat, long, today_d, zone)
    tomorrow_sunrise, _ = get_sunrise_sunset(lat, long, tomorrow_d, zone)

    yesterday_sunset = yesterday_sunset.replace(year=yesterday_d.year, month=yesterday_d.month, day=yesterday_d.day)
    today_sunrise = today_sunrise.replace(year=today_d.year, month=today_d.month, day=today_d.day)
    today_sunset = today_sunset.replace(year=today_d.year, month=today_d.month, day=today_d.day)
    tomorrow_sunrise = tomorrow_sunrise.replace(year=tomorrow_d.year, month=tomorrow_d.month, day=tomorrow_d.day)

    data_json = fetch_NOAA_data(station_id, yesterday_d)
    if data_json:
        plot_data(data_json, now_dtz)

    if IS_RPI:
        try:
            print_debug("Initializing e-ink display...")
            epd = epd7in5_V2.EPD()
            epd.init()
            print_debug("Displaying plot...")
            with Image.open(os.path.join(maindir, 'plot_image.bmp')) as plot_image:
                epd.display(epd.getbuffer(plot_image.transpose(Image.ROTATE_180)))
            epd.sleep()
        except Exception as e:
            print(f"Display Error: {e}")
        finally:
            # CRITICAL: Always wait for refresh to finish before power-off pulse
            print_debug("Holding for 20s to ensure physical refresh completes...")
            import time
            time.sleep(20)
