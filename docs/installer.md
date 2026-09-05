# ds5bridge-setup.exe — the Windows installer and uninstaller

`ds5bridge-setup-<version>.exe` is an Inno Setup installer that does what
`scripts/install.ps1` does, as a wizard with a checkbox per step, and ships an
uninstaller (registered in Settings → Apps) that takes the drivers back out
too. The PowerShell scripts keep working and do the same things; use
whichever you prefer.

## What it installs

| checkbox | default | what happens |
|---|---|---|
| **ds5bridge app** | always | Copied from inside the exe to `%LOCALAPPDATA%\ds5bridge\app` (the layout the in-app updater owns). Start Menu shortcut. Cannot be unticked: this installer always installs ds5bridge. |
| **usbip-win2 0.9.7.7** | on | The virtual-USB driver the bridge needs. **Downloaded** at install time from its GitHub release, SHA-256 checked against the pinned hash, then run silently. Exactly 0.9.7.7 — its maintainer warns 0.9.7.8 corrupts memory. Your USB 3.0 hubs restart briefly while it installs. |
| **HidHide 1.5.230** | on | Optional; only the "hide the Bluetooth pad while bridged" feature needs it. Downloaded and hash-checked the same way. Its filter driver activates after the next **reboot**; the installer says so and never reboots on its own. |
| *task:* restore point | on | A System Restore point before the driver goes in (usbip-win2's README asks for one). Only offered when usbip-win2 is selected; only made when it is actually about to be installed. |
| *task:* start at login | off | The same HKCU Run entry the tray's own menu switch writes. |

Every step is idempotent: if usbip-win2 0.9.7.7 or HidHide is already there,
its box stays ticked but reads "already installed (will be verified, not
reinstalled)" and nothing is downloaded for it. Re-running the installer on a
healthy machine only refreshes the app.

The installer remembers which driver it installed and which it merely found
(`HKLM\SOFTWARE\ds5bridge\Setup`, `UsbipInstalledByUs` / `HidHideInstalledByUs`
= 1 or 0). The uninstaller uses that: a driver ds5bridge installed is removed
by default, one that was already on the machine is kept by default.

**Before anything is changed** the installer checks that no other installer
is at work (`msiexec`, another Inno setup or uninstaller, `devnode`,
`nefconw`, `pnputil`, or an open Windows Installer transaction). Two driver
installers running at once can leave Windows unable to enumerate devices, so
a driver install stops with a message when that is the case; an app-only run
goes on. It also repairs one specific piece of damage if it finds it: a
`HidHide` entry left in the HID classes' `UpperFilters` with no HidHide
service behind it (what an interrupted HidHide install/uninstall leaves), which
otherwise kills every keyboard, mouse and pad from the next boot.

**Why the app is embedded but the drivers are downloaded.** The app is our
own code and is the thing being installed, so it is inside the exe. The two
driver packages are third-party signed kernel drivers that this project does
not redistribute (see `NOTICE`); the installer fetches each from its pinned
URL and refuses to run anything whose SHA-256 does not match the hash baked
into the installer — the same URLs and hashes `scripts/install.ps1` uses. The
script is structured so that a build can carry either file inside the exe
instead (`build-installer.ps1 -BundleUsbip ... -BundleHidHide ...`); the hash
check applies either way.

**After installing** the wizard shows an *Installation check* page listing
each verification it ran:

```
[ok] usbip.exe --version -- 0.9.7.7 (C:\Program Files\USBip\usbip.exe)
[ok] service usbip2_filter -- running
[ok] service usbip2_ude -- running
[ok] devnode -- USBip 3.X Emulated Host Controller (ROOT\USB\0000, status OK)
[info] port 3240 -- in use by usbipd (pid 1234) -- that is usbipd-win (WSL USB passthrough); fine, ds5bridge uses 3241 and never touches it
[ok] HidHideCLI.exe -- 1.5.230.0 (C:\Program Files\Nefarius Software Solutions\HidHide\x64\HidHideCLI.exe)
[ok] service HidHide -- running
[ok] ds5bridge.exe --version -- 0.4.0 (C:\Users\you\AppData\Local\ds5bridge\app\ds5bridge.exe)
[ok] ds5bridge-tray.exe -- present
```

