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
| usbip-win2 | a free, Microsoft-signed driver — the installer puts it there |
| HidHide | optional, also free and signed — only "hide the Bluetooth pad while bridged" needs it; the installer includes it |
| Charge | keep the controller above ~20 %. Below 15 % it gets unreliable in ways that look like software bugs. |

You do **not** need to turn on test signing or disable Secure Boot. One reboot
after the install is needed before HidHide's optional hiding works; everything
else works at once.

---

## Install

### The installer (recommended)

1. Download **`ds5bridge-setup-<version>-bundled.exe`** from the
   [releases page](https://github.com/Macle57/ds5-virtual-cable/releases/latest).
   Everything is inside it — the app, usbip-win2 0.9.7.7 and HidHide 1.5.230 —
   so nothing is downloaded during the install and it works offline.
   (`ds5bridge-setup-<version>.exe` without `-bundled` is the same installer
   that downloads the two driver packages from their vendors as it goes,
   SHA-256-checked: 34 MB smaller, otherwise identical.)
2. Run it. Windows will probably show **"Windows protected your PC"**. That is
   SmartScreen reacting to a program it has never seen, not a virus warning —
   nothing here is code-signed yet. Click **More info → Run anyway**.
3. One UAC prompt (the drivers need it; the app never does). Leave every box
   ticked. It creates a System Restore point, installs usbip-win2 (**your USB
   3.0 devices blink out and come back** — do not run it while something is
   writing to a USB drive), installs HidHide, installs ds5bridge to
   `%LOCALAPPDATA%\ds5bridge` with a Start Menu shortcut, and ends on an
   *Installation check* page listing what it verified.
4. **Reboot once** when it is done: HidHide's driver only starts hiding after
   the next start of Windows. Everything else works immediately.

Re-running the installer on a machine that already has it all only refreshes
the app. What each checkbox does, the silent switches and the uninstaller's
options are in [docs/installer.md](installer.md).

### Or: one line of PowerShell

The same steps, scripted, for people who would rather read what runs. Open
**PowerShell** (Start menu, type "powershell", Enter) and paste:

```powershell
irm https://raw.githubusercontent.com/Macle57/ds5-virtual-cable/main/scripts/install.ps1 | iex
```

It will ask for administrator rights once (the driver needs them; the app never
does), and then does, in order, telling you as it goes:

1. **Creates a System Restore point.** usbip-win2's own README asks for one
   before installing, because the next step installs two kernel drivers.
2. **Installs usbip-win2 0.9.7.7** — exactly that version, verified by
   checksum. (Its maintainer warns 0.9.7.8 can corrupt memory, so the
   installer will never "helpfully" take the latest.) **All your USB 3.0 hubs
   restart during this** — devices blink out and come back; do not run it
   while something is writing to a USB drive.
3. **Installs HidHide** — optional; it only powers the "hide the Bluetooth pad
   while bridged" feature. If this step fails you get a warning and everything
   else still works. HidHide's driver activates after the next reboot.
4. **Installs ds5bridge** itself to `%LOCALAPPDATA%\ds5bridge`, verified
   against the release's published checksums, adds a Start Menu shortcut, and
   starts the tray.

Safe to re-run any time — each step notices when its work is already done and
skips itself. If you want to see the plan first without changing anything:

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/Macle57/ds5-virtual-cable/main/scripts/install.ps1))) -DryRun
```

Other switches, same pattern: `-NoHidHide` (skip step 3), `-Autostart` (also
register start-at-login — the tray menu has the same switch), `-NoLaunch`.

If you have `usbipd` installed for WSL, the installer will mention it and move
on: it owns port 3240, ds5bridge deliberately uses 3241, and nothing conflicts.

### Or: by hand

The steps above are exactly the manual procedure:
usbip-win2 0.9.7.7 from
https://github.com/vadimgrn/usbip-win2/releases/tag/v.0.9.7.7 (create a
restore point first; do **not** use 0.9.7.8), optionally HidHide
(`winget install Nefarius.HidHide`), then unzip the
`ds5bridge-*-win-x64.zip` from this project's releases page anywhere you like.

### What is in the ds5bridge folder

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
below 20 %. Hover for the per-controller detail. A second dot in the corner
means more than one controller is bridged.

**You do not have to click anything.** The tray bridges every controller that is
switched on, and picks up ones you turn on later — so the normal routine is
"turn the controller on, start the game".

Right-click for the menu:

| item | what it does |
|---|---|
| **Open dashboard** | opens the status dashboard in your browser |
| **Bridging enabled** | the master switch. Unticking it stops every bridge |
| **Controllers >** | one row per controller — tick to bridge it, untick to leave it alone |
| **Hide Bluetooth pads while bridged** | optional, off by default — sets *every* controller at once; see below |
| **Hide per controller >** | the same choice, one pad at a time |
| **Rescan for controllers** | look again now instead of waiting for the next sweep |
| **Start at login** | run the tray when you log in |
| **Quit** | stop everything and put it all back |

Every one of those is remembered, in
`%APPDATA%\ds5bridge\config.json`.

#### Turning it off, and why you might

Unticking a controller — or **Bridging enabled** for all of them — stops its
bridge and hands the controller straight back to Windows' own Bluetooth stack.
Nothing is uninstalled, no driver is touched, and the pad goes on working as an
ordinary Bluetooth controller.

That is the right thing to do when a game already supports a DualSense properly
over Bluetooth, or when a launcher gets confused by seeing two pads. Tick it
again when you want the wired features back.

#### Optional: hiding the Bluetooth pad, so games see ONE controller

While a controller is bridged, Windows has **two** DualSenses -- the real
Bluetooth one and the virtual wired one. Most games take the wired one and all
is well; that is the "The game sees TWO controllers" row in troubleshooting.
Some do not. A few assign both to player slots, so one physical pad drives two
players.

If that is happening to you, ds5bridge can hide the Bluetooth pad from
everything except itself for as long as the bridge is up. It needs one more
free, Microsoft-signed driver:

**[HidHide](https://github.com/nefarius/HidHide/releases)** -- download
`HidHide_1.5.230_x64.exe` from that page, run it, and **reboot**.

> **The reboot is not optional.** HidHide filters HID devices by attaching to
> them as they are created, so until you restart, it has no effect on any
> device that already existed -- including your controller. Skipping it makes the
> feature look silently broken.

> ### Uninstall HidHide only with its own uninstaller
>
> Settings -> Apps -> HidHide -> Uninstall. **Do not remove it from Device
> Manager.** Doing that leaves its filter entries behind and *every* HID device
> on the machine then fails to start -- no keyboard, no mouse -- which takes a
> registry edit from the Windows Recovery Environment to undo. This is HidHide's
> own documented hazard, not something ds5bridge does.

After the reboot, check it actually works before you rely on it:

```
ds5bridge doctor
```

The `HidHide` rows say whether it is installed and whether it answers. If you
have the source checkout, `python app\tools\hidhide_verify.py` goes further and
proves both halves on your real controller -- that other programs stop seeing the
pad, *and* that ds5bridge can still open it. It puts everything back when it is
done.

Then tick **Hide Bluetooth pads while bridged** for all of them at once (the
box is ticked only when *every* pad is set to hide), or the one controller
under **Hide per controller**. From that moment:

* only ds5bridge can see the Bluetooth pad; games, Steam and tools such as
  `dualsense-tester` see only the virtual wired one;
* **games that are already running are unaffected.** HidHide blocks *opening*
  the device, not devices that are already open, so hide it *before* you start
  the game;
* unticking it, unticking the controller, quitting the tray, Ctrl+C, closing the
  window -- every one of those puts the pad back immediately.

It is **off by default**, deliberately: the failure mode of this feature is a
controller you cannot see, so nothing changes unless you go and ask for it.

#### If a pad is ever left hidden

Only a hard kill or a power cut can do this -- every normal exit puts the pad
back. If it happens, the fix is one of these, in order of convenience:

| | |
|---|---|
| **Just start ds5bridge again** | any part of it -- the tray, `ds5bridge run`, `ds5bridge cleanup` -- checks on startup and puts back anything a dead run left hidden. This is usually all you need. |
| **Tray -> Hide per controller -> Unhide everything now** | stops every bridge and returns every pad. Visible whenever something is hidden, even if HidHide has been uninstalled. |
| `ds5bridge unhide` | the same, from a terminal, for when the tray will not start. |
| `ds5bridge doctor` | says whether HidHide is installed, whether it answers, and lists anything still hidden along with whether the process that hid it is still alive. |

And if you would rather not involve ds5bridge at all: HidHide ships its own
tools. `HidHideCLI.exe --cloak-off` turns the whole thing off in one command, and
`HidHideClient.exe` is a window where you can untick the device by hand. They are
in `C:\Program Files\Nefarius Software Solutions\HidHide\x64`. Your controller is
never locked behind ds5bridge being able to run.

Non-standard install location? Point at it with `--hidhide-cli`, or set
`"hidhide_cli"` in `config.json`.

### If you have two controllers

```
ds5bridge.exe devices
```

```
2 controller(s) enumerated over Bluetooth:

   d42f4ba1485d  battery 70% (discharging), firmware Sep 18 2025 13:15:28
 x a0fa9c0dd8bb  -- not responding (read error)
