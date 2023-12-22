#   This is the definition of the epd sleep function 
#   Defined in 
#   TideTracker_repo\e-ink_demo_code\lib\waveshare_epd\epd7in5_V2.py

    def sleep(self):
        self.send_command(0x02) # POWER_OFF
        self.ReadBusy()
        
        self.send_command(0x07) # DEEP_SLEEP
        self.send_data(0XA5)
        
        epdconfig.delay_ms(2000)
        epdconfig.module_exit()