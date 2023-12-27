#!/usr/bin/python
# -*- coding:utf-8 -*-
import sys
import os


import logging
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
import ephem
from matplotlib.ticker import FuncFormatter
import os
from scipy.signal import find_peaks
import numpy as np

libdir = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'e-ink_lib')
maindir = os.path.dirname(os.path.realpath(__file__))
if os.path.exists(libdir):
    sys.path.append(libdir)

from pathlib import Path
import re
import json

def is_raspberry_pi():
    # https://raspberrypi.stackexchange.com/a/139704/540
    CPUINFO_PATH = Path("/proc/cpuinfo")

    if not CPUINFO_PATH.exists():
        return False
    with open(CPUINFO_PATH) as f:
        cpuinfo = f.read()
    return re.search(r"^Model\s*:\s*Raspberry Pi", cpuinfo, flags=re.M) is not None 

IS_RPI = is_raspberry_pi()

if(IS_RPI):
    from waveshare_epd import epd7in5_V2


DISPLAY_PLOT = True

print("BEGINNING")

YEXTEND = 0.175 # y-axis addition to move labels and extend ylim0 and ylim1 to make room for labels and present data

PERIOD = 2 #hours between TPL5110 reloads
STATIC_TIMEZONE = True #used to set timezone to Fort Myers so get_timezone is averted
# The data and axes will always be in local time, 
# but the sunrise/sunset and the present run of data
# are affected by the timezoneyou apply


def get_timezone(station_id):
    file_path = "stations.csv"

    # Open the CSV file and read its contents
    with open(file_path, 'r') as csvfile:
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
    observer.date = date + dt.timedelta(days = 1) 
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
    RANGE_HOURS = 60
    DATUM = "mllw"
    # NOAA: https://api.tidesandcurrents.noaa.gov/api/prod/
        #   "...all internet data services have limits on the amount/length 
        #   of data which can be retrieved per request."
        # 1-minute interval data	Data length is limited to 4 days
        # 6-minute interval data	Data length is limited to 1 month
        # Hourly interval data	    Data length is limited to 1 year
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
        print(f"Error fetching NOAA data: {e}")
        return None

def rm_lead_zeros(time_string):
    # Replace leading zeros in the hour part of the time string
    time_string = time_string.replace('01:', '1:').replace('02:', '2:').replace('03:', '3:').replace('04:', '4:').replace('05:', '5:').replace('06:', '6:').replace('07:', '7:').replace('08:', '8:').replace('09:', '9:')
    return time_string
    
