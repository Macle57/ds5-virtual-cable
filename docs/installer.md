# ds5bridge-setup.exe — the Windows installer and uninstaller

`ds5bridge-setup-<version>-bundled.exe` is **the** install: an Inno Setup
installer that carries the app and the two driver packages — usbip-win2
0.9.7.7 and HidHide 1.5.230, unmodified and byte-for-byte as their authors
published them (~73 MB in total) — with a checkbox per step, a verification
page at the end, and an uninstaller (registered in Settings → Apps) that
takes the drivers back out too. Nothing is downloaded during the install; it
works offline; it is the one file on every release page (with its
`SHA256SUMS`). `scripts/install.ps1` is a one-line bootstrapper around it
(fetch, check the hash, run), and the app's own "Install update" menu item
downloads and runs the same file silently.

(A download-mode flavour, `ds5bridge-setup-<version>.exe`, which fetches the
two driver packages from their vendors' release pages during the install, can
still be built locally — `build-installer.ps1` without `-Bundle` — and behaves
identically apart from one log line, `Downloading temporary file` instead of
`Extracting temporary file: ...USBip-0.9.7.7-x64.exe`, and the absence of the
vendors' licence notices `THIRD-PARTY-NOTICES.txt` next to the uninstaller.
It is not published.)

## What it installs

| checkbox | default | what happens |
|---|---|---|
| **ds5bridge app** | always | Copied from inside the exe to `%LOCALAPPDATA%\ds5bridge\app` (the layout the in-app updater owns). Start Menu shortcut for the tray. Cannot be unticked: this installer always installs ds5bridge. |
| **usbip-win2 0.9.7.7** | on | The virtual-USB driver the bridge needs. Its own installer — extracted from the exe, or downloaded from its GitHub release — is SHA-256 checked against the pinned hash, then run silently. Exactly 0.9.7.7 — its maintainer warns 0.9.7.8 corrupts memory. Your USB 3.0 hubs restart briefly while it installs. |
| **HidHide 1.5.230** | on | Optional; only the "hide the Bluetooth pad while bridged" feature needs it. Extracted and hash-checked the same way. Its class filter only joins a HID device's stack when that stack is built, so right after installing it the installer **restarts the device node of every connected Bluetooth DualSense** (only those — never a keyboard or mouse) and then reads each one's driver stack (`DEVPKEY_Device_Stack`) to prove `\Driver\HidHide` is in it: `[ok] HidHide filter -- attached to N DualSense device(s), no reboot needed`. Only a pad it could not fix gets a `[reboot]` line. The installer never reboots on its own. |
| *task:* restore point | on | A System Restore point before the driver goes in (usbip-win2's README asks for one). Only offered when usbip-win2 is selected; only made when it is actually about to be installed. |
| *task:* start ds5bridge with Windows | on | Two exclusive options under it (radio buttons); choosing one removes the other, unticking the box removes both. |
| &nbsp;&nbsp;*— as a Windows service, before anyone signs in* | **default** | The Windows service `ds5bridge` (LocalSystem, automatic start; `sc query ds5bridge`). It starts at boot, before the sign-in screen, and keeps `ds5bridge-tray.exe --service-child` running in the console session — the same tray, icon, menu, dashboard and everything, so your controllers are bridged before you sign in and stay bridged across log-off and log-on, the way Chrome Remote Desktop's host works. The tray's settings then live in `%ProgramData%\ds5bridge\config.json` (your `%APPDATA%\ds5bridge\config.json` is copied there once, the first time this option is installed); its log is `%ProgramData%\ds5bridge\logs\tray.log`, the service's `service.log`. Registered by `ds5bridge.exe service install`; **Quit** in the tray menu stops the service (the menu says so). See "The service" below. |
| &nbsp;&nbsp;*— when you sign in* | | The scheduled task `ds5bridge` (Task Scheduler library root): at logon of the installing user only, *Run with highest privileges*, interactive token, no execution time limit, one instance, runs on battery — the same task the tray's own "Start at login" switch creates, and the only way to start an elevated program at logon without a prompt (an HKCU Run entry cannot; Windows skips it silently). What 0.5 offered. |
| *task:* Start menu entry / Desktop shortcut | on / on | Internet shortcuts named **ds5bridge dashboard** that open `http://127.0.0.1:<dashboard_port>/` (the port from `config.json`, default 8765) in your default browser — the quickest way to the dashboard. |

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

**The hash check.** The two driver packages are third-party signed kernel
drivers. The bundled build carries the vendors' official installers exactly
as published (the build refuses to embed a file whose SHA-256 or Authenticode
signature is wrong, and `NOTICE` and `THIRD-PARTY-NOTICES.txt` say so); a
download-mode build fetches each from its pinned URL. Either way, nothing
runs until the file on disk has the SHA-256 baked into the installer. The
app itself is always inside the exe: it is our own code and the thing being
installed.

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
[ok] HidHide filter -- attached to 2 DualSense device(s), no reboot needed
[ok] start with Windows -- service 'ds5bridge' registered (LocalSystem, automatic start: ds5bridge starts before anyone signs in; settings in C:\ProgramData\ds5bridge)
[ok] ds5bridge.exe --version -- 0.5.0 (C:\Users\you\AppData\Local\ds5bridge\app\ds5bridge.exe)
[ok] ds5bridge-tray.exe -- present
```

(When HidHide was installed in this run, `[ok] HidHide installer -- ran
(exit 0)` and the result of the attach step precede those: its service starts
at once, but the filter only joins HID devices whose stack is built after it
was registered, so the installer restarts the connected DualSenses' device
nodes and checks — `[ok] HidHide filter -- attached to N DualSense device(s),
no reboot needed (restarted N device(s) to make it so)`, or `[info] HidHide
filter -- no Bluetooth DualSense connected right now; HidHide joins a
controller's device stack when it connects`, or, only when a pad still lacks
the filter afterwards, `[reboot] HidHide -- its filter is not attached to 1
of 2 connected DualSense device(s) ...`. The `start-at-login` line appears
when that task was ticked.)

