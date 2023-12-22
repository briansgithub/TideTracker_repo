import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.ticker import FuncFormatter
from datetime import datetime, timedelta
import numpy as np

def custom_date_format(x, _):
    # Convert the numpy float value to a datetime object
    x_datetime = mdates.num2date(x)

    # Custom formatting function to show only the date if the time is midnight
    if x_datetime.hour == 0 and x_datetime.minute == 0:
        return x_datetime.strftime('%b. %d')  # Format for midnight
    else:
        return x_datetime.strftime('%b. %d\n%I:%M %p')  # Format for other times

# Sample data for demonstration
now_dtz = datetime.now()
times = [now_dtz - timedelta(hours=i) for i in range(24)]
values = np.random.rand(24)

# Plotting
plt.plot(times, values)

# Customize the printed format of the x-axis labels with a period after the month abbreviation
plt.gca().xaxis.set_major_formatter(FuncFormatter(custom_date_format))
plt.gca().xaxis.set_major_locator(mdates.HourLocator(interval=6))  # Show every 6 hours

# Set x-axis labels to bold
for label in plt.gca().xaxis.get_majorticklabels():
    label.set_weight('bold')

plt.gcf().autofmt_xdate(rotation=45)  # Rotate x-axis labels for better visibility

plt.show()
