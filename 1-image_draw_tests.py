from PIL import Image, ImageDraw, ImageFont
import os #to delete output_image.png
import time

# Assuming you have the font file 'path/to/your/font.ttf'
font_path = r'D:\Users\Brian\Desktop\W\Programming_write\Python_workspaces\Tide Project tests\TideTracker_repo\e-ink_demo_code\examples\pic\Font.ttc'
font_size = 24
font24 = ImageFont.truetype(font_path, font_size)

# Assuming you have an 'epd' object with width and height attributes
# Replace 'epd.width' and 'epd.height' with the actual dimensions
epd_width = 640  # Replace with actual width
epd_height = 384  # Replace with actual height

Himage = Image.new('1', (epd_width, epd_height), 255)  # 255: clear the frame
draw = ImageDraw.Draw(Himage)

draw.text((10, 0), 'hello world', font=font24, fill=0)
draw.text((10, 20), '7.5inch e-Paper', font=font24, fill=0)
draw.text((150, 0), u'微雪电子', font=font24, fill=0)
draw.line((20, 50, 70, 100), fill=0)
'''draw.line((70, 50, 20, 100), fill=0)
draw.rectangle((20, 50, 70, 100), outline=0)
draw.line((165, 50, 165, 100), fill=0)
draw.line((140, 75, 190, 75), fill=0)
draw.arc((140, 50, 190, 100), 0, 360, fill=0)
draw.rectangle((80, 50, 130, 100), fill=0)
draw.chord((200, 50, 250, 100), 0, 360, fill=0)
'''

# Save the image to a file (optional)
Himage.save('output_image.png')

# Display the image (optional, depending on your environment)
Himage.show()

# Delete the output.png file
os.remove('output_image.png')

time.sleep(1)