def plot_data(data, now_dtz):
     # Extract time and value data. strptime converts string to datetime
    all_times = [dt.datetime.strptime(entry['t'], '%Y-%m-%d %H:%M') for entry in data['predictions']]
    all_times = [_.replace(tzinfo=now_dtz.tzinfo) for _ in all_times]
    all_values = [float(entry['v']) for entry in data['predictions']]

    start_time = (now_dtz - dt.timedelta(days = 1)).replace(hour = 12, minute = 0, second = 0, microsecond = 0)

    # Filter data points that occurred after the start time
    filtered_times = [t for t in all_times if t >= start_time]
    filtered_values = [v for t, v in zip(all_times, all_values) if t >= start_time]

    # Plotting. Size of 7.5in e-ink is 163.2mm x 97.92mm. Converted to in: 6.425 x 3.855
    plt.figure(figsize=(1.2*6.425, 1.2*3.855))

    # Plot filtered data
    # plt.plot(all_times, all_values, label='v vs t', color='black')
    plt.plot(filtered_times, filtered_values, label='v vs t', color='black')

    # Set x-axis range to start at 12:00 PM and go to the last time in the list
    plt.xlim(start_time, filtered_times[-1])

    # Find peaks in the data
    peaks, _ = find_peaks(filtered_values)
    valleys, _ = find_peaks(-np.array(filtered_values))  # Find minima by inverting the values

    # Annotate peaks on the plot
    approx_label_width = dt.timedelta(hours=4.5) # eyeballed from graph
    # Format x graph limits as datetimes (from foat64 since epoch time)
    xlim0 = mdates.num2date(plt.xlim()[0],zone)
    xlim1 = mdates.num2date(plt.xlim()[1],zone)

    for peak_index in peaks:
        x_coord = filtered_times[peak_index]
        y_coord = filtered_values[peak_index]
        
        delta_x = dt.timedelta(hours=0)

        # Check if the annotation is too close to the left edge
        if x_coord - approx_label_width/2 < xlim0:  # Adjust timedelta as needed
            delta_x += approx_label_width/2  # Move the annotation to the right

        # Check if the annotation would be too close to the right edge
        if x_coord + approx_label_width/2 > xlim1:  # Adjust timedelta as needed
            delta_x -= approx_label_width/2  # Move the annotation to the left
            
        # Annotate
        plt.annotate(rm_lead_zeros(f'{x_coord:%I:%M %p}'),
                    xy=(x_coord, y_coord),
                    xytext=(x_coord + delta_x, y_coord + YEXTEND),  # Adjust text position
                    arrowprops=dict(facecolor='none', edgecolor='none'),  # No arrow
                    ha='center', va='center', fontsize=8, weight='bold')

    # Annotate valleys on the plot
    for valley_index in valleys:
        x_coord = filtered_times[valley_index]
        y_coord = filtered_values[valley_index]

        delta_x = dt.timedelta(hours=0)

        # Check if the annotation is too close to the left edge
        if x_coord - approx_label_width/2 < xlim0:  # Adjust timedelta as needed
            delta_x += approx_label_width/2  # Move the annotation to the right

        # Check if the annotation is too close to the right edge
        if x_coord + approx_label_width/2 > xlim1:  # Adjust timedelta as needed
            delta_x -= approx_label_width/2  # Move the annotation to the left
        
        plt.annotate(rm_lead_zeros(f'{x_coord:%I:%M %p}'),
                    xy=(x_coord, y_coord),
                    xytext=(x_coord + delta_x, y_coord - YEXTEND),  # Adjust text position
                    arrowprops=dict(facecolor='none', edgecolor='none'),  # No arrow
                    ha='center', va='center', fontsize=8, weight='bold')    



    plt.title(f'Tide Predictions for\n{city}, {state}', weight='bold')
    # plt.xlabel('Time', fontsize=14, weight='bold')  # Bold x-axis label

    plt.ylabel('Tide Height (ft)\nAbove Chart Depth', weight='bold')  # Changed y-axis label
    # Add "ft." label to y-axis tick labels
    def add_ft_label(value, _):
        rounded_value = round(value, 1)
        return f"{rounded_value} ft."
    plt.gca().yaxis.set_major_formatter(FuncFormatter(add_ft_label))
    
    
    def custom_x_axis_major_label_format(value, _):
        # Custom format definition taken directly from mdates.DateFormatter() definition.

        value_datetime = mdates.num2date(value, zone)
        if (value_datetime.hour == 0 or value_datetime.hour == 12) and value_datetime.minute == 0:
            result = rm_lead_zeros(value_datetime.strftime('%b. %d\n%I:%M %p'))
        else:
            result = rm_lead_zeros(value_datetime.strftime('%I:%M %p'))

        return result
    plt.gca().xaxis.set_major_formatter(FuncFormatter(custom_x_axis_major_label_format))


    ### This will not work.
    '''
    major_tick_labels = plt.gca().xaxis.get_majorticklabels()
    for label in major_tick_labels:
        print(label)
    '''
    # See https://stackoverflow.com/questions/11244514/modify-tick-label-text
    
    ### date_formatter = mdates.DateFormatter('%b. %d\n%I:%M %p', tz=zone)
    ### plt.gca().xaxis.set_major_formatter(date_formatter)
 

    # Set x-axis minor tick labels format
    
    def custom_x_axis_minor_label_format(value, _):
    # Custom format definition taken directly from mdates.DateFormatter() definition.
        value_datetime = mdates.num2date(value, zone)
        result = value_datetime.strftime('%I').replace('01','1').replace('02','2').replace('03','3').replace('04','4').replace('05','5').replace('06','6').replace('07','7').replace('08','8').replace('09','9')

        return result
    ### plt.gca().xaxis.set_minor_formatter(FuncFormatter(custom_x_axis_minor_label_format))
    
    # Set x-axis minor tick labels to be bold - may be broken
    # Doesn't work
    '''
    for label in plt.gca().xaxis.get_minorticklabels():
        label.set_weight('bold')
        label.set_rotation(45)  # Adjust the rotation angle as needed for better visibility
        label.set_fontsize(4) 
    '''

    # This (probably) ensures that the x-axis is labeled in 6-hour increments. (Although this probably happens automatically)
    plt.gca().xaxis.set_major_locator(mdates.HourLocator(interval=6))  # Show every 6 hours

    # Set x-axis minor locator to show ticks every 3 hours
    plt.gca().xaxis.set_minor_locator(mdates.HourLocator(interval=2))

    plt.gca().tick_params(axis='x', which='major', size=9)
    plt.gca().tick_params(axis='x', which='minor', size=4)
 

    # Set x-axis labels to bold
    for label in plt.gca().xaxis.get_majorticklabels():
        label.set_weight('bold')
    # This rotates the labels and prevents overlapping 
    plt.gcf().autofmt_xdate(rotation=45)  # Rotate x-axis labels for better visibility



    # Overlay additional data points onto the existing plot with a black, solid line and linewidth 12
    two_hours_later = now_dtz + dt.timedelta(hours=PERIOD)
    present_times = [t for t in filtered_times if now_dtz <= t <= two_hours_later]
    present_values = [v for t, v in zip(filtered_times, filtered_values) if now_dtz <= t <= two_hours_later]
    plt.plot(present_times, present_values, label='Present Run', color='black', linewidth=12)

    # Draw a Vertical line at current time
    # plt.axvline(now_dtz, 0 , color='r')
    #Horizontal line through y=0
    plt.axhline(y=0, color='black', linewidth=2, label='Zero Line')

    plt.grid(True)

    # Get the current y-limits
    ylim0 = plt.ylim()[0]
    ylim1 = plt.ylim()[1]

    # give the labels some buffer space in the y direction
    ylim_offset = YEXTEND

    # Increase the y-limits by the offset
    plt.ylim(ylim0 - ylim_offset, ylim1 + ylim_offset)

    # Set y-limits to cover the entire range. For shading at night. 
    plt.ylim(plt.gca().get_ylim()[0], plt.gca().get_ylim()[1])
    ### plt.fill_betweenx(y=[plt.gca().get_ylim()[0], plt.gca().get_ylim()[1]], x1=yesterday_sunset, x2=today_sunrise, color='gray', alpha=0.5, label='Shaded Area')
    ### plt.fill_betweenx(y=[plt.gca().get_ylim()[0], plt.gca().get_ylim()[1]], x1=today_sunset, x2=tomorrow_sunrise, color='gray', alpha=0.5, label='Shaded Area')
    # Fill between yesterday's sunset and today's sunrise with a dithered pattern (only hatching, no solid fill)
    plt.fill_betweenx(y=[plt.gca().get_ylim()[0], plt.gca().get_ylim()[1]], x1=yesterday_sunset, x2=today_sunrise, facecolor='gray', edgecolor='none', label='Shaded Area')

    # Fill between today's sunset and tomorrow's sunrise with a different dithered pattern (only hatching, no solid fill)
    plt.fill_betweenx(y=[plt.gca().get_ylim()[0], plt.gca().get_ylim()[1]], x1=today_sunset, x2=tomorrow_sunrise, facecolor='gray', edgecolor='none', label='Shaded Area')

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
    img = img.convert('1') #convert bit-depth from 32 (default) to 1
    img.save(os.path.join(maindir, 'plot_image.bmp')) # Waveshare can display either png or bmp as long as they're <= 800x480 pixels


    if DISPLAY_PLOT and not IS_RPI:
        img.show()

    img.close()

    return

