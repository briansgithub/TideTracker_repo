#!/usr/bin/python
# -*- coding:utf-8 -*-

import sys
import os
import logging
from PIL import Image

maindir = os.path.dirname(os.path.realpath(__file__))

logging.info("epd7in5_V2 Paste over")

logging.info("Paste error message over screen window")
plot_image = Image.open(os.path.join(maindir, 'error_image.bmp')).convert("RGB")
plot_image = plot_image.transpose(Image.ROTATE_180)

error_image = Image.open(os.path.join(maindir, 'no_wifi.bmp')).convert("RGB")
error_image = error_image.transpose(Image.ROTATE_180)

# Draw image in the center of the screen
err_width, err_height = error_image.size
plot_width, plot_height = plot_image.size

# Calculate the coordinates for the top-left corner to paste in the center
paste_x = int((plot_width - err_width) / 2)
paste_y = int((plot_height - err_height) / 2)

plot_image.paste(error_image, (paste_x, paste_y))
plot_image.show()
