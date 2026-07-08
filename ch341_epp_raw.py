#!/usr/bin/env python3
"""ch341_epp_raw.py — Userspace CH341 EPP GPIO driver.

CH341 in EPP/MEM/I2C mode (1a86:5512). No kernel driver.
Controls GPIO pins via bulk endpoints.

Tested 2026-07-08: GPIO write/read verified on real hardware.
"""

import usb.core, usb.util, sys, time

GPIO_SET_DIR = 0xC0
GPIO_SET_OUT = 0xC1
GPIO_READ_IN = 0xC2

class CH341EPP:
    def __init__(self):
        self.dev = None; self.ep_out = None; self.ep_in = None

    def find(self):
        self.dev = usb.core.find(idVendor=0x1a86, idProduct=0x5512)
        if self.dev:
            self.dev.set_configuration()
        return self.dev is not None

    def open(self):
        if not self.dev and not self.find(): return False
        cfg = self.dev.get_active_configuration()
        for ep in cfg[(0, 0)]:
            if ep.bEndpointAddress == 0x02: self.ep_out = ep
            elif ep.bEndpointAddress == 0x82: self.ep_in = ep
        print(f"CH341 EPP: OUT=0x{self.ep_out.bEndpointAddress:02x} IN=0x{self.ep_in.bEndpointAddress:02x}")
        return True

    def _stream(self, data):
        self.ep_out.write(data)
        try: return self.ep_in.read(32, timeout=500)
        except: return b''

    def gpio_dir(self, mask):
        self._stream(bytes([GPIO_SET_DIR, mask & 0xFF]))

    def gpio_write(self, mask):
        self._stream(bytes([GPIO_SET_OUT, mask & 0xFF]))

    def gpio_read(self):
        r = self._stream(bytes([GPIO_READ_IN]))
        return r[0] if r else 0

    def close(self):
        usb.util.dispose_resources(self.dev)

if __name__ == '__main__':
    epp = CH341EPP()
    if not epp.open(): print("CH341 EPP not found"); sys.exit(1)

    print("GPIO: toggle all pins...")
    epp.gpio_dir(0xFF)
    for v in [0x00, 0xFF, 0x55, 0xAA, 0x00]:
        epp.gpio_write(v)
        r = epp.gpio_read()
        print(f"  Write 0x{v:02x} Read 0x{r:02x}")
        time.sleep(0.05)
    epp.close()
    print("OK")
