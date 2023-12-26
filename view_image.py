#!/usr/bin/python
# -*- coding:utf-8 -*-

import sys
import os
from PIL import Image
import logging

libdir = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'e-ink_lib')
maindir = os.path.dirname(os.path.realpath(__file__))
if os.path.exists(libdir):
    sys.path.append(libdir)

from waveshare_epd import epd7in5_V2

def main(image_path):
    logging.info("epd7in5_V2 Paste over")
    
    epd = epd7in5_V2.EPD()
    epd.init()
    ###epd.Clear()

    image_bmp = Image.open(image_path)
    image_bmp = image_bmp.transpose(Image.ROTATE_180)
    epd.display(epd.getbuffer(image_bmp))

    logging.info("EPD Go to Sleep...")
    epd.sleep()

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python script.py <image_path>")
        sys.exit(1)

    image_path = sys.argv[1]
    
    if not os.path.exists(image_path):
        print(f"Error: Image file '{image_path}' not found.")
        sys.exit(1)

    main(image_path)
