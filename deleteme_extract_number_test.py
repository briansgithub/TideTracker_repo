import re

def extract_number_from_string(input_string):
    match = re.match(r'^(\d+)', input_string)
    
    if match:
        return int(match.group(1))
    else:
        # If no number is found in the line, return the default value 8725520
        return 8725520

# Example string
example_string = "This is a string without a number"

# Call the function and print the result
result = extract_number_from_string(example_string)
print(result)
