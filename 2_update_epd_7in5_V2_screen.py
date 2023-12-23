#!/usr/bin/python
# -*- coding:utf-8 -*-
import sys
import os
libdir = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'e-ink_lib')
maindir = os.path.dirname(os.path.realpath(__file__))
if os.path.exists(libdir):
    sys.path.append(libdir)

import logging
from waveshare_epd import epd7in5_V2
import time
from PIL import Image,ImageDraw,ImageFont
import traceback

logging.basicConfig(level=logging.DEBUG)

try:
    epd = epd7in5_V2.EPD()
    ### logging.info("\ninit and Clear\n")
    epd.init()
    ### epd.Clear()

    logging.info("\nDisplay a file (does it need to be .bmp?)\n")
    plot_image = Image.open(os.path.join(maindir, 'plot_image.bmp'))
    plot_image = plot_image.transpose(Image.ROTATE_180)
    epd.display(epd.getbuffer(plot_image))
    #time.sleep(2)

    ### # Initialize a canvas. Open a file and display it on the canvas. 
    ### logging.info("4. Create composite images")
    ### Himage2 = Image.new('1', (epd.width, epd.height), 255)  # 255: clear the frame
    ### bmp = Image.open(os.path.join(picdir, '100x100.bmp'))
    ### Himage2.paste(bmp, (50,10))
    ### epd.display(epd.getbuffer(Himage2))
    ### time.sleep(2)

    logging.info("\nGoto Sleep...\n")
    epd.sleep()
    
except IOError as e:
    logging.info(e)
    
except KeyboardInterrupt:    
    logging.info("ctrl + c:")
    epd7in5_V2.epdconfig.module_exit()
    exit()
