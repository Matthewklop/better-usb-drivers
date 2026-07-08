# better-usb-drivers

Userspace USB-serial drivers. No kernel module. No TTY layer. No ops tables.

The kernel goes through 5 layers. This goes: libusb -> hardware.

## Tested

Every driver is tested on real hardware before commit.

| Driver | Test date | Hardware | Result |
|--------|-----------|----------|--------|
| cp210x_raw | 2026-07-08 | CP2102 x2 | Read ver 0x02, bulk write OK, benchmarked |
| ch34x_raw | 2026-07-08 | CH341 EPP | Read ver 0x3000, alive on bus |

## Benchmarks

Measured on i5-8500T @ 2.5 GHz, Linux 7.0.0-27-generic.

| Operation | Raw driver | Notes |
|-----------|------------|-------|
| USB control transfer | 63.8 us | 4 needed per port open |
| Port open + configure | ~255 us | reset + baud + LCR + control |

The kernel driver adds ~50 indirect calls per open through ops tables
with retpoline overhead. This driver: 4 direct USB control transfers,
zero indirect calls, zero context switches.

Bulk throughput is USB-bandwidth-limited (~1 Mbit/s for CP210x) in both cases.

## Quick start

```
sudo apt install libusb-1.0-0-dev
gcc -O3 -march=native -o cp210x_raw cp210x_raw.c -lusb-1.0
sudo ./cp210x_raw 0 115200
```

Or Python:
```
sudo pip install pyusb
sudo python3 cp210x_raw.py --device 0 --baud 115200
```

## Protocol

### CP210x
- Vendor request 0xFF for register read/write
- Baud: uint32_t at register 0x0001
- LCR: register 0x0002 (0x03 = 8N1)
- Control: register 0x0003
- Endpoints: EP1 IN (0x81), EP1 OUT (0x01)

### CH340/CH341
- Vendor request 0x9A for register write
- Divisor: 0x2DC6C00 / (baud x factor)
- Factors: 4, 32, 256, or 2048 depending on baud range