The app checks the same thing every time it hides a pad (`hide_effective` /
`hide_note` in the dashboard's `/api/state`, the `[filter]` block of
`ds5bridge doctor`, and the tray balloon, which says "NOT hidden yet" instead
of "hidden" when the filter is missing) and, running as administrator,
restarts the device node itself before it bridges the pad.

The same list is saved to `%LOCALAPPDATA%\ds5bridge\installer\last-install-check.txt`
and written into the setup log (`%TEMP%\Setup Log <date>.txt`, or the file
you name with `/LOG=`).

## Silent install

```
ds5bridge-setup-0.5.0-bundled.exe /SILENT /NORESTART /LOG="%TEMP%\ds5bridge-install.log"
```

Standard Inno Setup switches apply. The ones that matter here:

| switch | meaning |
|---|---|
| `/SILENT` or `/VERYSILENT` | no wizard (`/SILENT` still shows a progress bar). A silent install never starts the tray (see `/STARTTRAY`). |
| `/NORESTART` | never reboot. **Always pass it in silent mode.** The installer never restarts after an install, but its preflight can find that a restart must come *first* (a usbip-win2 removal left half-done, see below): interactive runs ask; a silent run without this switch restarts on its own, with it Setup exits with code 8 and must be run again after the restart (a silent run does not relaunch itself). |
| `/VERYSILENT /SUPPRESSMSGBOXES` | fully unattended. With plain `/SILENT`, a refusal (another installer still running after a minute's wait) is shown as a message box that waits for OK; with these two it goes to the log only (exit code 7; 8 when a restart must come first). |
| `/COMPONENTS="app,usbip"` | choose the checkboxes; names are `app`, `usbip`, `hidhide` (`app` is always installed, whatever the list says). Without the switch, Inno Setup reuses the selection of the previous run on that machine. |
| `/TASKS=...` / `/MERGETASKS=...` | tick / untick tasks. Names: `restorepoint` (on by default), `autostart\service` (on by default: the Windows service), `autostart\task` (the sign-in task instead), `!autostart` (neither: removes a service or task from an earlier install), `dashstartmenu` and `dashdesktop` (on by default). E.g. `/MERGETASKS="autostart\task,!dashdesktop"`. |
| `/STARTTRAY=1` | ds5bridge's own: start ds5bridge at the end of a **silent** run — the **service** when that is the chosen mode (`ds5bridge.exe service start`; its supervisor starts the tray), otherwise the tray exe (the installer is elevated, and so is the tray, so no prompt). What the in-app updater passes. Ignored in an interactive run, where the Finish page's "Start ds5bridge now" checkbox does the same. |
| `/LOG="file"` | full log including every helper line |

Exit code 0 means Setup finished; read `last-install-check.txt` (or grep the
log for `[FAIL]`) to know whether every verification passed.

The in-app updater runs exactly
`/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /COMPONENTS=app /MERGETASKS=!restorepoint /STARTTRAY=1 /LOG="%LOCALAPPDATA%\ds5bridge\updates\install-<ver>.log"`
after downloading the release's installer and checking it against
`SHA256SUMS` — so an update never installs a driver, never makes a restore
point and never reboots, and a refusal (exit 7) relaunches the version that
was already there. `/COMPONENTS=app` is remembered by Inno as the previous
selection, so the next interactive run of the installer on that machine opens
with the two driver boxes unticked; they read "already installed" anyway.

`scripts/install.ps1` is the one-line way to get here: it downloads the
release's `ds5bridge-setup-<version>-bundled.exe`, verifies it against the
release's `SHA256SUMS` (a mismatch deletes the file; a `SHA256SUMS` that does
not name the installer is a refusal, not a pass), and runs it — interactively
by default, or with `-Silent` (→ `/SILENT /NORESTART /SUPPRESSMSGBOXES`),
`-NoHidHide` / `-NoUsbip` (→ `/COMPONENTS=...`), `-NoRestorePoint` /
`-Autostart` (the service) / `-AutostartAtLogin` (the task) / `-NoAutostart` /
`-NoShortcuts` (→ `/MERGETASKS=...`), `-AppVersion v0.5.0` (a specific tag),
`-Setup <local exe>` (a build you already have; no hash to check it against)
and `-DryRun`. It never elevates itself; the installer does.

