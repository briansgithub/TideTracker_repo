import subprocess

# Command to run the script with root privileges
command = ['sudo', 'bash', '0_test.sh']

# Run the command
process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

# Wait for the command to finish and get the output
stdout, stderr = process.communicate()

# Check the return code to see if the command was successful
if process.returncode == 0:
    print(f"Script executed successfully:\n{stdout.decode('utf-8')}")
else:
    print(f"Error running the script:\n{stderr.decode('utf-8')}")
