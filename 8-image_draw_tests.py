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

def get_timezone(latitude, longitude):
    tf = TimezoneFinder()
    timezone_str = tf.timezone_at(lng=longitude, lat=latitude)
    
    if timezone_str:
        return pytz.timezone(timezone_str)
    else:
        # Return a default timezone if the location is not found
        return pytz.timezone('UTC')

def get_sunrise_sunset(latitude, longitude, date, zone=None):

    print("sunrise/sunset function")
    print("\t", longitude, latitude, date, zone)
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
    range_hours = 60
    datum = "mllw"
    interval_minutes = 10

    # Modify the URL with yesterday's date and the station ID variable
    url = f"https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?begin_date={yesterday_date_string}&range={range_hours}&product=predictions&datum={datum}&interval={interval_minutes}&format=json&units=english&time_zone=lst_ldt&station={station_id}"
    print("\n"+url+"\n")  # Change print statement

    # Retrieve data from the URL
    response = requests.get(url)
    
    return response.json()

# Modify the plot_tide_predictions function to use get_station_coordinates
def compute_station_time_info(station_id):
    city, state, lat, long, zone = get_station_info(station_id)
    
    # _dtz := date, time,
    now_dtz = dt.datetime.now(zone)
    today_d = now_dtz.date()
    yesterday_d = today_d - dt.timedelta(days=1)
    tomorrow_d = today_d + dt.timedelta(days=1)

    print(f"DEBUG - today:\n\t {today_d}")
    print(f"DEBUG - city, state, zone\n\t{city}, {state}, {zone}")
    print()

    yesterday_sunrise, yesterday_sunset = get_sunrise_sunset(lat, long, yesterday_d, zone)
    today_sunrise, today_sunset = get_sunrise_sunset(lat, long, today_d, zone)
    tomorrow_sunrise, tomorrow_sunset = get_sunrise_sunset(lat, long, tomorrow_d, zone)

    print()
    print("Yesterday")
    print(yesterday_d, "\n\t", yesterday_sunrise.strftime("%I:%M %p %Z"), "\n\t", yesterday_sunset.strftime("%I:%M %p %Z"),"\n")
    print("Today")
    print(today_d, "\n\t", today_sunrise.strftime("%I:%M %p %Z"), "\n\t", today_sunset.strftime("%I:%M %p %Z"),"\n")
    print("Tomorrow")
    print(tomorrow_d, "\n\t", tomorrow_sunrise.strftime("%I:%M %p %Z"), "\n\t", tomorrow_sunset.strftime("%I:%M %p %Z"),"\n")

    return


def plot_data(data):
     # Extract time and value data. strptime converts string to datetime
    all_times = [dt.datetime.strptime(entry['t'], '%Y-%m-%d %H:%M') for entry in data['predictions']]
    all_values = [float(entry['v']) for entry in data['predictions']]

    # Calculate the start time as 12 hours ago
    start_time = now - dt.timedelta(hours=12)

    # Filter data points that occurred after the start time
    filtered_times = [t for t in all_times if t >= start_time]
    filtered_values = [v for t, v in zip(all_times, all_values) if t >= start_time]

    # Plotting
    plt.figure(figsize=(10, 6))

    # Plot filtered data with a black line
    plt.plot(filtered_times, filtered_values, label='v vs t', color='black')

    plt.title('Tide Predictions', fontsize=18, weight='bold')  # Larger title
    plt.xlabel('Time', fontsize=14, weight='bold')  # Bold x-axis label
    plt.ylabel('ΔHeight (Feet)\nfrom Low Tide', fontsize=14, weight='bold')  # Changed y-axis label

    # Format x-axis dates with three-letter month abbreviation, rotate, and display time
    plt.gca().xaxis.set_major_locator(mdates.HourLocator(interval=6))  # Show every 6 hours
    plt.gca().xaxis.set_major_formatter(mdates.DateFormatter('%b %d\n%I:%M %p'))
    plt.gcf().autofmt_xdate(rotation=45)  # Rotate x-axis labels for better visibility

    # Set x-axis range to start at 12:00 PM
    plt.xlim(start_time, filtered_times[-1])

    # Overlay additional data points onto the existing plot with a black, solid line and linewidth 12
    two_hours_later = now + timedelta(hours=2)
    additional_times = [t for t in all_times if now <= t <= two_hours_later]
    additional_values = [v for t, v in zip(all_times, all_values) if now <= t <= two_hours_later]

    plt.plot(additional_times, additional_values, label='Additional Data', color='black', linewidth=12)

    # Set y-limits to cover the entire range
    plt.ylim(plt.gca().get_ylim()[0], plt.gca().get_ylim()[1])
    # Highlight the area between yesterday's sunset and today's sunrise
    plt.fill_between(x=[yesterday_sunset, today_sunrise], y1=plt.gca().get_ylim()[0], y2=plt.gca().get_ylim()[1], color='gray', alpha=0.5, label='Nighttime')
    
    str_yesterday_sunset = yesterday_sunset.strftime("%I:%M %p %Z")
    print(f"yesterday sunset: {str_yesterday_sunset}")
    

    plt.grid(True)

#    plt.legend()

    # Save the plot as an image
    plt.savefig("plot_image.png")

    # Show the plot (optional)
    # plt.show()

    # Open the saved image using PIL
    img = Image.open("plot_image.png")
    # Display the image (optional)
    img.show()

    # Save the image with a new filename if needed
    # img.save("saved_plot_image.png")

    img.close()

# Call the function with a specific station_id when the module is run directly
if __name__ == "__main__":
    station_id = "8725520" # Ft Myers
    #station_id = "8738043" # West Fowl River Bridge 
    compute_station_time_info(station_id)

    #data = fetch_NOAA_data(station_id, yesterday)

    #plot_data(data)
