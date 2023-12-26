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
from PIL import Image

logging.info("epd7in5_V2 Paste over")
epd = epd7in5_V2.EPD()
epd.init()
epd.Clear()


Himage2 = Image.new('1', (epd.width, epd.height), 255)  # 255: clear the frame

plot_image = Image.open(os.path.join(maindir, 'plot_image.bmp'))
logging.info("Paste error message over screen window")

err_img = Image.open(os.path.join(maindir, 'no_wifi.bmp'))

# Draw image in center of screen
err_width, err_height = err_img.size
plot_width, plot_height = plot_image.size
# Calculate the coordinates for the top-left corner to paste in the center
paste_x = int((plot_width - err_width) / 2)
paste_y = int((plot_height - err_height) / 2)


err_img = plot_image.paste(err_img, (paste_x, paste_y))

err_img = plot_image.transpose(Image.ROTATE_180)

epd.display(epd.getbuffer(err_img))

logging.info("EPD Go to Sleep...")
epd.sleep() 