def extract_number_from_string(input_string):
    match = re.match(r'^(\d+)', input_string)
    
    if match:
        return int(match.group(1))
    else:
        # If no number is found in the line, return the default value 8725520 (Fort Myers station ID number)
        return 8725520 

# Call the function with a specific station_id when the module is run directly
if __name__ == "__main__":

    json_file_path = os.path.join(maindir, 'tidetracker_persistent_data.json')

    # Read the JSON data from the file
    with open(json_file_path, 'r') as file:
        data = json.load(file)

    # Extract the station_id value
    station_string = data.get('station_id')
    station_id = extract_number_from_string(station_string)
    station_id = str(station_id)

    city, state, lat, long = get_station_info(station_id)
    zone = get_timezone(station_id)

    
    now_dtz = dt.datetime.now(zone) # _dtz := date, time, zone
    today_d = now_dtz.date()
    yesterday_d = today_d - dt.timedelta(days = 1)
    tomorrow_d = today_d + dt.timedelta(days = 1)

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

    data_json = fetch_NOAA_data(station_id, yesterday_d)

    plot_data(data_json, now_dtz)

    if(IS_RPI):
        try:
            epd = epd7in5_V2.EPD()
            ### logging.info("\ninit and Clear\n")
            epd.init()

            logging.info("\nDisplaying the .bmp on the e-ink display)\n")
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

            logging.info("\nGoto Sleep...\n")
            epd.sleep()
            
        except IOError as e:
            logging.info(e)
            
        except KeyboardInterrupt:    
            logging.info("ctrl + c:")
            epd7in5_V2.epdconfig.module_exit()
            exit()
