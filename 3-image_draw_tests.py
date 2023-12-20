import matplotlib.pyplot as plt
import time
from datetime import datetime, timedelta
import requests
from PIL import Image

# Get yesterday's date in the format YYYYMMDD
# Specify the station ID
# Datum - mean lower low water
yesterday_date = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
range_hours = 60
station_id = "8725520"
datum = "mllw"
interval_minutes = 10

# Modify the URL with yesterday's date and the station ID variable
url = f"https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?begin_date={yesterday_date}&range={range_hours}&product=predictions&datum={datum}&interval={interval_minutes}&format=json&units=english&time_zone=lst_ldt&station={station_id}"
print(url)

# Retrieve data from the URL
response = requests.get(url)
data = response.json()

# Extract time and value data
times = [datetime.strptime(entry['t'], '%Y-%m-%d %H:%M') for entry in data['predictions']]
values = [float(entry['v']) for entry in data['predictions']]

# Plotting
plt.figure(figsize=(10, 6))
plt.plot(times, values, label='v vs t')
plt.title('Tide Predictions')
plt.xlabel('Date')
plt.ylabel('Height (Feet)\nDeviation from Average Low Tide, MLLW')
#plt.legend()
plt.grid(True)

# Save the plot as an image
plt.savefig("plot_image.png")

# Show the plot (optional)
#plt.show() 

# Open the saved image using PIL
img = Image.open("plot_image.png")
# Display the image (optional)
img.show()

# Save the image with a new filename if needed
#img.save("saved_plot_image.png")

img.close()