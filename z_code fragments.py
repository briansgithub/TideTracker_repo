print()
print("Yesterday")
print(yesterday_d, "\n\t", yesterday_sunrise.strftime("%I:%M %p %Z"), "\n\t", yesterday_sunset.strftime("%I:%M %p %Z"),"\n")
print("Today")
print(today_d, "\n\t", today_sunrise.strftime("%I:%M %p %Z"), "\n\t", today_sunset.strftime("%I:%M %p %Z"),"\n")
print("Tomorrow")
print(tomorrow_d, "\n\t", tomorrow_sunrise.strftime("%I:%M %p %Z"), "\n\t", tomorrow_sunset.strftime("%I:%M %p %Z"),"\n")


    # Highlight the area between yesterday's sunset and today's sunrise
    plt.fill_between(x=[yesterday_sunset, today_sunrise], y1=plt.gca().get_ylim()[0], y2=plt.gca().get_ylim()[1], color='gray', alpha=0.5, label='Nighttime')
    # Highlight the area between today's sunset and tomorrow's sunrise
    plt.fill_between(x=[today_sunset, tomorrow_sunrise], y1=plt.gca().get_ylim()[0], y2=plt.gca().get_ylim()[1], color='gray', alpha=0.5, label='Nighttime')