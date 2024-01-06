#!/usr/bin/python
# -*- coding:utf-8 -*-
import sys
import os

from PIL import Image, ImageDraw, ImageFont

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import datetime as dt
import requests
from PIL import Image
import csv
import pytz
import ephem
from matplotlib.ticker import FuncFormatter
from scipy.signal import find_peaks
import numpy as np



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

print_debug("BEGINNING")

YEXTEND = 0.175*0.6  # y-axis addition to move labels and extend ylim0 and ylim1 to make room for labels and present data

PERIOD = 2  # hours between TPL5110 reloads
STATIC_TIMEZONE = True  # used to set timezone to Fort Myers so get_timezone is averted

print_debug("Defining functions...")


def get_timezone(station_id):

    # Open the CSV file and read its contents
    csv_path = os.path.join(maindir, 'stations.csv')
    with open(csv_path, 'r') as csvfile:
        # Create a CSV reader
        csv_reader = csv.DictReader(csvfile)

        # Iterate through rows in the CSV file
        for row in csv_reader:
            # Check if the station_id is in the 'Station ID' column
            if row['Station ID'] == station_id:
                # Retrieve the time_zone for the matching row
                time_zone_str = row['time_zone']

                # Convert the string to a pytz time zone
                try:
                    time_zone = pytz.timezone(time_zone_str)
                    return time_zone
                except pytz.UnknownTimeZoneError:
                    return f"Unknown time zone: {time_zone_str}"

    # Default to UTC if station ID is not found
    return pytz.utc


def get_sunrise_sunset(latitude, longitude, date, zone=None):
    print_debug(f"Calculating sunrise and sunset for date: {date}")

    # This code has a bug where some places, like Honolulu, HI for example,
    # return sunset with a date of 2 days ago instead of with yesterday's date.
    # It doesn't make a big difference since contiguous days have negligibly different
    # sunrise and sunset times.

    # Create an observer object
    observer = ephem.Observer()
    observer.lon = str(longitude)
    observer.lat = str(latitude)

    # Convert the date to the required format

    # Calculate sunrise and sunset times
    # Get the "next sunset" after the midnight at the beginning of the date
    observer.date = date
    sunset = observer.next_setting(ephem.Sun())

    # Get the "previous sunrise" before the midnight that ends the date (technically the next day)
    observer.date = date + dt.timedelta(days=1)
    sunrise = observer.previous_rising(ephem.Sun())  # Get the most recent sunrise

    # Format the results
    sunrise_time = ephem.localtime(sunrise)
    sunset_time = ephem.localtime(sunset)

    if zone:
        return sunrise_time.astimezone(zone), sunset_time.astimezone(zone)
    else:
        return sunrise_time, sunset_time


def get_station_info(station_id):
    CSV_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'stations.csv')
    with open(CSV_PATH, newline="", encoding="utf-8") as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            if row["Station ID"] == station_id:
                city = row["City"]
                state = row["State"]
                decimal_latitude = float(row["decimal_latitude"])
                decimal_longitude = float(row["decimal_longitude"])
                return city, state, decimal_latitude, decimal_longitude


def fetch_NOAA_data(station_id, date):
    print_debug(f"Fetching NOAA data for station ID {station_id} on date {date}")
    RANGE_HOURS = 60
    DATUM = "mllw"

    INTERVAL_MINUTES = 5
    yesterday_date_string = date.strftime("%Y%m%d")

    try:
        # Modify the URL with yesterday's date and the station ID variable
        url = f"https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?begin_date={yesterday_date_string}&range={RANGE_HOURS}&product=predictions&datum={DATUM}&interval={INTERVAL_MINUTES}&format=json&units=english&time_zone=lst_ldt&station={station_id}"

        # Retrieve data from the URL
        response = requests.get(url)
        response.raise_for_status()  # Raise an exception for HTTP errors

        return response.json()

    except requests.exceptions.RequestException as e:
        print_debug(f"Error fetching NOAA data: {e}")
        return None


def rm_lead_zeros(time_string):
    print_debug(f"Removing leading zeros in time string: {time_string}")
    # Replace leading zeros in the hour part of the time string
    time_string = (
        time_string.replace('01:', '1:')
        .replace('02:', '2:')
        .replace('03:', '3:')
        .replace('04:', '4:')
        .replace('05:', '5:')
        .replace('06:', '6:')
        .replace('07:', '7:')
        .replace('08:', '8:')
        .replace('09:', '9:')
    )
    return time_string


