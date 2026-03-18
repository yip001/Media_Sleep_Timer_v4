import os
import time
from datetime import datetime, timedelta

def countdown(t):
    try:
        end_time = datetime.now() + timedelta(seconds=t)
        while t:
            hours, remainder = divmod(t, 3600)
            minutes, seconds = divmod(remainder, 60)
            time_str = "{:02d}:{:02d}:{:02d}".format(hours, minutes, seconds)
            end_time_str = end_time.strftime("%H:%M:%S")
            print("The system will shut down in {} at {}.".format(time_str, end_time_str), end='\r')
            time.sleep(1)
            t -= 1
        os.system("shutdown -h now")
    except KeyboardInterrupt:
        print("\nReset the timer")

timer = 1 * 60 * 60   # Countdown for 1 hour (3600 seconds)

countdown(timer)

