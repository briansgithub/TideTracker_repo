import pandas as pd
from timezonefinder import TimezoneFinder

def get_time_zone_and_print_city_state(row):
    tf = TimezoneFinder()

    # Find the timezone based on latitude and longitude
    time_zone = tf.timezone_at(lng=row["decimal_longitude"], lat=row["decimal_latitude"])

    city, state = row["City"], row["State"]

    print(f"City: {city}, State: {state}, Time Zone: {time_zone}")

    return time_zone if time_zone else "Unknown"

def add_time_zone_to_csv(input_csv, output_csv):
    df = pd.read_csv(input_csv)

    df["time_zone"] = df.apply(get_time_zone_and_print_city_state, axis=1)

    df.to_csv(output_csv, index=False)

if __name__ == "__main__":
    input_file = "stations.csv"
    output_file = "stations_with_timezone.csv"

    add_time_zone_to_csv(input_file, output_file)
