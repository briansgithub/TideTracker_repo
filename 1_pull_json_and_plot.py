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
#DISPLAY_PLOT = False

PERIOD = 2 #hours between TPL5110 reloads
STATIC_TIMEZONE = True #used to set timezone to Fort Myers so get_timezone is averted

def get_timezone(latitude, longitude):
    if(STATIC_TIMEZONE):
        return pytz.timezone('US/Eastern') #Fort Myers
    else:
        tf = TimezoneFinder()
        timezone_str = tf.timezone_at(lng=longitude, lat=latitude)
        
        if timezone_str:
            return pytz.timezone(timezone_str)
        else:
            # Return a default timezone if the location is not found
            return pytz.timezone('UTC')

def get_sunrise_sunset(latitude, longitude, date, zone=None):

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
                return city, state, decimal_latitude, decimal_longitude, get_timezone(decimal_latitude, decimal_longitude)

def fetch_NOAA_data(station_id, date):
    RANGE_HOURS = 60
    DATUM = "mllw"
    INTERVAL_MINUTES = 10
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

    peaks, _ = find_peaks(filtered_values)
    valleys, _ = find_peaks(-np.array(filtered_values))  # Find minima by inverting the values

    # Annotate peaks on the plot
    for peak_index in peaks:
        plt.annotate(rm_lead_zeros(f'{filtered_times[peak_index]:%I:%M %p}'),
                    xy=(filtered_times[peak_index], filtered_values[peak_index]),
                    xytext=(filtered_times[peak_index], filtered_values[peak_index] + 0.05),  # Adjust text position
                    arrowprops=dict(facecolor='none', edgecolor='none'),  # No arrow
                    ha='center', va='center', fontsize=8, weight='bold')

    # Annotate valleys on the plot
    for valley_index in valleys:
        plt.annotate(rm_lead_zeros(f'{filtered_times[valley_index]:%I:%M %p}'),
                    xy=(filtered_times[valley_index], filtered_values[valley_index]),
                    xytext=(filtered_times[valley_index], filtered_values[valley_index] - 0.05),  # Adjust text position
                    arrowprops=dict(facecolor='none', edgecolor='none'),  # No arrow
                    ha='center', va='center', fontsize=8, weight='bold')

 


    plt.title(f'Tide Predictions for\n{city}, {state}', weight='bold')
    # plt.xlabel('Time', fontsize=14, weight='bold')  # Bold x-axis label

    plt.ylabel('Deviation from\nAvg. Low Tide', weight='bold')  # Changed y-axis label
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

    # Set x-axis range to start at 12:00 PM and go to the last time in the list
    plt.xlim(start_time, filtered_times[-1])

    # Overlay additional data points onto the existing plot with a black, solid line and linewidth 12
    two_hours_later = now_dtz + dt.timedelta(hours=PERIOD)
    present_times = [t for t in filtered_times if now_dtz <= t <= two_hours_later]
    present_values = [v for t, v in zip(filtered_times, filtered_values) if now_dtz <= t <= two_hours_later]
    plt.plot(present_times, present_values, label='Present Stretch', color='black', linewidth=12)

    # Draw a Vertical line at current time
    # plt.axvline(now_dtz, 0 , color='r')
    #Horizontal line through y=0
    plt.axhline(y=0, color='black', linewidth=2, label='Zero Line')

    plt.grid(True)

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

    # use a buffer to save plt.savefig to instead of to a file to reduce wear on the microSD card)
    from io import BytesIO
    buffer = BytesIO()
    plt.savefig(buffer, format='png', dpi=600)
    buffer.seek(0)
    if DISPLAY_PLOT:
        img = Image.open(buffer)
    img = img.resize((800, 480))
    img = img.convert('1') #convert bit-depth from 32 (default) to 1
    img.save("plot_image.bmp") # Waveshare can display either png or bmp as long as they're <= 800x480 pixels


    img.show()
    img.close()

    return

# Call the function with a specific station_id when the module is run directly
if __name__ == "__main__":
    station_id = "8725520" # Ft Myers
    # station_id = "8738043" # West Fowl River Bridge 

    city, state, lat, long, zone = get_station_info(station_id)
    
    now_dtz = dt.datetime.now(zone) # _dtz := date, time, zone
    today_d = now_dtz.date()
    yesterday_d = today_d - dt.timedelta(days = 1)
    tomorrow_d = today_d + dt.timedelta(days = 1)

    yesterday_sunrise, yesterday_sunset = get_sunrise_sunset(lat, long, yesterday_d, zone)
    today_sunrise, today_sunset = get_sunrise_sunset(lat, long, today_d, zone)
    tomorrow_sunrise, tomorrow_sunset = get_sunrise_sunset(lat, long, tomorrow_d, zone)    

    data_json = fetch_NOAA_data(station_id, yesterday_d)

    plot_data(data_json, now_dtz)
