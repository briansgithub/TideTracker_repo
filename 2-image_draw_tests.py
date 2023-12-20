import json
import matplotlib.pyplot as plt
from datetime import datetime

# Read JSON data from a file
# JSON is downloaded from NOAA api call
with open('datagetter.json', 'r') as file:
    json_data = json.load(file)

# Extract time and value data
times = [datetime.strptime(entry['t'], '%Y-%m-%d %H:%M') for entry in json_data['predictions']]
values = [float(entry['v']) for entry in json_data['predictions']]

# Plotting
plt.figure(figsize=(10, 6))
plt.plot(times, values, label='v vs t')
plt.title('v vs t')
plt.xlabel('Time')
plt.ylabel('Value')
plt.legend()
plt.grid(True)
plt.show()
