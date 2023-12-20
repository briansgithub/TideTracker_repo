import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime, timedelta
import requests
from PIL import Image

# Get yesterday's date with time set to midnight
yesterday_date = datetime.now() - timedelta(days=1)
yesterday_date = yesterday_date.replace(hour=0, minute=0, second=0, microsecond=0)
yesterday_date_string = yesterday_date.strftime("%Y%m%d")

range_hours = 60
station_id = "8725520"
datum = "mllw"
interval_minutes = 10

# Modify the URL with yesterday's date and the station ID variable
url = f"https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?begin_date={yesterday_date_string}&range={range_hours}&product=predictions&datum={datum}&interval={interval_minutes}&format=json&units=english&time_zone=lst_ldt&station={station_id}"
print("\n" + url + "\n")

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
plt.plot(filtered_times, filtered_values, label='v vs t')
plt.title('Tide Predictions', fontsize=18, weight='bold')  # Larger title
plt.xlabel('Date and Time', fontsize=14, weight='bold')  # Bold x-axis label
plt.ylabel('ΔHeight (Feet)\nfrom Low Tide', fontsize=14, weight='bold')  # Updated y-axis label


# Format x-axis dates with three-letter month abbreviation, rotate, and display time
plt.gca().xaxis.set_major_locator(mdates.HourLocator(interval=6))  # Show every 6 hours
plt.gca().xaxis.set_major_formatter(mdates.DateFormatter('%b %d\n%I:%M %p'))
plt.gcf().autofmt_xdate(rotation=45)  # Rotate x-axis labels for better visibility

# Set x-axis range to start at 12:00 PM
plt.xlim(start_time, filtered_times[-1])

plt.grid(True)

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
