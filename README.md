# better-usb-drivers

Userspace USB-serial drivers. No kernel module. No TTY layer. No ops tables.

The kernel goes through 5 layers. This goes: libusb -> hardware.

## Drivers

| Chip | File | Lang | Status |
|------|------|------|--------|
| CP2102/CP2104 | cp210x_raw.c | C | Working |
| CP2102/CP2104 | cp210x_raw.py | Python | Working |
| CH340/CH341 | ch34x_raw.py | Python | Working (EPP mode) |

## Quick start

```
sudo apt install libusb-1.0-0-dev
gcc -O3 -march=native -o cp210x_raw cp210x_raw.c -lusb-1.0
sudo ./cp210x_raw 0 115200
```

## Why

Kernel chain: tty -> usb-serial-core -> driver -> usb-core -> xhci
This chain: libusb -> hardware

No indirect calls. No retpoline. No abstraction layers.

## Protocol

### CP210x
- Write: vendor request 0xFF, value=register, index=address
- Baud: uint32_t at register 0x0001
- Endpoints: EP1 IN (0x81), EP1 OUT (0x01)

### CH340/CH341
- Write: vendor request 0x9A, value=encoded, index=register
- Divisor: 0x2DC6C00 / (baud x factor)
- Factors: 4, 32, 256, or 2048
