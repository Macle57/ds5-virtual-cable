# ds5-virtual-usb

Software-only "virtual wire" for the DualSense: bridges a Bluetooth-connected DualSense into an emulated **wired USB DualSense** on Windows — full HD haptics, adaptive triggers, speaker/headphone audio, and mic, with games auto-detecting it as wired.

- `docs/FINDINGS.md` — reverse-engineered BT protocol (report layouts, Opus/haptic codec params, USB identity)
- `docs/ARCHITECTURE.md` — design + phased plan
- `prototype/` — Phase 1 user-mode BT bridge core

Reference implementations this builds on: [daidr/dualsense-tester](https://github.com/daidr/dualsense-tester), [awalol/DS5Dongle](https://github.com/awalol/DS5Dongle).
