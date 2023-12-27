from bs4 import BeautifulSoup

# Read HTML from file
with open("input_to_3-formatted_station_strings.txt", "r") as file:
    html_content = file.read()

# Parse HTML using BeautifulSoup
soup = BeautifulSoup(html_content, 'html.parser')

# Extract option elements
options = soup.find_all('option')

# Extract option values and text
option_data = [(int(option['value']), option.text.strip()) for option in options]

# Sort option data based on the value
sorted_option_data = sorted(option_data, key=lambda x: x[0])

# Create a new HTML with sorted options
sorted_html = '<select>\n'
for value, text in sorted_option_data:
    sorted_html += f'    <option value="{value}">{text}</option>\n'
sorted_html += '</select>'

# Save the sorted HTML to a new file
with open("input_to_4-sorted_station_dropdown_items.txt", "w") as output_file:
    output_file.write(sorted_html)

print("Sorting and saving completed.")
