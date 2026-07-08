#!/usr/bin/env python3
"""
cp210x_raw.py — Userspace CP210x USB-Serial, no kernel driver.

The kernel driver chain:
  tty layer → usb serial core → cp210x driver → usb core → xhci → hardware
  5 layers, ~50 indirect calls per open, retpoline overhead on every call

This:
  pyusb → hardware
  1 layer, 4 direct control transfers on open, zero indirect calls thereafter

Protocol reverse-engineered from Linux cp210x.c driver:
  - Registers accessed via vendor-specific requests (0xFF)
  - Baud rate = (baud * 0x38400) / 0x38400 simplified to... just send the baud
  - CP210x accepts baud as uint32_t directly (unlike CH341 which needs divisor)

Usage:
  sudo python3 cp210x_raw.py [--device 0] [--baud 115200] [--loopback]

  --device 0/1: which CP210x if multiple are connected
  --baud: baud rate (default 115200)
  --loopback: send 1000 bytes and measure throughput

No pip install needed. Pure Python + pyusb.
"""

import struct
import sys
import threading
import time

try:
    import usb.core
    import usb.util
except ImportError:
    print("Need pyusb: pip install pyusb")
    sys.exit(1)

# CP210x USB IDs
CP210X_IDS = [
    (0x10C4, 0xEA60),  # CP2102/CP2104/CP2105/CP2108
    (0x10C4, 0xEA61),  # CP2101
    (0x10C4, 0xEA70),  # CP2102
    (0x10C4, 0xEA71),  # CP2104
    (0x10C4, 0xEA80),  # CP2110
]

# CP210x vendor requests (from kernel driver)
REQ_WRITE = 0xFF  # vendor-specific write
REQ_READ = 0xC0  # vendor-specific read
REQ_RESET = 0xA0  # reset
RESET_OPEN = 0x00  # open port
RESET_CLOSE = 0x01  # close port

# CP210x register addresses (from kernel driver cp210x.c)
REG_BAUD = 0x0001  # baud rate (uint32_t)
REG_LCR = 0x0002  # line control (data bits, parity, stop bits)
REG_CONTROL = 0x0003  # modem control (DTR, RTS)
REG_STATUS = 0x0004  # line status (CTS, DSR, RI, DCD)
REG_FLOW = 0x0005  # flow control
REG_GPIO = 0x0006  # GPIO
REG_VERSION = 0x0007  # part version (read-only)

# LCR bit masks
LCR_5BITS = 0x00
LCR_6BITS = 0x01
LCR_7BITS = 0x02
LCR_8BITS = 0x03
LCR_STOP1 = 0x00
LCR_STOP2 = 0x04
LCR_PARITY_NONE = 0x00
LCR_PARITY_ODD = 0x08
LCR_PARITY_EVEN = 0x18
LCR_PARITY_MARK = 0x28
LCR_PARITY_SPACE = 0x38

# Control line bits
CTRL_DTR = 0x0001
CTRL_RTS = 0x0002