(When HidHide was installed in this run, an `[ok] HidHide installer -- ran
(exit 0)` and a `[reboot] HidHide -- freshly installed; ...` line precede
those: its service starts at once, but the filter only joins HID devices
enumerated after it was registered, so a controller that is already paired
needs the reboot before it can be hidden.)

The same list is saved to `%LOCALAPPDATA%\ds5bridge\installer\last-install-check.txt`
and written into the setup log (`%TEMP%\Setup Log <date>.txt`, or the file
you name with `/LOG=`).

## Silent install

```
ds5bridge-setup-0.4.0.exe /SILENT /NORESTART /LOG="%TEMP%\ds5bridge-install.log"
```

Standard Inno Setup switches apply. The ones that matter here:

| switch | meaning |
|---|---|
| `/SILENT` or `/VERYSILENT` | no wizard (`/SILENT` still shows a progress bar) |
| `/NORESTART` | never reboot. **Always pass it in silent mode.** The installer itself never asks Windows to restart, but it is the safe habit with any Inno installer. |
| `/VERYSILENT /SUPPRESSMSGBOXES` | fully unattended. With plain `/SILENT`, a refusal (another installer running, a usbip-win2 removal waiting for a reboot) is shown as a message box that waits for OK; with these two it goes to the log only (exit code 7). |
| `/COMPONENTS="app,usbip"` | choose the checkboxes; names are `app`, `usbip`, `hidhide` (`app` is always installed, whatever the list says). Without the switch, Inno Setup reuses the selection of the previous run on that machine. |
| `/TASKS="autostart"` / `/MERGETASKS="!restorepoint"` | tick / untick the two tasks (`restorepoint` is on by default, `autostart` off) |
| `/LOG="file"` | full log including every helper line |

Exit code 0 means Setup finished; read `last-install-check.txt` (or grep the
log for `[FAIL]`) to know whether every verification passed.

## Uninstall

Settings → Apps → **ds5bridge** → Uninstall, or run
`%LOCALAPPDATA%\ds5bridge\unins000.exe`. A dialog offers three boxes:

* **Also remove usbip-win2** — ticked by default if ds5bridge's installer
  put it there, unticked if it was already on the machine (the box says
  which). Untick if something else on the machine uses it.
* **Also remove HidHide** — same rule. Untick if DS4Windows or similar uses
  it. (It asks for a reboot to finish unloading its filter driver.)
* **Delete my settings too** — off by default. `%APPDATA%\ds5bridge`
  (per-controller settings, labels, the hide journal).

Before anything is deleted the uninstaller stops the tray, runs
`ds5bridge cleanup` and `ds5bridge unhide`, stops usbip's auto-re-attach
(`usbip attach -X`) and detaches every attached device, and removes
ds5bridge's own entries from HidHide's whitelist and hide list. When HidHide
itself is being removed, its whole hide list is cleared and the cloak turned
off first, so no device can be left invisible. Then the vendors' own
uninstallers run silently, HidHide first
(`msiexec /X{HidHide product code} /qn /norestart`) and usbip-win2 last
(`C:\Program Files\USBip\unins000.exe /VERYSILENT`) — and only when no other
installer is running at that moment and nothing is attached any more
(`usbip port` empty, no virtual controller enumerated); otherwise that driver
is left in place with a `[warn]`/`[FAIL]` line saying why.