## The service

`ds5bridge.exe service ...` is the command line behind the "as a Windows
service" option, and what to reach for when something about it needs a
look:

| command | what it does |
|---|---|
| `ds5bridge.exe service status` | installed? running? starts with Windows? and where the logs are |
| `ds5bridge.exe service install` | register it (administrator): `sc create ds5bridge binPath= "\"...\ds5bridge.exe\" service" start= auto obj= LocalSystem`, a description, and a failure policy (the Service Control Manager restarts a crashed supervisor). Copies your `%APPDATA%\ds5bridge\config.json` and any hide-journal records into `%ProgramData%\ds5bridge` once (`--no-migrate` / `--migrate-from DIR`). Idempotent; re-running the installer or an update runs it again. Removes the sign-in task if one is there. |
| `ds5bridge.exe service start` / `stop` | through the SCM; `stop` waits for the tray to tear its bridges down (pads unhidden, virtual pads detached) — up to a minute |
| `ds5bridge.exe service uninstall` | stop and delete it |
| `ds5bridge.exe service` | what the SCM runs; from a terminal it just says so |

How it works: the service process (`ds5bridge.exe service`, LocalSystem,
session 0) is a **supervisor only**. It starts `ds5bridge-tray.exe
--service-child` in the active console session (its own SYSTEM token,
re-stamped with the session id, on `winsta0\default`), restarts it if it
exits (with a backoff, so a crash cannot loop hot), moves it when the console
session changes (fast user switching, a log-off that tears the old session
down), and stops it through a named event when the service stops or Windows
shuts down. The tray runs as SYSTEM in your session: the icon, balloons,
input injection and the dashboard all work as before; the browser for "Open
dashboard" is started with *your* token (not SYSTEM's). Before anyone signs
in there is no taskbar, so the tray bridges without an icon and adds it when
explorer starts. **Quit** in the tray menu (labelled *Quit (stops the
ds5bridge service)*) exits with a code the supervisor reads as "stop the
service", so Quit is not a restart button; "Start with Windows (service)" in
the menu switches the service between automatic and manual start (the
service stays installed, the running tray is untouched). A tray started by
hand while the service runs declines with a message rather than fighting the
first one for the pads. The in-app updater stops the service, runs the
installer, and the installer's `/STARTTRAY=1` starts the service again.

