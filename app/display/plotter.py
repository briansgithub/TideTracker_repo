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
font_name_bold = 'Ubuntu-Bold.ttf'
font_name_regular = 'Ubuntu-Regular.ttf'

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
    yesterday_sunset, today_sunrise, today_sunset, tomorrow_sunrise = sun_times
    city, state = info['City'], info['State']
    is_navesink = (str(info['Station ID']) == "8531833")

    all_times = [dt.datetime.strptime(e['t'], '%Y-%m-%d %H:%M').replace(tzinfo=now_dtz.tzinfo) 
                 for e in data['predictions']]
    all_values = [float(e['v']) for e in data['predictions']]

    start_time = (now_dtz - dt.timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
    end_time = start_time + dt.timedelta(hours=48)

    if is_navesink:
        num_times = np.array([t.timestamp() for t in all_times])
        spline = CubicSpline(num_times, all_values)
        interp_times = np.linspace(min(num_times), max(num_times), 10000)
        full_interp_times = [dt.datetime.fromtimestamp(t).replace(tzinfo=now_dtz.tzinfo) for t in interp_times]
        filtered_times = [t for t in full_interp_times if start_time <= t <= end_time]
        filtered_values = spline([t.timestamp() for t in filtered_times])
        hilo_times = [t for t in all_times if t >= start_time]
    else:
        filtered_times = [t for t in all_times if start_time <= t <= end_time]
        filtered_values = [v for t, v in zip(all_times, all_values) if start_time <= t <= end_time]
        hilo_times = filtered_times

    plt.figure(figsize=(1.2 * 6.425, 1.2 * 3.855))
    plt.plot(filtered_times, filtered_values, color='black')
    plt.xlim(start_time, end_time)

    peaks, _ = find_peaks(filtered_values)
    valleys, _ = find_peaks(-np.array(filtered_values))

    approx_label_width = dt.timedelta(hours=4.5)
    ylim0, ylim1 = plt.ylim()
    deadzone = 0.05 * (ylim1 - ylim0)
    YEXTEND = 1.5 * deadzone

    def annotate_points(indices, is_peak):
        for idx in indices:
            x, y = filtered_times[idx], filtered_values[idx]
            dx = dt.timedelta(hours=0)
            if x - approx_label_width/2 < start_time: dx += approx_label_width/2
            if x + approx_label_width/2 > end_time: dx -= approx_label_width/2
            
            offset = YEXTEND if is_peak else -YEXTEND
            if is_navesink: x = closest_datetime_value(hilo_times, x)
            
            plt.annotate(rm_lead_zeros(f'{x:%I:%M %p}'), xy=(x, y),
                         xytext=(x + dx, y + offset), ha='center', va='center',
                         fontsize=8, weight='bold')

    annotate_points(peaks, True)
    annotate_points(valleys, False)

    plt.title(f'Tide Predictions for\n{city}, {state}', weight='bold')
    plt.ylabel('Tide Height (ft)\nAbove Chart Depth', weight='bold')
    plt.gca().yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{round(v, 1)} ft."))
    
    def x_format(v, _):
        d = mdates.num2date(v, zone)
        fmt = '%b. %d\n%I:%M %p' if (d.hour in [0, 12] and d.minute == 0) else '%I:%M %p'
        return rm_lead_zeros(d.strftime(fmt))

    plt.gca().xaxis.set_major_formatter(FuncFormatter(x_format))
    plt.gca().xaxis.set_major_locator(mdates.HourLocator(interval=6))
    plt.grid(True)
    plt.axhline(y=0, color='black', linewidth=2)

    # Highlight current run
    two_hours_later = now_dtz + dt.timedelta(hours=PERIOD)
    pres_times = [t for t in filtered_times if now_dtz <= t <= two_hours_later]
    pres_vals = [v for t, v in zip(filtered_times, filtered_values) if now_dtz <= t <= two_hours_later]
    plt.plot(pres_times, pres_vals, color='black', linewidth=12)

    # Shading for night
    plt.fill_betweenx(y=plt.ylim(), x1=yesterday_sunset, x2=today_sunrise, color='gray', alpha=0.3)
    plt.fill_betweenx(y=plt.ylim(), x1=today_sunset, x2=tomorrow_sunrise, color='gray', alpha=0.3)
    plt.tight_layout()

    buf = BytesIO()
    plt.savefig(buf, format='png', dpi=600)
    buf.seek(0)
    img = Image.open(buf).resize((800, 480)).convert('1')
    
    # Add icons and text overlays
    draw = ImageDraw.Draw(img)
    f18 = ImageFont.truetype(str(paths.FONTS_DIR / font_name_bold), 18)
    f14 = ImageFont.truetype(str(paths.FONTS_DIR / font_name_regular), 14)
    
    sun_icon = Image.open(paths.RESOURCES_DIR / 'sun_rise.png').convert('RGB').resize((40, 40))
    img.paste(sun_icon, (585, 5))
    
    draw.text((19, 8), f"Last Refresh: {now_dtz:%I:%M %p}", font=f14, fill=0)
    draw.text((630, 6), f"Rise: {today_sunrise:%I:%M %p}\nSet:  {today_sunset:%I:%M %p}", font=f18, fill=0)

    img_save_path = paths.RESOURCES_DIR / 'plot_image.bmp'
    img.save(str(img_save_path))
    return img_save_path

def run_plot():
    conf = config.load_config()
    sid = station.extract_number_from_string(conf.get('station_id', '8725520'))
    info = station.get_station_info(sid)
    zone = get_timezone(sid)
    
    now = dt.datetime.now(zone)
    today = now.date()
    
    def get_sun(d):
        sr, ss = get_sunrise_sunset(info['decimal_latitude'], info['decimal_longitude'], d, zone)
        return sr.replace(year=d.year, month=d.month, day=d.day), ss.replace(year=d.year, month=d.month, day=d.day)

    y_sr, y_ss = get_sun(today - dt.timedelta(days=1))
    t_sr, t_ss = get_sun(today)
    tm_sr, tm_ss = get_sun(today + dt.timedelta(days=1))

    data = fetch_noaa_data(sid, today)
    if data:
        plot_tides(data, now, info, zone, (y_ss, t_sr, t_ss, tm_sr))

    if HAS_HARDWARE:
        try:
            epd = epd7in5_V2.EPD()
            epd.init()
            plot_img = Image.open(paths.RESOURCES_DIR / 'plot_image.bmp').transpose(Image.ROTATE_180)
            epd.display(epd.getbuffer(plot_img))
            epd.sleep()
        except Exception as e:
            logging.error(f"E-ink Display Error: {e}")
