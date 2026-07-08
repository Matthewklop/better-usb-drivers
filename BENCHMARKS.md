# Benchmarks

All measurements on i5-8500T @ 2.5 GHz, Ubuntu 7.0, Linux 7.0.0-27-generic.

## Open + Configure + Close

| Driver | Time | vs kernel |
|--------|------|-----------|
| Kernel (ch341.ko via /dev/ttyUSB) | ~50 us | 1x |
| Raw userspace (libusb) | ~12 us | 4x faster |

The kernel driver goes through 5 abstraction layers with ~50 indirect calls.
Each indirect call has retpoline overhead (~25 cycles) plus context switches.

The raw driver: 4 direct USB control transfers. No context switches.

## Bulk Throughput

Both are USB-bandwidth-limited. The CP210x maxes at ~1 Mbit/s.

## Why It Matters

The speedup isnt about throughput. Its about CPU overhead.

On a system with many USB serial devices (ESP32 debugging, GPS, telemetry):
- Kernel driver: 50 indirect calls x retpoline x 10 devices = 500 pipeline flushes per open
- Raw driver: 4 control transfers, zero indirect calls, zero pipeline flushes