```

Bridge all of them at once — each gets its own virtual controller, its own
audio device and its own port:

```
ds5bridge.exe --all
```

```
a0fa9c0dd8bb  running  port 3241  battery 50%  250 reports/s  up 8s
d42f4ba1485d  running  port 3242  battery 20%  250 reports/s  up 8s
```

Or name just one. With more than one live, `ds5bridge` will not guess:

```
ds5bridge.exe --serial d42f4ba1485d
```

Use the address, never "the first one" — the order changes between sessions.

Each controller keeps the same port run after run, which is what keeps Windows
treating it as the same device rather than a new one each time.

---

## What actually happens to your controller

* **Nothing permanent.** No firmware is touched, the pairing is never rewritten,
  and no setting on the controller is changed. The program deliberately refuses
  to forward the kinds of commands that could do any of that.
* **Nothing is installed** by `ds5bridge` itself. It uses the usbip-win2 driver
  the installer set up and one local network port (3241, loopback only —
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
| **"More than one Bluetooth DualSense is connected"** | two live controllers, and guessing would be wrong | `ds5bridge --all` bridges both. To pick one, run `ds5bridge devices` then `ds5bridge --serial <address>`. The tray does all of them without asking. |
| **A controller is connected but never gets bridged** | it is switched off in the tray menu, or the master switch is | `ds5bridge doctor` prints both, and the tray menu turns them back on. |
| **Random dropouts, timeouts, weird stutters** | **check the battery first.** Below ~15 % a DualSense produces failures that look exactly like software bugs | `ds5bridge devices` shows the level. Charge it. This has fooled every phase of this project at least once. |
| **"usbip-win2 is not installed"** | the driver install was skipped, or it went somewhere unusual | Re-run the install line (it only does what is missing), or install 0.9.7.7 from the link above. If it is installed somewhere odd, `ds5bridge --usbip "D:\path\to\usbip.exe"`. |
| **"TCP 3241 held by PID..."** | a previous run crashed and its server is still alive | `ds5bridge cleanup`. It kills the leftover and detaches anything stale. |
| **"left over from a previous run ... cleaning up"** | same thing, and it already fixed itself | Nothing. It is telling you, not asking. |
| **"ds5bridge is already running"** | you launched it twice | Use the one that is already running. Two would fight over one controller. |
| **The game does not see a controller** | usually the game was started first, or it is using a different input backend | Stop the game, make sure the status line says `running`, start the game again. Also check Windows **Settings → Bluetooth & devices → Devices** shows "Wireless Controller". |
| **The game sees TWO controllers** | the real Bluetooth one *and* the virtual wired one | Expected — Windows sees both. Most games take the wired one. If yours does not, unpair the Bluetooth controller from the *game's* settings, not from Windows. Or install HidHide and tick **Hide Bluetooth pads while bridged** (see "Using it"), which removes the Bluetooth one for everything but ds5bridge. |
| **My controller has vanished from Windows entirely** | a run was hard-killed while it had the Bluetooth pad hidden | Start ds5bridge again — it puts it back on startup. Or `ds5bridge unhide`. Or the tray's **Unhide everything now**. `ds5bridge doctor` shows what is still hidden. |
| **I ticked "Hide Bluetooth pad" and nothing happened** | either HidHide is not installed, or the game was already running | `ds5bridge doctor` says which. HidHide cannot hide a device from a program that already has it open — close the game, then start it again. |
| **"NOT hiding: this program is not on HidHide's whitelist"** | ds5bridge refused to hide, because it could not confirm it would still be able to read the controller itself | Deliberate, and the safe outcome: hiding a pad it cannot open would leave you with no working controller at all. Run `ds5bridge doctor` and read the `granting` row; `python app\tools\hidhide_verify.py` gives the full diagnosis. |
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
ds5bridge --all                every connected controller, one each
ds5bridge --serial d42f4ba1485d   ...using that controller specifically
ds5bridge devices              list controllers and battery levels
ds5bridge doctor               check this machine's setup, and your settings
ds5bridge cleanup              undo a run that crashed
ds5bridge unhide               give back a pad left hidden by a crash
ds5bridge tray                 run with the tray icon
ds5bridge tray --serial <addr> ...limited to one controller, just this once
ds5bridge --version
```