Two things verified from the SYSTEM child that were open questions: the
dictation toggle's default-device switch (`IPolicyConfig`) changes *your*
default microphone exactly as before -- Windows' default audio roles are
machine-wide, not per user; and the balloons are ordinary notifications, so
Windows' **Do not disturb** hides them like everyone else's (they are not
lost: bridging still happens, only the message is).

## Uninstall

Settings → Apps → **ds5bridge** → Uninstall, or run
`%LOCALAPPDATA%\ds5bridge\unins000.exe`. A dialog offers three boxes:

* **Also remove usbip-win2** — ticked by default if ds5bridge's installer
  put it there, unticked if it was already on the machine (the box says
  which). Untick if something else on the machine uses it.
* **Also remove HidHide** — same rule. Untick if DS4Windows or similar uses
  it. (Its filter driver unloads at the next reboot.)
* **Delete my settings too** — off by default. `%APPDATA%\ds5bridge`
  (per-controller settings, labels, the hide journal) and the service's
  copy in `%ProgramData%\ds5bridge` (its `config.json`, journal, logs).

Removing either driver needs **one restart** afterwards, and the uninstaller
makes that unmistakable: its result dialog opens with a bold **"A RESTART IS
REQUIRED to finish removing the drivers."** and a paragraph saying what the
restart does (HidHide's filter unloads at boot; usbip-win2's disabled driver
is taken out by the logon task — one reboot finishes both halves), and when
you close that dialog Inno's own **"restart now?"** question follows
(`UninstallNeedRestart`). Nothing restarts before you answer it. A silent
uninstall (`/SILENT` or `/VERYSILENT`) never restarts and never asks — not
even without `/NORESTART` — and reports the same facts as `[reboot]` lines in
`%TEMP%\ds5bridge-uninstall-check.txt`.

Before anything is deleted the uninstaller stops the service (its tray tears
down on the way out) or the tray, removes the service and the start-at-login
task (and a pre-0.5.0 Run value) and the dashboard shortcuts, runs
`ds5bridge cleanup` for the user's settings and the service's, and
`ds5bridge unhide` for whichever of the two still holds a hide record (with
none there is nothing owed, and the verb's optional rescue work -- reading
HidHide's lists, restarting the device node of a pad left invisible -- is
skipped: on one machine it sat inside a driver call for minutes and could not
be interrupted, and an uninstall must not wait on it), stops usbip's
auto-re-attach (`usbip attach -X`) and detaches every attached device, and
removes ds5bridge's own entries from HidHide's whitelist and hide list. Every
line the helper reports is shown on the progress form as it happens, so a
slow step (a device restart, a vendor uninstaller) is seen to be moving. The
result dialog names any known controller that has no HID device at that
moment and the fix when it is switched on: power-cycle it. When HidHide
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

