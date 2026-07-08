#!/usr/bin/env python3
"""
ch34x_raw.py — Userspace CH340/341 USB-Serial, no kernel driver needed.

The kernel driver (ch341.ko) goes through:
  tty layer → usb serial core → ch341 driver → usb core → xhci → hardware

That's 5 abstraction layers. Each with ops tables = indirect calls.

This goes:
  pyusb → hardware

That's 1 abstraction layer. Zero indirect calls.

Required: pip install pyusb
Run: sudo python3 ch34x_raw.py [baud=115200] [port=/dev/ttyUSB_raw0]
"""

import struct
import sys
import threading

try:
    import usb.core
    import usb.util
except ImportError:
    print("Need pyusb: pip install pyusb")
    print("Also need libusb: sudo apt install libusb-1.0-0-dev")
    sys.exit(1)

# CH340/341 USB IDs
CH340_IDS = [
    (0x1A86, 0x7523),  # CH340
    (0x1A86, 0x5523),  # CH341
    (0x1A86, 0x7522),  # CH340 (variant)
    (0x4348, 0x5523),  # CH341 (clone)
    (0x9986, 0x7523),  # CH340 (clone)
]

# USB request types
CH341_REQ = 0x9A  # vendor-specific control request

# CH341 register addresses (from decompiled driver)
REG_LCR = 0x1312  # line control register
REG_BAUD = 0x1312  # baud rate divisor (same addr, different encoding)
REG_CONTROL = 0x2518  # modem control (DTR/RTS)
REG_STATUS = 0x2519  # line status (CTS/DSR/RI/DCD)
REG_EXTRA = 0x2518 + 0x100  # for 8+ data bits variant


class CH341Raw:
    """Userspace CH341 driver — NO kernel module needed."""

    def __init__(self, baud=115200):
        self.dev = None
        self.ep_in = None
        self.ep_out = None
        self.running = False
        self.on_data = None  # callback for received data
        self.baud = baud

    def find(self):
        """Find first CH340/341 device."""
        for vid, pid in CH340_IDS:
            dev = usb.core.find(idVendor=vid, idProduct=pid)
            if dev:
                self.dev = dev
                print(f"Found CH34x: {vid:04x}:{pid:04x}")
                return True
        print("No CH340/341 found")
        return False

    def open(self):
        """Open device, configure, start reading."""
        if not self.dev:
            if not self.find():
                return False

        # Detach kernel driver if active
        if self.dev.is_kernel_driver_active(0):
            self.dev.detach_kernel_driver(0)
            print("Detached kernel driver")

        # Set configuration
        self.dev.set_configuration()

        # Find endpoints
        cfg = self.dev.get_active_configuration()
        intf = cfg[(0, 0)]
        for ep in usb.util.find_descriptor(intf, bmAttributes=0x02, find_all=True):
            if ep.bEndpointAddress & 0x80:
                self.ep_in = ep
            else:
                self.ep_out = ep

        if not self.ep_in or not self.ep_out:
            print("Could not find endpoints")
            return False

        print(f"IN: {self.ep_in.bEndpointAddress:#x}")
        print(f"OUT: {self.ep_out.bEndpointAddress:#x}")

        # Initialize — configure baud rate and line settings
        self._set_baud(self.baud)
        self._set_lcr(8, 1, 0)  # 8N1
        self._set_dtr_rts(True, False)

        return True

    def _ctrl_out(self, request, value, index):
        """Send control message — ONE USB transfer, no kernel indirection."""
        return self.dev.ctrl_transfer(
            bmRequestType=0x40,  # vendor OUT
            bRequest=request,
            wValue=value,
            wIndex=index,
            data_or_wLength=None,
        )

    def _set_baud(self, baud):
        """Set baud rate — from decompiled ch341_set_baudrate_lcr.

        The divisor calculation (from MIPS decompile):
          factor = 0x2DC6C0  (48MHz / 20?)
          if baud > 0x5B8D:   factor = 4 * baud
          elif baud > 0xB71:  factor = 32 * baud
          elif baud > 0x16E:  factor = 256 * baud
          else:               factor = 2048 * baud
          divisor = 0x2DC6C00 / factor
        """
        if baud <= 0:
            return

        # Calculate divisor — matching the decompiled algorithm
        # 0x2DC6C0 = 3,000,000 (CH341 base clock?)
        base = 0x2DC6C0
        if baud > 0x5B8D:  # > 23437
            factor = baud * 4
            ps = 4
        elif baud > 0xB71:  # > 2929
            factor = baud * 32
            ps = 32
        elif baud > 0x16E:  # > 366
            factor = baud * 256
            ps = 256
        else:
            factor = baud * 2048
            ps = 2048

        divisor = (0x2DC6C00 // factor) & 0xFF
        lcr = 0  # default LCR value

        # Pack: low byte = divisor, high byte = LCR control bits
        value = divisor | (lcr << 8)
        self._ctrl_out(CH341_REQ, value, REG_LCR)

    def _set_lcr(self, data_bits, stop_bits, parity):
        """Set line control register."""
        lcr = 0x80  # enable divisor latch?
        if data_bits == 7:
            lcr |= 0x02
        elif data_bits == 8:
            lcr |= 0x03
        if stop_bits == 2:
            lcr |= 0x04
        if parity:
            lcr |= 0x08

        # Send LCR config
        self._ctrl_out(CH341_REQ, 0, REG_LCR)

        # If 8+ data bits, send extra config (from decompiled driver)
        if data_bits > 7:
            self._ctrl_out(CH341_REQ, lcr & 0xFF, REG_EXTRA)

    def _set_dtr_rts(self, dtr, rts):
        """Set DTR and RTS lines."""
        value = 0
        if dtr:
            value |= 0x01
        if rts:
            value |= 0x02
        self._ctrl_out(CH341_REQ, value, REG_CONTROL)

    def write(self, data):
        """Write data — direct bulk OUT, no TTY layer overhead."""
        self.ep_out.write(data)

    def _read_loop(self):
        """Background read loop."""
        while self.running:
            try:
                data = self.ep_in.read(4096, timeout=100)
                if len(data) > 0 and self.on_data:
                    self.on_data(bytes(data))
            except usb.core.USBError as e:
                if e.errno != 110:  # timeout is fine
                    print(f"Read error: {e}")
                    break

    def start_reading(self, callback):
        """Start background read thread."""
        self.on_data = callback
        self.running = True
        self.read_thread = threading.Thread(target=self._read_loop, daemon=True)
        self.read_thread.start()

    def close(self):
        """Close and allow kernel driver to reattach."""
        self.running = False
        if self.read_thread:
            self.read_thread.join(timeout=1)
        usb.util.dispose_resources(self.dev)
        # Reattach kernel driver
        try:
            self.dev.attach_kernel_driver(0)
        except:
            pass


if __name__ == "__main__":
    import time

    ch = CH341Raw(baud=115200)
    if not ch.open():
        sys.exit(1)

    def on_data(data):
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()

    ch.start_reading(on_data)

    print("\nCH341 raw driver running. Type to send, Ctrl-C to exit.")
    try:
        while True:
            line = sys.stdin.buffer.read1(1024)
            if line:
                ch.write(line)
    except KeyboardInterrupt:
        pass
    finally:
        ch.close()
        print("\nClosed")
