import os
import sys
import logging
import time
from PIL import Image
from app.utils import paths

def run_error_display():
    if paths.IS_RPI:
        from waveshare_epd import epd7in5_V2
    
    maindir = paths.ROOT_DIR
    
    logging.info("epd7in5_V2 Paste over error message")
    
    try:
        plot_image = Image.open(paths.RESOURCES_DIR / 'plot_image.bmp').convert("RGB")
        plot_image = plot_image.transpose(Image.ROTATE_180)

        error_image = Image.open(paths.RESOURCES_DIR / 'no_wifi.bmp').convert("RGB")
        error_image = error_image.transpose(Image.ROTATE_180)

        # Draw image in center of screen
        err_width, err_height = error_image.size
        plot_width, plot_height = plot_image.size
        
        # Calculate the coordinates for the top-left corner to paste in the center
        paste_x = int((plot_width - err_width) / 2)
        paste_y = int((plot_height - err_height) / 2)

        plot_image.paste(error_image, (paste_x, paste_y))

        if paths.IS_RPI:
            epd = epd7in5_V2.EPD()
            epd.init()
            epd.display(epd.getbuffer(plot_image))
            logging.info("Error screen hardware refresh triggered, waiting 15s...")
            time.sleep(15) # Safety delay for physical drawing
            logging.info("EPD Go to Sleep...")
            epd.sleep()
        else:
            plot_image.show()
            
    except Exception as e:
        logging.error(f"Error in no_wifi_paste_over: {e}")

if __name__ == "__main__":
    run_error_display()
