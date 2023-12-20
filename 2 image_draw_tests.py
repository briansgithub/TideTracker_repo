import numpy as np
import matplotlib.pyplot as plt
from tappy import HarmonicAnalysis

# Example tidal data (time in hours, water level in meters)
time = np.array([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
water_level = np.array([0.5, 1.2, 1.5, 1.0, 0.7, 0.5, 0.9, 1.3, 1.6, 1.1, 0.8])

# Perform harmonic analysis
harmonics = HarmonicAnalysis()
harmonics.set_noaa_standard_constituents()
harmonics.set_data(time, water_level)
harmonics.solve()

# Generate tidal predictions
prediction_time = np.arange(0, 11, 0.1)
predicted_water_level = harmonics.at(prediction_time)

# Plot the original data and the harmonic prediction
plt.plot(time, water_level, 'o-', label='Observed Data')
plt.plot(prediction_time, predicted_water_level, label='Harmonic Prediction')
plt.xlabel('Time (hours)')
plt.ylabel('Water Level (meters)')
plt.title('Tidal Harmonic Analysis and Prediction')
plt.legend()
plt.grid(True)
plt.show()
