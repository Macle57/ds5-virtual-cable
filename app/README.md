# `app/` — the product layer

`emulator/ds5emu` is the USB/IP device and the Bluetooth backend. `app/ds5app`
is everything that turns those into something a person can run: one command
instead of two terminals, teardown that survives being crashed, a tray icon,
and a packaged exe.

Written in **Phase 4a**. Nothing about the USB or Bluetooth protocol lives here.

```
ds5app/
  usbip.py      find usbip.exe (registry -> Program Files -> PATH); attach,
                detach, `attach -X`, and `usbip port` PARSED FOR OWNERSHIP
  controller.py find the Bluetooth DualSense, prove it is alive, read battery
  service.py    BridgeService: the bring-up and tear-down order, the instance
                mutex, the crash hooks. THE one code path.
  cli.py        `ds5bridge` -- run / devices / cleanup / doctor / tray
  tray.py       pystray front end over the same BridgeService
tests/          hardware-free (the `usbip port` parser)
tools/          dev_tray.ps1 (run the tray from source), plus the hardware
                tests -- launcher lifecycle, reconnect, long soak
packaging/      PyInstaller spec + build.ps1
```

## Run it

Nothing here is installed and nothing has to be built. The tray you get from
`dev_tray.ps1` is the same tray the packaged exe shows -- same process, same
icon, same menu -- so the edit/run loop never has to go through PyInstaller:

```powershell
powershell -File app\tools\dev_tray.ps1              # console window + live logs
powershell -File app\tools\dev_tray.ps1 -Windowed    # no console, like ds5bridge-tray.exe
powershell -File app\tools\dev_tray.ps1 -Isolated    # scratch DS5_CONFIG, not your real settings
powershell -File app\tools\dev_tray.ps1 -Stop        # stop it, cleanly
```

Every start replaces the running one, and `-Stop` tears down through the
service rather than killing it, which is what gives a hidden Bluetooth pad back.
Do not stop it from Task Manager.

The commands it wraps, and everything else:

```powershell
cd D:\Codes\dualSense\ds5-virtual-usb\app
..\prototype\.venv\Scripts\python.exe -m ds5app                 # bridge, Ctrl+C to stop
..\prototype\.venv\Scripts\python.exe -m ds5app devices
..\prototype\.venv\Scripts\python.exe -m ds5app doctor
..\prototype\.venv\Scripts\python.exe -m ds5app cleanup
..\prototype\.venv\Scripts\python.exe -m ds5app tray --autostart
..\prototype\.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

End-user instructions are `docs/USER-GUIDE.md`. Build the exe with
`powershell -File app\packaging\build.ps1`.

## The five things that are easy to get wrong

Each one cost a real debugging session in Phase 4a. They are the reason this
layer exists as code rather than as a paragraph in a README.

1. **`usbip attach` arms a background auto-re-attach, and a hard kill does not
   disarm it.** After `taskkill /F` both `usbip port` and TCP 3241 come back
   empty — the driver detaches when the socket dies — but start any server on
   that port and a device appears that nobody asked for, plus a second one from
   your own attach. Nothing about the state of the machine reveals this
   beforehand, so `attach -X` runs **unconditionally at every start**.

2. **`usbip port` is a machine-wide table.** "Is anything attached?" is not the
   same question as "is anything of *mine* attached?". Treating them as the same
   made stale-state cleanup detach another bridge's device and killed a running
   30-minute soak six minutes in. `Usbip.our_ports()` filters on the
   `-> usbip://host:port/busid` line each entry carries; every cleanup and
   teardown path uses it.

3. **A second launch cannot be told from a crashed first one by looking at the
   port.** Port held + device attached reads exactly like "leftovers", so the
   second instance's auto-cleanup used to kill the healthy first one. A named
   mutex (`Local\ds5bridge-<port>`) settles it before anything looks at the port.

4. **Teardown needs three different hooks.** atexit for a normal exit or an
   unhandled exception; SIGINT/SIGTERM/SIGBREAK for Ctrl+C; and
   `SetConsoleCtrlHandler` — the only one that fires when the console window's X
   is clicked, and without it a closed terminal left the device attached. The
   tray needs a fourth: `raise KeyboardInterrupt` does not escape pystray's
   Win32 message loop, so `service.ON_TEARDOWN` carries a hook that stops the
   icon.

5. **`Server.wait_closed()` can hang forever.** When the driver detaches it
   *resets* the TCP connection; the proactor transport raises
   ConnectionResetError from inside `_call_connection_lost`, the transport's
   closed-future is never resolved, and both `writer.wait_closed()` and the
   `Server.wait_closed()` waiting on it wait for ever. Both are bounded at 1 s
   in `ds5emu/server.py`. Teardown went from 15.1 s (a caller's timeout) to
   2.1 s.

## Design notes

**The server runs in-process**, on its own asyncio thread, not as a child. That
deletes the whole class of failure `docs/STATUS.md` §17.8 traps 7 and 8 describe
— a stale emulator whose command line cannot be read, or one that cannot be
killed from a later shell — and it lets the CLI and the tray share one
`BridgeService` and one set of counters.

**`snapshot()` uses O(1) counters only.** No `UrbMeter.summary()`, which sorts;
sorting holds the GIL, and the GIL is what the isochronous endpoints need
released every millisecond (§17.8 trap 10). The report rate is differenced
between two calls rather than computed over a window.

**Nothing here installs a driver.** usbip-win2 is the user's decision, made
once, with the release pinned to 0.9.7.7 and 0.9.7.8 explicitly warned against.
When it is absent, `usbip.MISSING_MESSAGE` says which release, where, and why.

## Hardware tests

All three need the Bluetooth controller and usbip-win2. Each prints
`RESULT: PASS` or `FAIL` and exits accordingly.

```powershell
..\prototype\.venv\Scripts\python.exe tools\launcher_test.py --serial d42f4ba1485d
..\prototype\.venv\Scripts\python.exe tools\reconnect_test.py --serial d42f4ba1485d --cycles 3
..\prototype\.venv\Scripts\python.exe tools\soak.py --serial d42f4ba1485d --minutes 30 --jsonl C:\Temp\soak.jsonl
```

`launcher_test.py` covers Ctrl+C, hard kill then restart, `cleanup`, and a
double start, each ending by asserting `usbip port` is empty and the TCP port is
free. `reconnect_test.py` forces a link loss with the device attached and a
reader on it. `soak.py` runs the real service for as long as you ask, logging
rate, gaps and battery every minute — `--audio` exists but costs ~2.5 %/min of
battery, so a long audio soak is not something a battery can do.
