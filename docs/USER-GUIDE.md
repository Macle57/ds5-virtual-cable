# ds5bridge — user guide

**Your DualSense over Bluetooth, but games see a wired one.**

Some PC games only give you the good DualSense features — adaptive triggers, HD
haptics, the speaker, the touchpad, the light bar — when the controller is
plugged in with a USB cable. Over Bluetooth they fall back to a plain gamepad,
or ignore it entirely.

`ds5bridge` fixes that without a cable. It reads your controller over Bluetooth
and shows Windows a **wired DualSense that does not physically exist**. Games
see the wired device and turn everything on. You stay on the sofa.

Verified on *Spider-Man: Miles Morales* (Sony's PC port): every DualSense
feature worked immediately, with no configuration.

---

## What you need

| | |
|---|---|
| Windows | 10 or 11, 64-bit |
| A DualSense | PS5 controller, paired to this PC over Bluetooth |
| usbip-win2 | a free, Microsoft-signed driver — installed once, see below |
| Charge | keep the controller above ~20 %. Below 15 % it gets unreliable in ways that look like software bugs. |

You do **not** need to turn on test signing, disable Secure Boot, or reboot.

---

## Install — two steps, once

### Step 1: install usbip-win2 (the driver)

This is the piece that lets a program present a virtual USB device. It is not
part of `ds5bridge` and `ds5bridge` will not install it for you — a driver is
not something a game utility should push onto your machine behind your back.

1. Go to **https://github.com/vadimgrn/usbip-win2/releases/tag/v0.9.7.7**
2. Download **`USBip-0.9.7.7-x64.exe`**.
3. Run it and click through. It installs two Microsoft-signed drivers.

> **Use 0.9.7.7. Do not use 0.9.7.8** — its own maintainer warns that release
> can corrupt memory and crash Windows.

Two things to know about the install:

* **All your USB 3.0 hubs restart during it.** USB devices will blink out and
  come back. Do not do this while something is writing to a USB drive.
* It may say a reboot is required. On the development machine it worked
  immediately without one. If something behaves strangely later, reboot before
  assuming it is broken.

### Step 2: get ds5bridge

Unzip the `ds5bridge` folder anywhere — Desktop is fine. Inside:

| file | what it is |
|---|---|
| `ds5bridge.exe` | the command-line version — a window with a live status line |
| `ds5bridge-tray.exe` | the same thing as an icon next to the clock |

Windows will probably show **"Windows protected your PC"** the first time.
That is SmartScreen reacting to a program it has never seen, not a virus
warning — this build is not code-signed. Click **More info → Run anyway** if you
trust where you got it from. (If you would rather not, run it from source
instead — see `emulator/README.md`.)

Check your setup before anything else:

```
ds5bridge.exe doctor
```

```
[ok]   usbip.exe  C:\Program Files\USBip\usbip.exe
       version    0.9.7.7
[ok]   nothing attached
[ok]   TCP 3241 free
[ok]   controller d42f4ba1485d  battery 70%

no problems found
```

---

## Pair the controller (if you have not already)

1. Controller **off**. Hold **CREATE** (the little button left of the touchpad)
   and **PS** together until the light bar flashes twice a second.
2. On the PC: **Settings → Bluetooth & devices → Add device → Bluetooth**.
3. Pick **Wireless Controller**.

**Take the USB cable out.** A DualSense on a cable switches its Bluetooth radio
off. It still shows up in the paired-device list, which is exactly why
`ds5bridge devices` marks it — see the troubleshooting table.

---

## Using it

### The simple way

Double-click **`ds5bridge.exe`**. That is the whole thing:

```
  ds5bridge -- a Bluetooth DualSense, presented to Windows as a wired one

  -  usbip 0.9.7.7 at C:\Program Files\USBip\usbip.exe
  -  controller d42f4ba1485d  battery 70% (discharging), firmware Sep 18 2025 13:15:28
  -  battery 70%
  -  attaching the virtual controller ...
  *  virtual wired DualSense attached (controller d42f4ba1485d)

  Windows now sees a wired DualSense. Start your game.
  Press Ctrl+C here to stop and put everything back.

  running  d42f4ba1485d  battery 70%  250 reports/s  up 30s
```

Start your game. It sees a wired DualSense.

**To stop: press Ctrl+C in that window, or just close it.** Either one tears
everything down properly. So does the tray's Quit, and so does the PC shutting
down. Leave it running as long as you like.

### The tray way

Double-click **`ds5bridge-tray.exe`**. An icon appears next to the clock:

| colour | meaning |
|---|---|
| grey | stopped |
| blue | starting |
| green | running — a game will see the controller |
| amber | the controller went away; the virtual one is still there, waiting |
| red | something failed — hover for the reason |

The little bar across the bottom of the icon is the battery, and it turns red
below 20 %. Hover for controller address, battery and report rate. Right-click
for **Start bridging / Stop bridging / Quit**.

`ds5bridge-tray.exe --autostart` starts bridging the moment it launches — handy
in your Startup folder.

### If you have two controllers

```
ds5bridge.exe devices
```

```
2 controller(s) enumerated over Bluetooth:

   d42f4ba1485d  battery 70% (discharging), firmware Sep 18 2025 13:15:28
 x a0fa9c0dd8bb  -- not responding (read error)
```

With more than one live, `ds5bridge` will not guess:

```
ds5bridge.exe --serial d42f4ba1485d
```

Use the address, never "the first one" — the order changes between sessions.

---

## What actually happens to your controller

* **Nothing permanent.** No firmware is touched, the pairing is never rewritten,
  and no setting on the controller is changed. The program deliberately refuses
  to forward the kinds of commands that could do any of that.
* **Nothing is installed** by `ds5bridge` itself. It uses the usbip-win2 driver
  you installed in step 1 and one local network port (3241, loopback only —
  nothing leaves your PC).
* The `usbipd` service some people have for WSL is **never touched**. It owns a
  different port.
* When you stop, the virtual device disappears and the machine is exactly as it
  was.

Battery cost: input-only bridging is cheap. Driving the speaker and haptics
continuously is not — measured at roughly **2.5 % of battery per minute** with
both actuators going flat out, so a long session with a lot of haptics will
drain faster than a cable ever would. Charge between sessions.

---

## Troubleshooting

| what you see | what it means | what to do |
|---|---|---|
| **"no DualSense found over Bluetooth"** | not paired, or the controller is off | Hold CREATE+PS until the light bar flashes, then pair it in Windows Settings. |
| **"is enumerated but not responding"**, or an `x` in `devices` | **the controller is on a USB cable.** A cabled DualSense turns its Bluetooth radio off but leaves a ghost entry behind | Unplug the cable. Then press PS to wake it over Bluetooth. |
| **"More than one Bluetooth DualSense is connected"** | two live controllers, and guessing would be wrong | Run `ds5bridge devices`, then `ds5bridge --serial <address>`. |
| **Random dropouts, timeouts, weird stutters** | **check the battery first.** Below ~15 % a DualSense produces failures that look exactly like software bugs | `ds5bridge devices` shows the level. Charge it. This has fooled every phase of this project at least once. |
| **"usbip-win2 is not installed"** | step 1 was skipped, or it went somewhere unusual | Install 0.9.7.7 from the link above. If it is installed somewhere odd, `ds5bridge --usbip "D:\path\to\usbip.exe"`. |
| **"TCP 3241 held by PID..."** | a previous run crashed and its server is still alive | `ds5bridge cleanup`. It kills the leftover and detaches anything stale. |
| **"left over from a previous run ... cleaning up"** | same thing, and it already fixed itself | Nothing. It is telling you, not asking. |
| **"ds5bridge is already running"** | you launched it twice | Use the one that is already running. Two would fight over one controller. |
| **The game does not see a controller** | usually the game was started first, or it is using a different input backend | Stop the game, make sure the status line says `running`, start the game again. Also check Windows **Settings → Bluetooth & devices → Devices** shows "Wireless Controller". |
| **The game sees TWO controllers** | the real Bluetooth one *and* the virtual wired one | Expected — Windows sees both. Most games take the wired one. If yours does not, unpair the Bluetooth controller from the *game's* settings, not from Windows. |
| **Controller works, but no haptics or triggers** | the game is using the Bluetooth device, not the virtual wired one | See the row above. |
| **Sound comes out of the controller speaker when you did not want it** | Windows picked the virtual DualSense as the default playback device | **Settings → System → Sound → Output**, choose your usual speakers. |
| **"Windows protected your PC"** | SmartScreen; the build is not code-signed | More info → Run anyway, if you trust the source. |
| **Everything looks stuck after a crash** | leftovers | `ds5bridge cleanup`. It is safe to run any time, twice, or when nothing is up. |

### If the controller switches off mid-game

The virtual wired controller **stays plugged in** and starts reporting a
completely neutral controller — sticks centred, nothing pressed — within one
second. Your game does not see a controller being unplugged, it just sees
somebody who stopped playing. Turn the controller back on and it picks up
again by itself, typically well under a second later. Nothing to click.

(That is deliberate. Making the virtual device disappear instead would drop most
games straight to a "please reconnect your controller" screen, and some of them
do not recover cleanly when it comes back.)

---

## Commands

```
ds5bridge                      start bridging (Ctrl+C to stop)
ds5bridge --serial d42f4ba1485d   ...using that controller specifically
ds5bridge devices              list controllers and battery levels
ds5bridge doctor               check this machine's setup
ds5bridge cleanup              undo a run that crashed
ds5bridge tray                 run with the tray icon
ds5bridge tray --autostart     ...and start bridging immediately
ds5bridge --version
```

Useful extras: `--status-every 5` (more frequent status lines), `--port 3242`
(if something else wants 3241), `--audio-target headphone` (route audio to the
controller's 3.5 mm jack instead of its speaker — untested), `-v` (verbose logs
when reporting a problem).

---

## Known limits

* **Not signed.** SmartScreen will warn on first run. Nothing to do about that
  without a code-signing certificate.
* **One controller at a time** per instance.
* **The headphone jack route is untested.** `--audio-target headphone` exists
  but nothing has ever been plugged in to check it.
* **Long sessions with heavy audio drain the battery fast** — see above.
* Tested on one machine, one Windows 11 build, one game. It is very likely fine
  elsewhere; it has not been *proven* fine elsewhere.

If you hit something not in the table, run with `-v`, keep the output, and
include your `ds5bridge doctor` result.
