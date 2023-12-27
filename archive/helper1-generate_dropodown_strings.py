import csv

csv_file_path = 'stations.csv'
output_file_path = 'station_strings.txt'

with open(csv_file_path, 'r') as csv_file, open(output_file_path, 'w') as output_file:
    csv_reader = csv.DictReader(csv_file)
    for row in csv_reader:
        station_id = row['Station ID']
        city = row['City']
        state = row['State']
        
        output_line = f"{station_id} - {city}, {state}\n"
        output_file.write(output_line)

print(f"Output has been saved to {output_file_path}")