def plot_data(data, now_dtz):
    print_debug("Plotting data...")

    # Extract time and value data. strptime converts string to datetime
    all_times = [dt.datetime.strptime(entry['t'], '%Y-%m-%d %H:%M') for entry in data['predictions']]
    all_times = [_.replace(tzinfo=now_dtz.tzinfo) for _ in all_times]
    all_values = [float(entry['v']) for entry in data['predictions']]

    start_time = (now_dtz - dt.timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)

    # Filter data points that occurred after the start time
    filtered_times = [t for t in all_times if t >= start_time]
    filtered_values = [v for t, v in zip(all_times, all_values) if t >= start_time]

    # Plotting. Size of 7.5in e-ink is 163.2mm x

    # Plotting. Size of 7.5in e-ink is 163.2mm x 97.92mm. Converted to in: 6.425 x 3.855
    print_debug("Creating plot figure...")

    plt.figure(figsize=(1.2 * 6.425, 1.2 * 3.855))

    # Plot filtered data
    plt.plot(filtered_times, filtered_values, label='v vs t', color='black')

    # Set x-axis range to start at 12:00 PM and go to the last time in the list
    plt.xlim(start_time, filtered_times[-1])

    # Find peaks in the data
    peaks, _ = find_peaks(filtered_values)
    valleys, _ = find_peaks(-np.array(filtered_values))  # Find minima by inverting the values

    print_debug("Annotating peaks on the plot...")

    # Annotate peaks on the plot
    approx_label_width = dt.timedelta(hours=4.5)  # eyeballed from graph
    deadzone_height = 0.055  # .046 = approx text height ; ~ 0.00463ft/px. text is 9px high; 12px deadzone. 

    for peak_index in peaks:
        x_coord = filtered_times[peak_index]
        y_coord = filtered_values[peak_index]

        delta_x = dt.timedelta(hours=0)
        delta_y = 0

        # Check if the annotation is too close to the left edge
        if x_coord - approx_label_width / 2 < start_time:
            delta_x += approx_label_width / 2  # Move the annotation to the right

        # Check if the annotation would be too close to the right edge
        if x_coord + approx_label_width / 2 > filtered_times[-1]:
            delta_x -= approx_label_width / 2  # Move the annotation to the left

        text_center = y_coord + YEXTEND
        # For peak, if text is within half the deadzone above y=0, add half the deadzone
        if 0 < text_center <= deadzone_height/2:
            delta_y += deadzone_height/2
        # For peak, if text is within half the deadzone below y=0, add the deadzone
        if -(deadzone_height/2) <= text_center <= 0:
            delta_y += deadzone_height


        # Annotate
        plt.annotate(rm_lead_zeros(f'{x_coord:%I:%M %p}'),
                     xy=(x_coord, y_coord),
                     xytext=(x_coord + delta_x, text_center + delta_y),  # Adjust text position
                     arrowprops=dict(facecolor='none', edgecolor='none'),  # No arrow
                     ha='center', va='center', fontsize=8, weight='bold')

    print_debug("Annotating valleys on the plot...")

    # Annotate valleys on the plot
    for valley_index in valleys:
        x_coord = filtered_times[valley_index]
        y_coord = filtered_values[valley_index]

        delta_x = dt.timedelta(hours=0)
        delta_y = 0

        # Check if the annotation is too close to the left edge
        if x_coord - approx_label_width / 2 < start_time:
            delta_x += approx_label_width / 2  # Move the annotation to the right

        # Check if the annotation is too close to the right edge
        if x_coord + approx_label_width / 2 > filtered_times[-1]:
            delta_x -= approx_label_width / 2  # Move the annotation to the left
        
        text_center = y_coord - YEXTEND

        # For valley, if text is within half the deadzone above y=0, subtract the deadzone
        if 0 <= text_center <= deadzone_height/2:
            delta_y -= deadzone_height
        # For valley, if text is within half the deadzone below y=0, subtract half the deadzone
        if -(deadzone_height/2) <= text_center < 0:
            delta_y -= deadzone_height/2

        plt.annotate(rm_lead_zeros(f'{x_coord:%I:%M %p}'),
                     xy=(x_coord, y_coord),
                     xytext=(x_coord + delta_x, text_center + delta_y),  # Adjust text position
                     arrowprops=dict(facecolor='none', edgecolor='none'),  # No arrow
                     ha='center', va='center', fontsize=8, weight='bold')



    print_debug("Setting plot labels and formatting...")

    plt.title(f'Tide Predictions for\n{city}, {state}', weight='bold')

    plt.ylabel('Tide Height (ft)\nAbove Chart Depth', weight='bold')

    def add_ft_label(value, _):
        rounded_value = round(value, 1)
        return f"{rounded_value} ft."

    plt.gca().yaxis.set_major_formatter(FuncFormatter(add_ft_label))

    def custom_x_axis_major_label_format(value, _):
        value_datetime = mdates.num2date(value, zone)
        if (value_datetime.hour == 0 or value_datetime.hour == 12) and value_datetime.minute == 0:
            result = rm_lead_zeros(value_datetime.strftime('%b. %d\n%I:%M %p'))
        else:
            result = rm_lead_zeros(value_datetime.strftime('%I:%M %p'))

        return result

    plt.gca().xaxis.set_major_formatter(FuncFormatter(custom_x_axis_major_label_format))

    plt.gca().xaxis.set_major_locator(mdates.HourLocator(interval=6))
    plt.gca().xaxis.set_minor_locator(mdates.HourLocator(interval=2))

    plt.gca().tick_params(axis='x', which='major', size=9)
    plt.gca().tick_params(axis='x', which='minor', size=4)

    for label in plt.gca().xaxis.get_majorticklabels():
        label.set_weight('bold')

    plt.gcf().autofmt_xdate(rotation=45)

    two_hours_later = now_dtz + dt.timedelta(hours=PERIOD)
    present_times = [t for t in filtered_times if now_dtz <= t <= two_hours_later]
    present_values = [v for t, v in zip(filtered_times, filtered_values) if now_dtz <= t <= two_hours_later]
    plt.plot(present_times, present_values, label='Present Run', color='black', linewidth=12)

    plt.axhline(y=0, color='black', linewidth=2, label='Zero Line')

    plt.grid(True)

    ylim0 = plt.ylim()[0]
    ylim1 = plt.ylim()[1]

    fudge_factor = deadzone_height/5
    # Insert sunrise/sunset times on plot
    plt.annotate(rm_lead_zeros(f'{today_sunrise:%I:%M %p}'),
                xy=(today_sunrise, ylim1 + 2*YEXTEND - deadzone_height- fudge_factor),
                xytext=(today_sunrise, ylim1 + 2*YEXTEND - deadzone_height - fudge_factor),  # Adjust text position
                arrowprops=dict(facecolor='none', edgecolor='none'),  # No arrow
                ha='center', va='center', fontsize=10, weight='bold')

    plt.annotate(rm_lead_zeros(f'{today_sunset:%I:%M %p}'),
                xy=(today_sunset, ylim1 + 2*YEXTEND - deadzone_height - fudge_factor),
                xytext=(today_sunset, ylim1 + 2*YEXTEND - deadzone_height - fudge_factor),  # Adjust text position
                arrowprops=dict(facecolor='none', edgecolor='none'),  # No arrow
                ha='center', va='center', fontsize=10, weight='bold')


    # +2*YEXTEND: +1 for highest tide peak and +1 for sunrise/sunset tiemes to fit above highest tide peak annotation
    plt.ylim(ylim0 - YEXTEND, ylim1 + 2*YEXTEND)


    plt.ylim(plt.gca().get_ylim()[0], plt.gca().get_ylim()[1])

    plt.fill_betweenx(y=[plt.gca().get_ylim()[0], plt.gca().get_ylim()[1]], x1=yesterday_sunset,
                      x2=today_sunrise, facecolor='gray', edgecolor='none', label='Shaded Area')

    plt.fill_betweenx(y=[plt.gca().get_ylim()[0], plt.gca().get_ylim()[1]], x1=today_sunset,
                      x2=tomorrow_sunrise, facecolor='gray', edgecolor='none', label='Shaded Area')

    plt.tight_layout()

    # Format 'bmp' is not supported (supported formats: eps, jpeg, jpg, pdf, pgf, png, ps, raw, rgba, svg, svgz, tif, tiff, webp)
    # plt.savefig("plot_image.png", dpi=600)
    # plt.show()

    # use a buffer to save plt.savefig to instead of to a file (to reduce wear on the microSD card; and mitigage file path issues...)
    from io import BytesIO

    buffer = BytesIO()
    plt.savefig(buffer, format='png', dpi=600)
    buffer.seek(0)
    img = Image.open(buffer)


    img = img.resize((800, 480))
    img = img.convert('1')


    # Add sun rise/set icons
    sun_icon = Image.open(sun_rise_icon_path).convert('RGB').resize((40, 40))

    y_pos = 5
    left_x_pos = 19
    right_x_pos = 585
    x_buf_space = 5
    y_buf_space = 1

    img.paste(sun_icon, (right_x_pos,y_pos))

    # Add font
    draw = ImageDraw.Draw(img)

    # Refreh time
    draw.text((left_x_pos, y_pos + 3), 
        rm_lead_zeros(f'Last Refresh:'), 
        font = font14, 
        fill = 0)
    draw.text((left_x_pos+92, y_pos + 3), 
            rm_lead_zeros(f'{now_dtz:%I:%M %p}\n{now_dtz:%m/%d/%Y}'), 
            font = font14, 
            fill = 0)

    draw.text((right_x_pos + sun_icon.width + x_buf_space, y_pos + y_buf_space), 
              rm_lead_zeros(f'Rise:   {today_sunrise:%I:%M %p}\nSet:     {today_sunset:%I:%M %p}'), 
              font = font18, 
              fill = 0)
    

    img.save(os.path.join(maindir, 'plot_image.bmp'))

    if DISPLAY_PLOT and not IS_RPI:
        img.show()
        # plt.show()

    img.close()

    return


