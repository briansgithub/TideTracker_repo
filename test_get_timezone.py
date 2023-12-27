import csv
import pytz

def get_time_zone(station_id):
    file_path = "stations.csv"

    # Open the CSV file and read its contents
    with open(file_path, 'r') as csvfile:
        # Create a CSV reader
        csv_reader = csv.DictReader(csvfile)

        # Iterate through rows in the CSV file
        for row in csv_reader:
            # Check if the station_id is in the 'Station ID' column
            if row['Station ID'] == station_id:
                # Retrieve the time_zone for the matching row
                time_zone_str = row['time_zone']

                # Convert the string to a pytz time zone
                try:
                    time_zone = pytz.timezone(time_zone_str)
                    return time_zone
                except pytz.UnknownTimeZoneError:
                    return f"Unknown time zone: {time_zone_str}"

    # Default to UTC if station ID is not found
    return pytz.utc

# Example usage:
station_id_to_search = "9457804"
result = get_time_zone(station_id_to_search)
print(result)