**usbip-win2 is removed in two halves, with a reboot in between.** Its
driver (`usbip2_ude.sys` 0.9.7.7) cannot be unloaded while Windows is
running: the device goes away, but the driver then never returns from its
unload routine, which freezes Plug and Play and turns the next shutdown into
a five-minute hang ending in a `DRIVER_POWER_STATE_FAILURE` blue screen that
commits nothing (the vendor's own uninstaller runs into exactly this). So
while the driver is loaded the uninstaller does **not** ask Windows to remove
it. It disables the driver's service so it does not start at the next boot,
reports `[reboot] usbip-win2 -- its driver can only be removed after a
REBOOT ...`, and registers a one-shot task (*ds5bridge finish usbip-win2
removal*, Administrators, highest privileges, no time limit) that runs at
your next logon and does the removal proper: `pnputil /remove-device` of the
emulated controller, then usbip-win2's own uninstaller (which now completes:
it deletes both driver packages — the USB 3.0 hubs blink out and back once —
its files and its Apps entry), then any leftovers. Its log is
`%ProgramData%\ds5bridge\usbip-removal.txt`. Until that has run, **"USBip"
stays listed in Settings → Apps; that is expected.** If the task could not
be registered, run the uninstaller script again after the reboot, or use
Settings → Apps → USBip → Uninstall (that works once the driver is not
loaded). Every Plug and Play call in that second half runs under a timeout
and is never killed; if one blocks, the log says so and asks for a reboot.

The installer refuses to install a driver while such a removal is waiting
for its reboot (`[FAIL] usbip-win2 -- a removal of usbip-win2 is waiting
for a reboot ...`), and again right after the removal has run, until the
next boot (`[FAIL] usbip-win2 -- its driver services from a previous
install are still marked for deletion ...`): Windows keeps the two service
names reserved (`DeleteFlag=1`) until it boots, and a reinstall in between
would fail half-way. So **uninstall → reboot → (removal runs at logon) →
reboot → reinstall** is the sequence for a fresh usbip-win2. A result
dialog lists what was verified:

```
[ok] ds5bridge -- stopped (1 process(es))
[ok] usbip -- auto-re-attach stopped, nothing attached
[ok] HidHide all entries -- unhid 2 device(s), cloak off
[ok] HidHide uninstaller -- msiexec /X{...} exit 0
[ok] usbip-win2 -- nothing attached
[ok] usbip-win2 driver -- usbip2_ude set to not start at the next boot (it cannot be unloaded while Windows is running)
[ok] usbip-win2 removal -- scheduled for the next logon (task 'ds5bridge finish usbip-win2 removal'; log: C:\ProgramData\ds5bridge\usbip-removal.txt)
[reboot] usbip-win2 -- its driver can only be removed after a REBOOT; it is removed automatically at your next logon (USB 3.0 devices blink out and back once). Until then 'USBip' stays listed in Settings > Apps -- that is expected
[info] usbip-win2 -- its driver is disabled and the removal runs after the reboot (not checked now); USBip stays in Settings > Apps until then
[ok] HidHideCLI.exe -- gone
[ok] service HidHide -- marked for deletion (gone after a reboot)
[reboot] HidHide -- its filter driver is unloaded on the next reboot
[ok] Bluetooth DualSense -- 2 HID devnode(s) present and OK
[ok] app -- C:\Users\you\AppData\Local\ds5bridge removed
[info] settings -- kept at C:\Users\you\AppData\Roaming\ds5bridge
```

`usbipd` (usbipd-win, the WSL USB passthrough service on port 3240) is a
different product and is never touched.

Silent:

```
"%LOCALAPPDATA%\ds5bridge\unins000.exe" /SILENT /NORESTART /LOG="%TEMP%\ds5bridge-uninstall.log"
    [/KEEPUSBIP=1] [/KEEPHIDHIDE=1] [/REMOVEUSBIP=1] [/REMOVEHIDHIDE=1] [/PURGESETTINGS=1]
```

`/KEEP*=1` forces a driver to stay, `/REMOVE*=1` forces it out even if it
was adopted; keep wins when both are given; with neither, the recorded origin
decides. The result list is saved to `%TEMP%\ds5bridge-uninstall-check.txt`.

The PowerShell equivalent, for people who never used the exe, is
`scripts/uninstall.ps1` with `-KeepUsbip`, `-KeepHidHide`, `-RemoveUsbip`,
`-RemoveHidHide`, `-PurgeSettings` and `-DryRun`; it follows the same origin
records and the same guards, and also cleans up the Settings → Apps entry if
the exe installer had made one.

## Things to know