A driver must not be installed over such a removal (the driver would come
back half-alive), and Windows keeps the two service names reserved
(`DeleteFlag=1`) from the moment the removal proper has run until it next
boots, so a reinstall in between would fail half-way. **The installer
handles both itself** (helper: `Resolve-UsbipRemovalPending`, run by the
preflight whenever usbip-win2 is being installed; a usbip-win2 found on disk
with its driver disabled or marked for deletion counts as *not* installed,
so the wizard wants a fresh one):

- **Driver disabled, still loaded** — the uninstall's reboot has not
  happened yet. Setup stops with *Windows must restart before the driver can
  be installed* and Inno's *restart now?* choice. It registers a relaunch of
  itself for the next logon (a `RunOnce` value, pointing at a copy of the exe
  in `%ProgramData%\ds5bridge\resume`), so after the restart it comes back on
  its own and continues; approve its UAC prompt.
- **Driver disabled, not loaded** — after that reboot. If the logon task is
  running the removal, Setup waits for it (up to 8 minutes); if the task
  never ran (another user logged in, the task was refused), Setup removes
  the task and does the removal itself, right there. What is left is the two
  reserved names, so:
- **Services marked for deletion** — Setup stops with the same restart
  prompt and relaunches itself after the restart, then installs. A
  tombstone that is *older than the last boot* (Windows failed to clear it)
  is deleted instead, no restart.

