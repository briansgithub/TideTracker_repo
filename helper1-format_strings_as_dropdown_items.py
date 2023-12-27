input_filename = "station_strings.txt"
output_filename = "formatted_station_strings.txt"

with open(input_filename, "r") as input_file, open(output_filename, "w") as output_file:
    for line in input_file:
        # Split the line into parts based on "-" and strip extra whitespaces
        parts = [part.strip() for part in line.split("-")]
        
        # Extract the value and label
        value = parts[0].strip()
        label = "-".join(parts[1:]).strip()

        # Write the formatted string to the output file
        formatted_string = f'<option value="{value}">{line.strip()}</option>\n'
        output_file.write(formatted_string)

print(f"Formatted strings written to {output_filename}")