def extract_number_from_string(input_string):
    match = re.match(r'^(\d+)', input_string)

    if match:
        return int(match.group(1))
    else:
        return 8725520


if __name__ == "__main__":
    print_debug("Reading JSON data from file...")

    json_file_path = os.path.join(maindir, 'tidetracker_persistent_data.json')

    # Read the JSON data from the file
    with open(json_file_path, 'r') as file:
        data = json.load(file)

    station_string = data.get('station_id')

    station_id = extract_number_from_string(station_string)
    station_id = str(station_id)

    print_debug(f"Getting station information for ID: {station_id}...")

    city, state, lat, long = get_station_info(station_id)
    zone = get_timezone(station_id)

    now_dtz = dt.datetime.now(zone)  # _dtz := date, time, zone
    today_d = now_dtz.date()
    yesterday_d = today_d - dt.timedelta(days=1)
    tomorrow_d = today_d + dt.timedelta(days=1)

    yesterday_sunrise, yesterday_sunset = get_sunrise_sunset(lat, long, yesterday_d, zone)
    today_sunrise, today_sunset = get_sunrise_sunset(lat, long, today_d, zone)
    tomorrow_sunrise, tomorrow_sunset = get_sunrise_sunset(lat, long, tomorrow_d, zone)

    # THIS IS A STOPGAP PATCH BECAUSE I CAN'T FIGURE OUT WHAT MY BUG IS WITH SUNRISE AND SUNSET!
    # BUT I'M OUT OF TIME! So maybe I'll fix this later.

    yesterday_sunrise, yesterday_sunset = get_sunrise_sunset(lat, long, yesterday_d, zone)
    yesterday_sunrise = yesterday_sunrise.replace(year=yesterday_d.year, month=yesterday_d.month, day=yesterday_d.day)
    yesterday_sunset = yesterday_sunset.replace(year=yesterday_d.year, month=yesterday_d.month, day=yesterday_d.day)

    today_sunrise, today_sunset = get_sunrise_sunset(lat, long, today_d, zone)
    today_sunrise = today_sunrise.replace(year=today_d.year, month=today_d.month, day=today_d.day)
    today_sunset = today_sunset.replace(year=today_d.year, month=today_d.month, day=today_d.day)

    tomorrow_sunrise, tomorrow_sunset = get_sunrise_sunset(lat, long, tomorrow_d, zone)
    tomorrow_sunrise = tomorrow_sunrise.replace(year=tomorrow_d.year, month=tomorrow_d.month, day=tomorrow_d.day)
    tomorrow_sunset = tomorrow_sunset.replace(year=tomorrow_d.year, month=tomorrow_d.month, day=tomorrow_d.day)

    # Good enough

    print_debug("Fetching NOAA data...")

    data_json = fetch_NOAA_data(station_id, yesterday_d)

    print_debug("Plotting data...")

    plot_data(data_json, now_dtz)

    ### PUT THE SUNRISE + SUNSET HERE 
    

    if IS_RPI:
        try:
            print_debug("Initializing e-ink display...")

            epd = epd7in5_V2.EPD()
            epd.init()

            print_debug("Displaying the .bmp on the e-ink display...")

            plot_image = Image.open(os.path.join(maindir, 'plot_image.bmp'))
            plot_image = plot_image.transpose(Image.ROTATE_180)
            epd.display(epd.getbuffer(plot_image))
            #time.sleep(2)

            ### # Initialize a canvas. Open a file and display it on the canvas. 
            ### logging.info("4. Create composite images")
            ### Himage2 = Image.new('1', (epd.width, epd.height), 255)  # 255: clear the frame
            ### bmp = Image.open(os.path.join(picdir, '100x100.bmp'))
            ### Himage2.paste(bmp, (50,10))
            ### epd.display(epd.getbuffer(Himage2))
            ### time.sleep(2)


            print_debug("Going to sleep...")

            epd.sleep()

        except IOError as e:
            print(f"IOError: {e}")

        except KeyboardInterrupt:
            print("ctrl + c:")
            epd7in5_V2.epdconfig.module_exit()
            exit()