* **"Windows protected your PC".** Nothing here is code-signed — not the
  setup exe, not the app. SmartScreen shows that dialog the first time it
  sees the file; *More info → Run anyway* if you trust where you got it. The
  two driver packages it downloads are signed by their own publishers
  (usbip-win2 with an EV certificate, HidHide by Nefarius) and Windows loads
  their kernel drivers without test-signing or Secure Boot changes.
* **Administrator rights** are needed for the drivers, so the installer and
  uninstaller elevate (one UAC prompt each). The app itself never runs
  elevated: the "Start ds5bridge now" box on the last page launches it with
  your ordinary token, and so does the shortcut.
* **The app lives in your profile** (`%LOCALAPPDATA%\ds5bridge`), so that its
  updater can replace it without asking for administrator rights. If you are
  a standard user and elevate the installer with a *different*
  administrator account, the app lands in that account's profile instead;
  install from an administrator account, or use the zip.
* **Reboot.** HidHide needs one to start hiding (after install) and to unload
  (after uninstall). usbip-win2's removal always needs one (see above: its
  driver is disabled first, removed at the next logon). The installer never
  reboots for you. Until HidHide's reboot, a bridged controller is *not*
  hidden even though the tray asks for it (the installer's `[reboot]` line
  and the tray's own warning say so).
* **HidHide's leftover driver file.** HidHide's own uninstaller leaves
  `C:\Windows\System32\drivers\HidHide.sys` behind (in use until the reboot).
  The uninstaller reports it as `[info]` and deletes it once no HidHide
  service exists any more (after that reboot, from the usbip-win2 finishing
  task, or on a later run); it never touches the file while a HidHide
  service is registered.
* **Another installer running.** The installer and uninstaller refuse to
  touch a driver while `msiexec`, another setup, `devnode`, `nefconw` or
  `pnputil` is running. Let it finish and run again.
* **usbip-win2 already installed but a different version** — the installer
  replaces it with 0.9.7.7 (detaching anything attached first) and says so on
  the components page. 0.9.7.8 in particular is replaced with a warning.

## Building it

```
powershell -File app\packaging\build-installer.ps1              # builds dist\ds5bridge first if missing
powershell -File app\packaging\build-installer.ps1 -Rebuild     # PyInstaller clean build first
```

Needs Inno Setup 6.7 or newer (`winget install --id JRSoftware.InnoSetup
--exact --scope user`) and the PyInstaller venv `build.ps1` uses. Output:
`dist\ds5bridge-setup-<version>.exe`, version taken from
`app/ds5app/__init__.py`. Files involved:

| file | role |
|---|---|
| `app/packaging/ds5bridge.iss` | the Inno script: wizard, components, download + hash check, summary page, uninstaller dialog |
| `app/packaging/setup-helper.ps1` | the worker both halves call for everything that reads or changes machine state (services, devnodes, HidHide lists, the vendors' uninstallers); one `KIND|label|detail` line per fact |
| `app/packaging/build-installer.ps1` | builds the app if needed, compiles the script, prints the result's SHA-256 |

To carry the driver installers inside the exe instead of downloading them,
pass `-BundleUsbip <USBip-0.9.7.7-x64.exe> -BundleHidHide <HidHide_1.5.230_x64.exe>`;
the build refuses a file whose SHA-256 is not the pinned one, and the result
also installs `app/packaging/bundle/THIRD-PARTY-NOTICES.txt` next to the
uninstaller (the BSD-2 notice obligation; `NOTICE` describes the bundled
build too). Such a build behaves exactly like the download one except that
its log shows `Extracting temporary file: ...USBip-0.9.7.7-x64.exe` /
`...HidHide_1.5.230_x64.exe` instead of `Downloading temporary file` (the
SHA-256 check runs either way). Note that `build-installer.ps1` always
writes `dist\ds5bridge-setup-<version>.exe`: move a bundled build aside (for
example to `dist\bundle\ds5bridge-setup-<version>-bundled.exe`) before
building the download flavour again. `docs/bundle-handoff.md` has the
licence findings.
