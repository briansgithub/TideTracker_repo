#!/usr/bin/python
import time

def main_task():
    # Replace the following line with the task you want to perform
    print("Performing the main task...")

# Set the duration for the task (in seconds)
task_duration = 10 * 60  # 10 minutes

start_time = time.time()

while time.time() - start_time < task_duration:
    main_task()
    time.sleep(1)  # You can adjust the sleep duration if needed

print("Task completed after 10 minutes.")