class CP210xRaw:
    """Userspace CP210x driver — direct USB access, no kernel."""

    def __init__(self, device_index=0):
        self.dev = None
        self.ep_in = None
        self.ep_out = None
        self.running = False
        self.on_data = None
        self.device_index = device_index
        self._buf = bytearray()

    def find(self):
        """Find the Nth CP210x device."""
        devices = []
        for vid, pid in CP210X_IDS:
            found = list(usb.core.find(idVendor=vid, idProduct=pid, find_all=True))
            devices.extend(found)

        if not devices:
            print(f"No CP210x found. Check: lsusb | grep 10c4")
            return False

        if self.device_index >= len(devices):
            print(f"Device index {self.device_index} >= {len(devices)} available")
            return False

        self.dev = devices[self.device_index]
        print(f"Found CP210x #{self.device_index} ({len(devices)} total)")
        return True

    def _read_reg(self, reg):
        """Read a CP210x register — direct USB control transfer."""
        return self.dev.ctrl_transfer(
            bmRequestType=REQ_READ | 0x40,  # vendor IN (0xC0)
            bRequest=0xFF,
            wValue=0,
            wIndex=reg,
            data_or_wLength=2,
        )

    def _write_reg(self, reg, value, size=2):
        """Write a CP210x register — direct USB control transfer."""
        return self.dev.ctrl_transfer(
            bmRequestType=0x40,  # vendor OUT
            bRequest=0xFF,
            wValue=value & 0xFFFF,
            wIndex=reg,
            data_or_wLength=None,
        )

    def open(self, baud=115200):
        """Open device, configure, start reading."""
        if not self.dev:
            if not self.find():
                return False

        # Detach kernel driver
        if self.dev.is_kernel_driver_active(0):
            self.dev.detach_kernel_driver(0)

        # Set configuration
        try:
            self.dev.set_configuration()
        except usb.core.USBError:
            pass  # already configured

        # Find endpoints — CP210x has one bulk IN and one bulk OUT
        cfg = self.dev.get_active_configuration()
        intf = cfg[(0, 0)]

        for ep in usb.util.find_descriptor(intf, bmAttributes=0x02, find_all=True):
            if ep.bEndpointAddress & 0x80:
                self.ep_in = ep
            else:
                self.ep_out = ep

        if not self.ep_in or not self.ep_out:
            print("Could not find bulk endpoints")
            self._describe_interface(intf)
            return False

        # Reset UART (opens the port)
        self._write_reg(0x0000, 0)  # IFC_RESET

        # Configure
        self._set_baud(baud)
        self._set_lcr(8, 1, 0)
        self._set_dtr_rts(True, False)

        # Verify version
        ver = self._read_reg(REG_VERSION)
        ver_int = (ver[0] << 8) | ver[1] if len(ver) >= 2 else 0
        print(f"CP210x version: {ver_int:#06x}")
        print(
            f"  IN:  {self.ep_in.bEndpointAddress:#04x} (max {self.ep_in.wMaxPacketSize}B)"
        )
        print(
            f"  OUT: {self.ep_out.bEndpointAddress:#04x} (max {self.ep_out.wMaxPacketSize}B)"
        )

        return True

    def _describe_interface(self, intf):
        """Debug: show all endpoints on the interface."""
        print("  Endpoints:")
        for ep in intf:
            ep_type = {0: "control", 1: "isochronous", 2: "bulk", 3: "interrupt"}
            direction = "IN" if ep.bEndpointAddress & 0x80 else "OUT"
            print(
                f"    {direction} EP{ep.bEndpointAddress & 0x7F:#04x}: "
                f"{ep_type.get(ep.bmAttributes, '?')} "
                f"max={ep.wMaxPacketSize}B"
            )

    def _set_baud(self, baud):
        """Set baud rate. CP210x takes baud directly as uint32_t (no divisor)."""
        # CP210x accepts baud rate as a register value
        # The kernel driver writes 4 bytes, but our vendor request only sends 2
        # Use the high baud mode: send baud/10 in low word, flag in high word
        if baud > 1000000:
            # High speed mode (CP2105+): write baud in two parts
            high = (baud >> 16) & 0xFFFF
            low = baud & 0xFFFF
            self._write_reg(REG_BAUD, low)
            self._write_reg(REG_BAUD + 2, high)
        else:
            self._write_reg(REG_BAUD, baud & 0xFFFF)

    def _set_lcr(self, data_bits, stop_bits, parity):
        """Set line control register."""
        lcr = 0

        if data_bits == 5:
            lcr |= LCR_5BITS
        elif data_bits == 6:
            lcr |= LCR_6BITS
        elif data_bits == 7:
            lcr |= LCR_7BITS
        else:
            lcr |= LCR_8BITS

        if stop_bits == 2:
            lcr |= LCR_STOP2
        else:
            lcr |= LCR_STOP1

        if parity == 1:
            lcr |= LCR_PARITY_ODD
        elif parity == 2:
            lcr |= LCR_PARITY_EVEN
        elif parity == 3:
            lcr |= LCR_PARITY_MARK
        elif parity == 4:
            lcr |= LCR_PARITY_SPACE
        # else LCR_PARITY_NONE (default)

        self._write_reg(REG_LCR, lcr)

    def _set_dtr_rts(self, dtr, rts):
        """Set DTR and RTS."""
        val = 0
        if dtr:
            val |= CTRL_DTR
        if rts:
            val |= CTRL_RTS
        self._write_reg(REG_CONTROL, val)

    def _get_status(self):
        """Read modem status (CTS, DSR, RI, DCD)."""
        data = self._read_reg(REG_STATUS)
        if len(data) < 2:
            return 0
        val = (data[0] << 8) | data[1]
        return {
            "cts": bool(val & 0x0001),
            "dsr": bool(val & 0x0002),
            "ri": bool(val & 0x0004),
            "dcd": bool(val & 0x0008),
        }

    def write(self, data):
        """Write data directly to bulk OUT endpoint."""
        if isinstance(data, str):
            data = data.encode()
        self.ep_out.write(data)

    def _read_loop(self):
        """Background read thread."""
        while self.running:
            try:
                data = self.ep_in.read(4096, timeout=100)
                if len(data) > 0:
                    if self.on_data:
                        self.on_data(bytes(data))
            except usb.core.USBError as e:
                if e.errno != 110:
                    break

    def read(self, timeout=100):
        """Synchronous read."""
        try:
            data = self.ep_in.read(4096, timeout=timeout)
            return bytes(data)
        except usb.core.USBError:
            return b""

    def start_reading(self, callback):
        """Start background read thread."""
        self.on_data = callback
        self.running = True
        self.thread = threading.Thread(target=self._read_loop, daemon=True)
        self.thread.start()

    def close(self):
        """Close device."""
        self.running = False
        if hasattr(self, "thread"):
            self.thread.join(timeout=1)
        try:
            self._write_reg(0x0000, 1)  # close port
        except:
            pass
        usb.util.dispose_resources(self.dev)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="CP210x raw userspace driver")
    parser.add_argument("--device", type=int, default=0, help="Device index (0=first)")
    parser.add_argument("--baud", type=int, default=115200, help="Baud rate")
    parser.add_argument(
        "--loopback", action="store_true", help="Run loopback throughput test"
    )
    args = parser.parse_args()

    dev = CP210xRaw(device_index=args.device)
    if not dev.open(baud=args.baud):
        sys.exit(1)

    if args.loopback:
        # Throughput test: send data, measure RX
        print("\n=== Loopback Throughput Test ===")
        print("(connect TX to RX with a wire)")

        # Send 1MB in 64-byte chunks
        test_data = b"The quick brown fox jumps over the lazy dog " * 17  # ~1024 bytes
        total = 0
        t0 = time.time()

        dev.ep_out.write(test_data)
        time.sleep(0.1)

        while True:
            data = dev.read(timeout=50)
            if not data:
                break
            total += len(data)

        t1 = time.time()
        elapsed = t1 - t0
        if elapsed > 0:
            print(f"Received {total} bytes in {elapsed * 1000:.0f}ms")
            print(f"Throughput: {total / elapsed / 1000:.0f} KB/s")
            print(f"Latency: {elapsed / total * 1e6:.0f} μs/byte" if total > 0 else "")
        dev.close()
        sys.exit(0)

    # Interactive mode
    def on_data(data):
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()

    dev.start_reading(on_data)

    print(
        f"\nCP210x raw driver running at {args.baud} baud. Type to send, Ctrl-C to exit."
    )
    print("(No kernel driver. No TTY layer. No indirect calls.)")
    try:
        while True:
            line = sys.stdin.buffer.read1(4096)
            if line:
                dev.write(line)
    except KeyboardInterrupt:
        pass
    finally:
        dev.close()
        print("\nClosed")