So **uninstall → reboot → run setup → "restart now" → setup continues on
its own** is what a person sees when reinstalling a fresh usbip-win2, one
prompt instead of a refusal they had already obeyed (which is what happened
on 2026-09-06: *"a removal of usbip-win2 is waiting for a reboot" despite
rebooting, and USBip gone from Settings > Apps* — that was the second,
unannounced reboot). Two other refusals of the old preflight are gone with
it: another installer at work is now *waited for* (a minute, polling; an OEM
updater's `pnputil` at logon takes seconds) before Setup says so and asks
for Back, then Next; and a Windows Installer "installation in progress"
flag left behind by a crashed `msiexec` (it survives reboots and fails every
MSI with error 1618) is removed when no `msiexec` is alive. Silent runs get
the exit codes in the table above and never relaunch themselves. A result
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
[ok] Bluetooth DualSense -- 2 present and OK
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

`scripts/uninstall.ps1` takes the same choices as switches (`-KeepUsbip`,
`-KeepHidHide`, `-RemoveUsbip`, `-RemoveHidHide`, `-PurgeSettings`, `-Silent`,
`-DryRun`). When the exe's uninstaller is registered it simply runs that with
the switches mapped onto the parameters above and prints its result list;
only on a machine without one (an install that predates the exe) does it do
the removal itself, following the same origin records and the same guards.

## Things to know

* **"Windows protected your PC".** Nothing here is code-signed — not either
  setup exe, not the app — and that will stay so until the project has a
  code-signing certificate. SmartScreen shows that dialog the first time it
  sees the file; *More info → Run anyway* if you trust where you got it (the
  release page's `SHA256SUMS` lists the hash of every file, which is the way
  to check). The two driver packages are signed by their own publishers
  (usbip-win2 with an EV certificate, HidHide by Nefarius) and Windows loads
  their kernel drivers without test-signing or Secure Boot changes.
* **Administrator rights.** The installer and uninstaller elevate (one UAC
  prompt each) for the drivers. Since 0.5.0 **the tray runs as administrator
  too** (`ds5bridge-tray.exe` carries a requireAdministrator manifest):
  restarting a device node — the fix for both "unhide does not unhide" and
  "hide does not hide on a fresh install" — cannot be done from an ordinary
  token. Consequences: the "Start ds5bridge now" box launches it straight
  from the elevated installer (no second prompt); the Start Menu shortcut and
  a manual launch show one UAC prompt; start-at-login is a scheduled task with
  highest privileges, so the logon start shows none; "Open dashboard" hands
  the URL to `explorer.exe` so your browser does *not* inherit the token; and
  the in-app updater runs the installer without a prompt of its own. The
  console `ds5bridge.exe` (`doctor`, `unhide`, `run`) stays unelevated and
  says so when a step needs more.
* **The app lives in your profile** (`%LOCALAPPDATA%\ds5bridge`), the layout
  the updater and the installer share. If you are a standard user and elevate
  the installer with a *different* administrator account, the app — and the
  start-at-login task and the shortcuts — land in that account's profile
  instead; install from an administrator account. (The service, being
  machine-wide, does not care which account installed it; its settings are
  in `%ProgramData%`.)
* **Updating from 0.5.0.** The 0.5 "start at login" task selection is
  remembered by Inno as `autostart`, which is now the parent of the two
  options, so an update (silent or interactive without changing the Tasks
  page) moves you to the service — the recommended mode; your settings are
  copied to `%ProgramData%\ds5bridge` and the task is removed. Pick *when
  you sign in* on the Tasks page (or pass `/MERGETASKS="autostart\task"`) to
  keep the task instead; from then on Inno remembers that choice for updates.
* **Reboot.** After an install, usually none: the installer attaches HidHide's
  filter to the connected controllers itself and the app does the same for a
  pad it bridges (a pad that connects later gets the filter as its device
  stack is built). A `[reboot]` line appears only when a connected pad still
  lacks the filter after that, and the tray then says "NOT hidden yet" instead
  of "hidden". After an uninstall that removed a driver, **one** reboot is
  required (HidHide unloads at boot, usbip-win2's second half runs at the next
  logon), and the uninstaller asks. Nothing ever reboots on its own.
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
powershell -File app\packaging\build-installer.ps1 -Bundle      # THE release build (fetches + verifies the vendor installers once)
powershell -File app\packaging\build-installer.ps1              # download-mode build, for local use; builds dist\ds5bridge first if missing
powershell -File app\packaging\build-installer.ps1 -Rebuild     # PyInstaller clean build first
```

Needs Inno Setup **6.7.3** (`winget install --id JRSoftware.InnoSetup --exact
--scope user --version 6.7.3` — the version the installer was verified with;
7.x has not been tried) and the PyInstaller venv `build.ps1` uses. Output:
`dist\ds5bridge-setup-<version>-bundled.exe` or `dist\ds5bridge-setup-<version>.exe`
(`-OutputDir` to put it elsewhere), version taken from `app/ds5app/__init__.py`.
Builds are deterministic. `.github/workflows/release.yml` runs the `-Bundle`
command on a tag and publishes that one exe and its `SHA256SUMS` — nothing
else. Files involved:

| file | role |
|---|---|
| `app/packaging/ds5bridge.iss` | the Inno script: wizard, components, download + hash check, summary page, uninstaller dialog |
| `app/packaging/setup-helper.ps1` | the worker both halves call for everything that reads or changes machine state (services, devnodes, HidHide lists and its filter attachment, the start-at-login task, the vendors' uninstallers); one `KIND|label|detail` line per fact |
| `app/packaging/build-installer.ps1` | builds the app if needed, compiles the script, prints the result's SHA-256 |

`-Bundle` downloads `USBip-0.9.7.7-x64.exe` and `HidHide_1.5.230_x64.exe`
into `app/packaging/bundle/vendor/` (git-ignored; skipped when they are
already there with the right hash), refuses either unless its SHA-256 is the
pinned one and its Authenticode signature is valid, and embeds both;
`-BundleUsbip <file> -BundleHidHide <file>` does the same from files you
already have. The result also installs `app/packaging/bundle/THIRD-PARTY-NOTICES.txt`
next to the uninstaller (the BSD-2 notice obligation; `NOTICE` describes the
bundled build too) and gets the `-bundled` name, so the two flavours can sit
side by side in `dist\`. `docs/bundle-handoff.md` has the licence findings.
