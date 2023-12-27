import json

# Specify the path to the JSON file
json_file_path = 'tidetracker_persistent_data.json'

# Read the JSON data from the file
with open(json_file_path, 'r') as file:
    data = json.load(file)

# Extract the station_id value
station_string = data.get('station_id')

# Print or use the station_id variable as needed
print(f"Station ID: {station_string}")
