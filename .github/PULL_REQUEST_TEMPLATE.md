<!-- Keep this short. The important part is the last section. -->

## What this changes

## Why

## How it was verified

<!--
  House style: say what you measured, not that it "works". For example --
    "Unit tests: 173, OK (skipped=41) on a bare Python; 173 OK with the venv."
    "Ran tools/bridge_input_soak.py --seconds 15: 249.9/s, 0 discontinuities,
     3749/3749 field parity, battery 85% before and after."
  If it touches pacing, timing or instrumentation, please say so explicitly --
  docs/STATUS.md 17.4 explains why that area breaks in ways tests do not catch.
-->

- [ ] `cd emulator; python -m unittest discover -s tests -t .` passes
- [ ] `emulator/ds5emu/` still imports stdlib only (`python tools/check_stdlib_only.py`)
- [ ] If a new trap was found, it is written down in `docs/STATUS.md` with the
      measurement that found it
