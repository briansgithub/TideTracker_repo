#!/usr/bin/python
# -*- coding:utf-8 -*-

import os
import sys
import logging
from PIL import Image
import tt_utils

def run_error_display():
    if tt_utils.IS_RPI:
        from waveshare_epd import epd7in5_V2
    
    maindir = tt_utils.ROOT_DIR
    
    logging.info("epd7in5_V2 Paste over error message")
    
    try:
        plot_image = Image.open(os.path.join(maindir, 'plot_image.bmp')).convert("RGB")
        plot_image = plot_image.transpose(Image.ROTATE_180)

        error_image = Image.open(os.path.join(maindir, 'no_wifi.bmp')).convert("RGB")
        error_image = error_image.transpose(Image.ROTATE_180)

        # Draw image in center of screen
        err_width, err_height = error_image.size
        plot_width, plot_height = plot_image.size
        
        # Calculate the coordinates for the top-left corner to paste in the center
        paste_x = int((plot_width - err_width) / 2)
        paste_y = int((plot_height - err_height) / 2)

        plot_image.paste(error_image, (paste_x, paste_y))

        if tt_utils.IS_RPI:
            epd = epd7in5_V2.EPD()
            epd.init()
            epd.display(epd.getbuffer(plot_image))
            logging.info("EPD Go to Sleep...")
            epd.sleep()
        else:
            plot_image.show()
            
    except Exception as e:
        logging.error(f"Error in no_wifi_paste_over: {e}")

if __name__ == "__main__":
    run_error_display()
