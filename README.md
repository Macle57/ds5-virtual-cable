# ds5-virtual-usb

Software-only "virtual wire" for the DualSense: bridges a Bluetooth-connected DualSense into an emulated **wired USB DualSense** on Windows — full HD haptics, adaptive triggers, speaker/headphone audio, and mic, with games auto-detecting it as wired.

**Start with [`docs/STATUS.md`](docs/STATUS.md)** — it is the handoff document:
what is verified on hardware (with evidence), what is implemented but untested,
every gotcha, how to run everything, and the open questions.

- `docs/STATUS.md` — current state, handoff notes, CLI reference
- `docs/FINDINGS.md` — reverse-engineered BT protocol (report layouts, Opus/haptic codec params, USB identity)
- `docs/usb-ground-truth.md` — real USB descriptors read off the wired controller
- `docs/ARCHITECTURE.md` — design + phased plan
- `prototype/ds5bridge/` — Phase 1 user-mode BT bridge core (`python -m ds5bridge`)
- `prototype/tools/` — descriptor dumpers and closed-loop audio/haptic verification harnesses

Phases 0 and 1 are complete and hardware-verified: input decode at 476 Hz over BT,
SetState with proven CRC32 enforcement, WAV → speaker (+56.7 dB confirmed by FFT
loopback through the controller's own mic), HD haptics (+25.8 dB at the commanded
frequency), and mic capture at 99.4 frames/s. The controller's 45 kHz audio
consumption rate is measured, not assumed.

Reference implementations this builds on: [daidr/dualsense-tester](https://github.com/daidr/dualsense-tester), [awalol/DS5Dongle](https://github.com/awalol/DS5Dongle).
