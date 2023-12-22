import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import datetime as dt
import requests
from PIL import Image
import csv
import pytz
from timezonefinder import TimezoneFinder
import ephem

print("BEGINNING")

PERIOD = 2 #hours between TPL5110 reloads

def get_timezone(latitude, longitude):
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
    observer.date = date

    # Calculate sunrise and sunset times
    sunrise = observer.previous_rising(ephem.Sun())  # Get the most recent sunrise
    sunset = observer.next_setting(ephem.Sun())  # Get the next sunset

    # Format the results
    sunrise_time = ephem.localtime(sunrise)
    sunset_time = ephem.localtime(sunset)

    if zone:
        return sunrise_time.astimezone(zone), sunset_time.astimezone(zone)
    else: 
        return sunrise_time, sunset_time
    
def get_station_info(station_id):
    with open("stations.csv", newline="", encoding="utf-8") as csvfile:
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

    # Modify the URL with yesterday's date and the station ID variable
    url = f"https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?begin_date={yesterday_date_string}&range={RANGE_HOURS}&product=predictions&datum={DATUM}&interval={INTERVAL_MINUTES}&format=json&units=english&time_zone=lst_ldt&station={station_id}"

    # Retrieve data from the URL
    response = requests.get(url)
    
    return response.json()

def plot_data(data, now_dtz):
     # Extract time and value data. strptime converts string to datetime
    all_times = [dt.datetime.strptime(entry['t'], '%Y-%m-%d %H:%M') for entry in data['predictions']]
    all_times = [_.replace(tzinfo=now_dtz.tzinfo) for _ in all_times]
    all_values = [float(entry['v']) for entry in data['predictions']]

    start_time = (now_dtz - dt.timedelta(days = 1)).replace(hour = 12, minute = 0, second = 0, microsecond = 0)

    # Filter data points that occurred after the start time
    filtered_times = [t for t in all_times if t >= start_time]
    filtered_values = [v for t, v in zip(all_times, all_values) if t >= start_time]

    # Plotting
    plt.figure(figsize=(10, 6))

    # Plot filtered data
    # plt.plot(all_times, all_values, label='v vs t', color='black')
    plt.plot(filtered_times, filtered_values, label='v vs t', color='black')
    plt.title(f'Tide Predictions for\n{city}, {state}', fontsize=18, weight='bold')
    plt.xlabel('Time', fontsize=14, weight='bold')  # Bold x-axis label
    plt.ylabel('ΔHeight (Feet)\nfrom Avg. Low Tide', fontsize=14, weight='bold')  # Changed y-axis label

    # This sets the printed format of the x-axis labels
    plt.gca().xaxis.set_major_formatter(mdates.DateFormatter('%b %d\n%I:%M %p', tz=zone))
    # This (probably) ensures that the x-axis is labeled in 6-hour increments. (Although this probably happens automatically)
    plt.gca().xaxis.set_major_locator(mdates.HourLocator(interval=6))  # Show every 6 hours
    # This rotates the labels and prevents overlapping 
    plt.gcf().autofmt_xdate(rotation=45)  # Rotate x-axis labels for better visibility

    # Set x-axis range to start at 12:00 PM and go to the last time in the list
    plt.xlim(start_time, filtered_times[-1])

    # Overlay additional data points onto the existing plot with a black, solid line and linewidth 12
    two_hours_later = now_dtz + dt.timedelta(hours=PERIOD)
    present_times = [t for t in filtered_times if now_dtz <= t <= two_hours_later]
    present_values = [v for t, v in zip(filtered_times, filtered_values) if now_dtz <= t <= two_hours_later]
    plt.plot(present_times, present_values, label='Present Stretch', color='black', linewidth=12)

    plt.scatter(now_dtz, 0 , color='r', label='New Point')

    plt.grid(True)

    plt.savefig("plot_image.png")
    plt.show()

    img = Image.open("plot_image.png")
    #img.show()
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