Useful extras: `--status-every 5` (more frequent status lines), `--port 3242`
(if something else wants 3241), `--audio-target headphone` (route audio to the
controller's 3.5 mm jack instead of its speaker — untested),
`--hide-bluetooth` (hide the Bluetooth pad for this run; needs HidHide), `-v`
(verbose logs when reporting a problem).

---

## Updates

The tray checks GitHub for a newer release **when it starts and once a day
after that**. The check is one small request for public release information —
nothing about you or your machine is sent, and nothing is downloaded or
installed by the check itself.

When a newer version exists you get a balloon notification, and the tray's
right-click menu grows one row: **Install update x.y.z**. Clicking it downloads
the new version, verifies it against the release's published SHA-256 checksums,
and restarts the tray on the new version — bridging stops for the few seconds
that takes and comes back on its own. If a download ever fails its checksum it
is deleted and nothing changes.

Not interested? Put `"update_check": false` in
`%APPDATA%\ds5bridge\config.json` and the tray never asks GitHub anything.
You can always update by re-running the install line — it is the same
download, and it leaves your settings alone.

(If you run from source instead of the installed app, the menu item opens the
release page rather than swapping files under your git checkout — `git pull`
is your update path.)

---

## Uninstall

**If you used the installer:** Settings → Apps → **ds5bridge** → Uninstall. A
dialog offers three boxes — *also remove usbip-win2*, *also remove HidHide*
(both ticked when the installer put them there, unticked when they were
already on the machine before it), *delete my settings too* (off) — then does
everything below. Details in [docs/installer.md](installer.md).

**Otherwise, or from a terminal:**

```powershell
irm https://raw.githubusercontent.com/Macle57/ds5-virtual-cable/main/scripts/uninstall.ps1 | iex
```

Either way it quits the tray, detaches the virtual pad, un-hides anything
HidHide was hiding, removes the app, its shortcut and its start-at-login entry
— and then **removes the two drivers too**, verifying each removal. The
exception: a driver that was already on the machine before the installer put
ds5bridge there is kept (the installer remembers which was which; the script
says "kept: it was already installed before ..." and `-RemoveUsbip` /
`-RemoveHidHide` override that). Add `-KeepUsbip` and/or `-KeepHidHide` (via
the scriptblock form above) if other software uses them: DS4Windows also
uses HidHide, and usbip-win2 may be your own tool. (`usbipd` for WSL is a
different product entirely and is never touched.) It asks for administrator
rights once, for the drivers, and refuses to start while another installer
is running.

**The reboot sequence.** usbip-win2's driver cannot be unloaded while Windows
is running — it would freeze Plug and Play and blue-screen the next shutdown —
so neither uninstaller asks Windows to do it:

1. **Uninstall.** The app and HidHide go at once; usbip-win2's driver is
   disabled and its removal scheduled. "USBip" stays listed in Settings → Apps
   for now — that is expected.
2. **Reboot.** At your next logon a one-shot task finishes the removal (USB
   devices blink out and back once; its log is
   `%ProgramData%\ds5bridge\usbip-removal.txt`).
3. **Reboot again before reinstalling.** Windows keeps the two driver service
   names reserved until it boots; the installer refuses to install usbip-win2
   in between and tells you why.

Nothing reboots on its own. Your settings in `%APPDATA%\ds5bridge` are kept
(add `-PurgeSettings`, or tick the box, to remove them too).

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
