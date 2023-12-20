import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime, timedelta
import requests
from PIL import Image
import csv
import ephem

def get_sunrise_sunset(decimal_latitude, decimal_longitude, date):
    # Create an observer object for the given coordinates
    observer = ephem.Observer()
    observer.lat = str(decimal_latitude)
    observer.lon = str(decimal_longitude)

    # Set the date for which to compute sunrise and sunset times, considering only the date component
    observer.date = ephem.Date(date.date())

    # Compute sunrise and sunset times
    sunrise = observer.previous_rising(ephem.Sun())
    sunset = observer.next_setting(ephem.Sun())

    # Convert the times to Python datetime objects
    sunrise_datetime = datetime.utcfromtimestamp(min(sunrise.datetime().timestamp(), sunset.datetime().timestamp()))
    sunset_datetime = datetime.utcfromtimestamp(max(sunrise.datetime().timestamp(), sunset.datetime().timestamp()))

    return sunrise_datetime, sunset_datetime

def get_station_coordinates(station_id):
    with open("stations.csv", newline="", encoding="utf-8") as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            if row["Station ID"] == station_id:
                city = row["City"]
                state = row["State"]
                decimal_latitude = float(row["decimal_latitude"])
                decimal_longitude = float(row["decimal_longitude"])
                return city, state, decimal_latitude, decimal_longitude

# Modify the plot_tide_predictions function to use get_station_coordinates
def plot_tide_predictions(station_id):
    city, state, decimal_latitude, decimal_longitude = get_station_coordinates(station_id)

    # Get sunrise and sunset times for yesterday, today, and tomorrow
    yesterday = datetime.now() - timedelta(days=1)
    today = datetime.now()
    tomorrow = datetime.now() + timedelta(days=1)

    yesterday_sunrise, yesterday_sunset = get_sunrise_sunset(decimal_latitude, decimal_longitude, yesterday)
    today_sunrise, today_sunset = get_sunrise_sunset(decimal_latitude, decimal_longitude, today)
    tomorrow_sunrise, tomorrow_sunset = get_sunrise_sunset(decimal_latitude, decimal_longitude, tomorrow)

    # Print or use the sunrise and sunset times as needed
    print(f"Sunrise at {city}, {state} (Yesterday): {yesterday_sunrise}")
    print(f"Sunset at {city}, {state} (Yesterday): {yesterday_sunset}")

    print(f"Sunrise at {city}, {state} (Today): {today_sunrise}")
    print(f"Sunset at {city}, {state} (Today): {today_sunset}")

    print(f"Sunrise at {city}, {state} (Tomorrow): {tomorrow_sunrise}")
    print(f"Sunset at {city}, {state} (Tomorrow): {tomorrow_sunset}")
    
    print(decimal_latitude)
    print(decimal_longitude)
    # Get yesterday's date with time set to midnight
    yesterday_date = datetime.now() - timedelta(days=1)
    yesterday_date = yesterday_date.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_date_string = yesterday_date.strftime("%Y%m%d")

    range_hours = 60
    datum = "mllw"
    interval_minutes = 10

    # Modify the URL with yesterday's date and the station ID variable
    url = f"https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?begin_date={yesterday_date_string}&range={range_hours}&product=predictions&datum={datum}&interval={interval_minutes}&format=json&units=english&time_zone=lst_ldt&station={station_id}"
    print("\n"+url+"\n")  # Change print statement

    # Retrieve data from the URL
    response = requests.get(url)
    data = response.json()

    # Extract time and value data
    all_times = [datetime.strptime(entry['t'], '%Y-%m-%d %H:%M') for entry in data['predictions']]
    all_values = [float(entry['v']) for entry in data['predictions']]

    # Calculate the start time as 12 hours ago
    start_time = yesterday_date + timedelta(hours=12)

    # Filter data points that occurred after the start time
    filtered_times = [t for t in all_times if t >= start_time]
    filtered_values = [v for t, v in zip(all_times, all_values) if t >= start_time]

    # Plotting
    plt.figure(figsize=(10, 6))

    # Plot filtered data with a black line
    plt.plot(filtered_times, filtered_values, label='v vs t', color='black')

    plt.title('Tide Predictions', fontsize=18, weight='bold')  # Larger title
    plt.xlabel('Date and Time', fontsize=14, weight='bold')  # Bold x-axis label
    plt.ylabel('ΔHeight (Feet)\nfrom Low Tide', fontsize=14, weight='bold')  # Changed y-axis label

    # Format x-axis dates with three-letter month abbreviation, rotate, and display time
    plt.gca().xaxis.set_major_locator(mdates.HourLocator(interval=6))  # Show every 6 hours
    plt.gca().xaxis.set_major_formatter(mdates.DateFormatter('%b %d\n%I:%M %p'))
    plt.gcf().autofmt_xdate(rotation=45)  # Rotate x-axis labels for better visibility

    # Set x-axis range to start at 12:00 PM
    plt.xlim(start_time, filtered_times[-1])

    # Overlay additional data points onto the existing plot with a black, solid line and linewidth 12
    now = datetime.now()
    two_hours_later = now + timedelta(hours=2)
    additional_times = [t for t in all_times if now <= t <= two_hours_later]
    additional_values = [v for t, v in zip(all_times, all_values) if now <= t <= two_hours_later]

    plt.plot(additional_times, additional_values, label='Additional Data', color='black', linewidth=12)

    # Highlight the area between yesterday's sunset and today's sunrise
    plt.fill_betweenx(y=[plt.gca().get_ylim()[0], plt.gca().get_ylim()[1]], x1=yesterday_sunset, x2=today_sunrise, color='gray', alpha=0.5, label='Nighttime')
    print(yesterday_sunset)
    print(yesterday_sunrise)
    print (all_times[0])

    plt.grid(True)

    plt.legend()

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
    station_id = "8725520"
    plot_tide_predictions(station_id)
