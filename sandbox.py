import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime, timedelta, timezone

# Sample data with timezone information
now_dtz = datetime.now(timezone.utc)
yesterday_sunset = now_dtz - timedelta(hours=24)  # Replace with actual sunset time
today_sunrise = now_dtz + timedelta(hours=6)  # Replace with actual sunrise time
today_sunset = now_dtz + timedelta(hours=18)  # Replace with actual sunset time
tomorrow_sunrise = now_dtz + timedelta(hours=30)  # Replace with actual sunrise time

# Plotting
plt.figure(figsize=(10, 6))

# Highlight the area between yesterday's sunset and today's sunrise
plt.fill_between(x=[yesterday_sunset, today_sunrise], y1=plt.gca().get_ylim()[0], y2=plt.gca().get_ylim()[1], color='gray', alpha=0.5, label='Nighttime')

# Highlight the area between today's sunset and tomorrow's sunrise
plt.fill_between(x=[today_sunset, tomorrow_sunrise], y1=plt.gca().get_ylim()[0], y2=plt.gca().get_ylim()[1], color='gray', alpha=0.5, label='Nighttime')

# Formatting x-axis labels as AM/PM in the specified timezone (e.g., UTC)
plt.gca().xaxis.set_major_locator(mdates.HourLocator(interval=1))
plt.gca().xaxis.set_major_formatter(mdates.DateFormatter('%I:%M %p', tz=timezone.utc))

# Set labels and title
plt.xlabel('Time')
plt.ylabel('Values')
plt.title('Plot with Datetime X-axis in AM/PM Format and Timezone UTC')

# Rotate x-axis labels for better readability
plt.xticks(rotation=45)

# Display the plot
plt.legend()
plt.show()
