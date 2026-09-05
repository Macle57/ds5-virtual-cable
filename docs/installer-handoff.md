# Installer work — handoff (installer-agent, 2026-09-04 21:47, written minutes before a forced reboot)

> **2026-09-05 13:45: read "Phase C results", parts 1 and 2 (last sections) first** —
> usbip-win2 0.9.7.7 cannot be unloaded while Windows runs (two crash dumps
> prove it); the uninstaller now removes it in two halves around a reboot
> (proven end to end), and a reinstall needs one more reboot after that
> (the SCM keeps the service names until boot). **Part 3: the clean-machine
> full install with build 9 PASSED** (restore point, both downloads, both
> driver installs, records 1/1). **Part 4: bridge + hide with the installed
> tray PASSED, and the DEFAULT uninstall through the exe (records 1/1)
> PASSED** -- HidHide removed, usbip-win2 stage A done, logon task armed.
> **Part 5: bundled install PASSED (no downloads, notices file), interactive
> uninstaller dialogs and the Installation-check page captured, build 10
> (`C9851595...` / bundled `EC186E8C...`).** Machine left FULLY INSTALLED
> with HidHide's reboot pending; "Still unproven" at the very end.
>
> **2026-09-05: see "Phase A results" at the end of this file** — the
> post-reboot recovery, the hardening (concurrency guard, orphan-filter
> repair, usbip removal sequencing, adoption flags), the tests that passed
> with evidence, and the remaining matrix. Sections above are the state as
> of the evening before; Known bugs #1, #2 and #6 are addressed there.

Companion to `docs/installer.md` (the user-facing description). This file is
the engineering state: what exists, what was proven, what was not, what broke
on the test machine and how to recover it after the reboot.

## Files created / changed (uncommitted, in the working tree)

| path | status | what |
|---|---|---|
| `app/packaging/ds5bridge.iss` | new | Inno Setup 6.7 script: components page (app / usbip-win2 0.9.7.7 / HidHide), tasks (restore point, autostart), download-with-SHA-256 of the two driver installers (`GetInstallerFile()` is the one place that would switch to a bundled copy via `/DBundleUsbip` / `/DBundleHidHide`), driver install in `ssInstall`, verification in `ssPostInstall`, "Installation check" summary page, uninstaller with a 3-checkbox dialog (`/KEEPUSBIP=1 /KEEPHIDHIDE=1 /PURGESETTINGS=1` silent equivalents) and a result dialog. Never reboots (`AlwaysRestart=no`, `NeedRestart` not implemented). |
| `app/packaging/setup-helper.ps1` | new | The worker both halves run with `powershell.exe` (5.1): verbs `restore-point`, `stop-app`, `teardown`, `hidhide-clear [-All]`, `remove-usbip`, `remove-hidhide`, `verify-install`, `verify-removed`; writes `KIND\|label\|detail` lines to `-Out`. Installed to `%LOCALAPPDATA%\ds5bridge\installer\` for the uninstaller. |
| `app/packaging/build-installer.ps1` | new | Builds `dist\ds5bridge` if missing (`-Rebuild` forces `build.ps1 -Clean`), reads the version from `app/ds5app/__init__.py`, finds ISCC, compiles, prints SHA-256. `-BundleUsbip/-BundleHidHide <file>` verify the pinned hash and embed. |
| `scripts/uninstall.ps1` | rewritten | Now removes usbip-win2 and HidHide by default; `-KeepUsbip`, `-KeepHidHide`, `-PurgeSettings`, `-DryRun`; elevates itself (re-runs `$PSCommandPath` or downloads the raw script for the `irm\|iex` case); clears HidHide whitelist/hide entries (all of them when removing HidHide); detaches usbip (`attach -X`, `detach -a`); removes the Inno Add/Remove key `{7B6E2D6A-...}_is1` if the exe installer was used; verifies every removal. |
| `scripts/install.ps1` | unchanged | Still works; not exercised in this session. |
| `docs/installer.md` | new | User-facing doc: components, silent switches, uninstall options, SmartScreen caveat, build. |
| `docs/USER-GUIDE.md` | edited | Pointers to the exe installer in *Install* and *Uninstall*; uninstall section updated for the new default (drivers removed unless kept). |
| `dist/ds5bridge-setup-0.4.0.exe` | build output (gitignored) | 38.7 MB, SHA-256 `E9B8CD8C40886673919C4DCA0F21A31AF34A3195DE3162D1C0CDEAF0596F1F2F`. **Built BEFORE the last `.iss` edit** (a `Log('components selected: ...')` line in `NextButtonClick(wpReady)`); rebuild before the next test. |
| `dist/ds5bridge/` | rebuilt 21:12 | fresh PyInstaller one-dir build, 0.4.0, 193 files, 135 MB. |
| `scratchpad/installer-agent-logs/` | evidence | all logs, screenshots and the harness scripts (see below). |

Not mine: `.gitignore` modification, `app/packaging/bundle/`, the Add/Remove
entry "ds5bridge (includes usbip-win2 and HidHide drivers)" at
`C:\Program Files\ds5bridge\unins000.exe` — those are the sibling
bundle-agent's. Two ds5bridge entries can coexist (different AppIds); the
user should pick one layout eventually. `README-human.md` untouched.

Tooling installed on the machine this session: Inno Setup 6.7.3, user scope,
`%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe` (via
`winget install --id JRSoftware.InnoSetup --exact --scope user`, no UAC).

## Build

```
powershell -File app\packaging\build-installer.ps1            # or -Rebuild
```
Output `dist\ds5bridge-setup-0.4.0.exe`. Stub compile for syntax checks only:
`ISCC.exe /Qp /DAppVersion=0.4.0 /DAppBuildDir=<tiny dir> /DOutputDir=<tmp> app\packaging\ds5bridge.iss`
(run ISCC from PowerShell, not Git Bash — bash rewrites `/D...` switches into paths).

## Run

```
dist\ds5bridge-setup-0.4.0.exe /SILENT /NORESTART /LOG="x.log" [/COMPONENTS="app,usbip,hidhide"] [/TASKS="autostart"] [/MERGETASKS="!restorepoint"]
"%LOCALAPPDATA%\ds5bridge\unins000.exe" /SILENT /NORESTART /LOG="y.log" [/KEEPUSBIP=1] [/KEEPHIDHIDE=1] [/PURGESETTINGS=1]
powershell -File scripts\uninstall.ps1 [-DryRun] [-KeepUsbip] [-KeepHidHide] [-PurgeSettings]
```
Results: `%LOCALAPPDATA%\ds5bridge\installer\last-install-check.txt`,
`%TEMP%\ds5bridge-uninstall-check.txt`, plus the `/LOG` file (every helper
line is in it: grep `summary:`).

## What was verified (evidence in `scratchpad/installer-agent-logs/`)

| # | test | result | evidence |
|---|---|---|---|
| 0 | `setup-helper.ps1 verify-install` / `verify-removed`, non-elevated, read-only | PASS: usbip 0.9.7.7, services running, devnode `ROOT\USB\0000`, port-3240 info line names `usbipd (pid 12936)`, HidHide 1.5.230.0 running, 2 BT pads | `helper-verify-install-nonelevated.txt` |
| 1 | wizard pages, interactive (elevated) | Components page shows "already installed (will be verified, not reinstalled)" for both drivers; Tasks page (restore point on, autostart off); Ready page with the Plan memo. **My walker had one ENTER too many** (no Welcome page in the modern style) and pressed Install, then cancelled via Inno's "Exit Setup?" while `ds5bridge.exe` was being written; Setup rolled back cleanly, no driver touched. That "Setup is not complete…" box is what the user saw; Inno's file-creation error wording ("Setup was unable to create…") is not in that flow and does not appear in the log or screenshots. | `ui-1..5.png`, `setup-log-2130-interactive-cancelled.txt` |
| 2 | **silent install over an already-complete machine** (`/SILENT /NORESTART`) | **PASS**, exit 0 in 9 s: `[ok] usbip-win2 0.9.7.7 -- was already installed`, `[ok] HidHide -- was already installed`, all 10 verification lines `[ok]`/`[info]`, app at `%LOCALAPPDATA%\ds5bridge\app` (0.4.0), Start Menu shortcut, Add/Remove entry "ds5bridge 0.4.0", `last-install-check.txt` written | `s1-install-idempotent.log`, `s2-s3-console-output.txt` (BEFORE/AFTER state dumps) |
| 3 | `scripts/uninstall.ps1 -DryRun`, non-elevated | PASS: prints the plan, changes nothing, exit 0 | (console, in the agent transcript) |
| 4 | silent uninstall intended as `/KEEPUSBIP=1 /KEEPHIDHIDE=1` | **harness bug — switches never reached the uninstaller** (see Known bugs #1); it ran a full uninstall concurrently with bundle-agent's full uninstall: app removed OK (`[ok] app -- %LOCALAPPDATA%\ds5bridge removed`, teardown/cleanup/unhide all `[ok]`), but `usbip-win2 uninstaller -- exited with 1` (second Inno uninstaller instance refused while bundle-agent's was running), so verify-removed reported 4 `[FAIL]` lines honestly | `s2-uninstall-keep-both.log` |
| 5 | silent install intended as `/COMPONENTS=app` | **same harness bug** — ran as "everything"; HidHide was absent at that moment (bundle-agent had removed it) so it downloaded `HidHide_1.5.230_x64.exe` (8078016 bytes, `SHA-256 verified` at 21:34:13) and ran its MSI while PnP was already wedged by the concurrent usbip-win2 removal. Frozen at time of writing. **The download + hash-verify path is therefore proven; the HidHide silent install path is not.** | `s3-install-app-only.log` |

## Not verified (blocked by the PnP hang / reboot)

* Full uninstall to a clean machine and full install from a clean machine
  (usbip-win2 download + install, restore point, HidHide install) with the
  post-install verification — i.e. the main cycle. Only the download/hash step
  and the "already installed" skip path were exercised.
* `/COMPONENTS="app,usbip"` (HidHide unticked) and `/KEEPUSBIP=1` /
  `/KEEPHIDHIDE=1` (untested because of Known bug #1 in the *harness*; the
  installer/uninstaller code paths are standard Inno `/COMPONENTS` and
  `{param:}` and are believed correct but unproven).
* The uninstaller's interactive options dialog and result dialog
  (`AskUninstallOptions`, `ShowUninstallSummary`) — compiled, never shown.
* The interactive "Installation check" page — compiled, never reached.
* Bundled mode (`/DBundleUsbip`, `/DBundleHidHide`) — compiles by construction
  only (`ExtractTemporaryFile` with `DestName` should be checked once).
* `scripts/uninstall.ps1` real (non-dry) run; `scripts/install.ps1` at all.
* App launch + bridging a pad after an exe install.

## Known bugs / caveats

1. **Test harness (fixed, unverified):** `scratchpad/installer-agent-logs/step.ps1`
   used `param([string]$Args)`; `$Args` is PowerShell's automatic variable and
   the value silently arrived empty, so runs s2 and s3 went out with **no**
   extra switches. Renamed to `-SetupArgs`. The harness now also refuses to
   run if `scratchpad\machine-lock` is held by another agent, takes the lock
   itself for every setup/uninstall launch (app-only runs included — the
   installer decides at run time what is missing), and refuses if an
   `msiexec`/`unins000`/setup process is already running.
2. **Concurrency with another installer** is not detected by the installer
   itself. Two Inno uninstallers of the same product refuse each other
   (exit 1, reported as `[FAIL]`); two different driver installers running at
   once can wedge PnP, which is what happened here. Consider an
   `AppMutex`-style guard around the helper's driver verbs, or at least a
   check for a running `msiexec`/`unins000.exe` before `remove-*`.
3. The `.iss` change after the last build (components/tasks logged at wpReady)
   is not in `dist\ds5bridge-setup-0.4.0.exe` yet.
4. `state.ps1`'s `usbip port` capture printed a PowerShell NativeCommandError
   when the VHCI device was gone (cosmetic, patched to go through `cmd /c`).
5. Elevated Setup writes into `{localappdata}` (`UsedUserAreasWarning=no`):
   correct for an admin account elevating itself (the normal home case);
   a standard user elevating with a different admin account gets the app in
   the wrong profile. Documented in `docs/installer.md`.
6. HidHide's uninstall leaves the service `RUNNING (DeleteFlag=1)` until
   reboot; reinstalling before that reboot is exactly the sequence that
   should not be attempted (s3 did it, involuntarily).

## Post-reboot recovery (machine state expected after the reboot)

Expected: HidHide absent (its MSI install was interrupted; if the MSI
completed instead, `HidHideCLI.exe` exists and the service is present —
either is fine); usbip-win2 files + "USBip version 0.9.7.7" Add/Remove entry
present but its devnode possibly gone/errored; `%LOCALAPPDATA%\ds5bridge`
absent (removed by s2); Add/Remove entries "ds5bridge 0.4.0" absent and the
bundle-agent's "ds5bridge (includes …)" present; HKCU Run `ds5bridge` still
pointing at `dist\ds5bridge\ds5bridge-tray.exe` (pre-existing dev value);
`%APPDATA%\ds5bridge` settings intact.

Steps, all elevated (`sudo powershell -NoProfile -ExecutionPolicy Bypass -File …`),
after taking `scratchpad\machine-lock`:

1. `scratchpad\installer-agent-logs\state.ps1 -Title after-reboot` — read
   before acting. Confirm no `msiexec`, `unins000`, `devnode.exe` running.
2. If `usbip.exe --version` answers 0.9.7.7 but the devnode
   `USBip 3.X Emulated Host Controller` is missing/errored, or either
   `usbip2_*` service is not RUNNING: run
   `"C:\Program Files\USBip\unins000.exe" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART`,
   check `sc query usbip2_filter` / `usbip2_ude` are gone (or DeleteFlag=1 →
   reboot again), then reinstall with the setup exe (step 4).
3. If HidHide is half-present (Add/Remove entry but no `HidHideCLI.exe`, or
   the reverse): `msiexec /X{01E0AB21-D1CC-42B4-9DFF-84FFE4F26DAF} /qn /norestart`
   (product code seen this session), reboot if it reports 3010, then let the
   setup exe reinstall it.
4. Rebuild and install everything:
   `powershell -File app\packaging\build-installer.ps1` then
   `dist\ds5bridge-setup-0.4.0.exe /SILENT /NORESTART /LOG="%TEMP%\ds5-install.log"`.
   Read `%LOCALAPPDATA%\ds5bridge\installer\last-install-check.txt`; expect
   every line `[ok]`/`[info]` except a `[warn] service HidHide` + `[reboot]`
   line if HidHide was freshly installed (its filter loads on the next boot).
5. Then run the intended matrix with the fixed harness
   (`step.ps1 -What uninstall -SetupArgs '/KEEPUSBIP=1' -Tag …`, etc.), one
   step at a time, lock held, never while another agent's installer runs.
6. Leave the machine fully installed; delete `scratchpad\machine-lock`.

---

## Phase A results (installer-recovery agent, 2026-09-04 22:20 → 2026-09-05 02:20)

Single agent, `scratchpad\machine-lock` held for the whole session, one
installer at a time, no reboot, usbipd-win / ViGEm / vJoy untouched, HidHide
never removed. Evidence: `scratchpad/installer-agent-logs/phaseA-*` (Inno
logs `phaseA-NN-*.log`, harness console captures `*-console.txt` with a
BEFORE/AFTER `state.ps1` dump each, `last-install-check.txt` copied inline).

### Starting point (after the parent's HID recovery)

`phaseA-00-state-after-reboot.txt`: usbip-win2 0.9.7.7 intact (services
RUNNING, `ROOT\USB\0000` OK); HidHide completely absent, HIDClass
`UpperFilters` empty (`LowerFilters=steamxbox`); `%LOCALAPPDATA%\ds5bridge`
0.4.0 present with its Add/Remove entry; dev tray (pid 23188, the HKCU Run
autostart of `dist\ds5bridge\ds5bridge-tray.exe`) running -- stopped cleanly
with `setup-helper.ps1 stop-app` (`phaseA-02a-stop-app.txt`). 25
HID/keyboard/mouse devices present, none with Code 32 (only the pre-existing
disabled vJoy node, `CM_PROB_DISABLED`).

### Builds (all `app\packaging\build-installer.ps1`, download mode)

| build | SHA-256 | what changed |
|---|---|---|
| 1 | `26539041F5EE80F0228D993400AD4EA074B6E3AA298E61B1C1516F675E91AB48` | the 21:45 `.iss` as left (uncompiled `Log` line) |
| 2 | `530C772A99E381008A48013E695B4A0B48173596F8F9E831F68AAA30A4948AA2` | hardening (see Code changes) |
| 3 | `CC12E7BAAC8F3DBC9E9FF3B9D09170B5C97615533AFC7A6429763891A72FFEC9` | `app` component fixed, empty selection refused, `-ExpectApp` always |
| 4 | `3F145F955402A11EB789B5097B3F8EBE11227E4ECFD5AB6AAD267BB86E5AC54E` | `$ClassKeyRoot` seam in the helper |
| **5 (final, installed)** | **`F1EC74B193886383D51CACEF748701BCF7548616CF27C84D2F8D9EE3AABB9140`** | `$ServicesKeyRoot` seam in the helper |

Stub compiles of the final `.iss` pass in download mode and in bundled mode
(`/DBundleUsbip=... /DBundleHidHide=...`, 38 MB stub with the notices file).

### What passed (in order run)

| run | what | result | evidence |
|---|---|---|---|
| 02 | **full silent install, HidHide absent** (build 1) -- the never-proven HidHide silent path | **PASS**, exit 0 in 15 s: `HidHide_1.5.230_x64.exe` downloaded (8078016 B), `SHA-256 verified`, `/exenoui /qn /norestart` exit 0; verify all `[ok]`/`[info]`; service HidHide RUNNING at once (so the old code printed no `[warn]`/`[reboot]` line -- fixed, see Code changes) | `phaseA-02-install-full.log`, `-console.txt` |
| 03 | post-install safety check | `UpperFilters=HidHide` on HIDClass, XnaComposite, XboxComposite; `Services\HidHide` present (Start 3, RUNNING); `ROOT\SYSTEM\0008` "Nefarius HidHide Device" OK; 25/25 input devices still OK, 0 x `CM_PROB_DISABLED_SERVICE`; driver store `oem80.inf`; updater task registered | `phaseA-03-post-install-hidclass-check.txt` |
| 04 | helper `check-busy` real -> PASS; `check-busy -MockBusy devnode,msiexec` -> `FAIL|other installers|running: devnode (mock), msiexec (mock)...` exit 1; `preflight` elevated -> PASS | `phaseA-04-guard-mock.txt`, `phaseA-04-preflight.txt` |
| 05 | `scripts\uninstall.ps1 -DryRun` (new code) | PASS, prints plan, changes nothing | `phaseA-05-uninstall-ps1-dryrun.txt` |
| 07 | full install, build 2 (drivers present) | PASS; `preflight` ran in the real flow; adoption records written `UsbipInstalledByUs=0, HidHideInstalledByUs=0` | `phaseA-07-install-full-build2*` |
| 08 | uninstall `/KEEPUSBIP=1 /KEEPHIDHIDE=1` (build 2) | PASS; switches on the Inno command line; app removed, drivers untouched | `phaseA-08-uninstall-keep-both*` |
| 09 | app-only install -- **two harness bugs and one installer bug found** | first attempt passed the switch as literal `\"app\"` (nested quoting); Inno selected nothing and **recorded the empty selection as "previous"**; the second attempt (no switch at all: the `-Components` parameter had not actually been added) reused it and installed only `unins000.exe` + helper while reporting "all checks passed". Fixed: harness rewritten (array ArgumentList, `-Components`, asserts every switch in the log); `.iss`: `app` is `fixed`, `NextButtonClick(wpReady)` refuses an empty selection, `verify-install` always gets `-ExpectApp` | `phaseA-09-install-app-only*` (kept as the failure record) |
| 11 | full install, build 3, `/COMPONENTS="app,usbip,hidhide"` confirmed in log | PASS, app back, stale selection overwritten | `phaseA-11-install-full-build3*` |
| 12 | uninstall `/KEEPUSBIP=1 /KEEPHIDHIDE=1`, switches confirmed | PASS (`remove usbip-win2=no (origin 0), remove HidHide=no (origin 0)`) | `phaseA-12-uninstall-keep-both*` |
| 13 | install `/COMPONENTS="app"` confirmed | PASS: `components selected: app`, `Dest filename: ...\app\ds5bridge.exe`, drivers only verified | `phaseA-13-install-app-only*` |
| 14 | install `/COMPONENTS="app,usbip"` confirmed | PASS: usbip "was already installed", HidHide `[info] not selected`, zero msiexec/download lines, cloak and whitelist unchanged | `phaseA-14-install-app-usbip*` |
| 15 | uninstall with **no switches** | PASS: both drivers kept purely by the adoption default (origin 0) | `phaseA-15-uninstall-default-adopted*` |
| 16 | full install | PASS | `phaseA-16-install-full*` |
| 17 | **real** `scripts\uninstall.ps1 -KeepUsbip -KeepHidHide` (elevated) | PASS in 3 s: bridge stop/cleanup/unhide, shortcut + Inno ARP key + app removed, HidHide kept with own entries cleared, usbip kept; the dev HKCU Run entry (`dist\...`) **left alone** (new behaviour: only a value pointing into `%LOCALAPPDATA%\ds5bridge` is removed) | `phaseA-17-uninstall-ps1-keep-both-console.txt` |
| 18 | orphan-filter repair unit test against scratch HKCU keys (function text lifted from the shipped helper) | PASS 8/8: `HidHide,steamxbox` -> `steamxbox`; `HidHide` alone -> value deleted; other class untouched; no-op while the service key exists; idempotent | `phaseA-18-orphan-repair-unit-test.txt`, `test-orphan-repair.ps1` |
| 20, 24 | full install with builds 4 and 5 | PASS; installed `installer\setup-helper.ps1` hash == source | `phaseA-20-*`, `phaseA-24-*` |
| 21 | installed app `ds5bridge.exe doctor` | healthy: usbip ok, nothing attached, 3241 free, both pads seen, HidHide 1.5.230.0 reachable, cloak on; only warns: one pad at 20 % battery, exe "not whitelisted yet" (expected until the first bridge). Start Menu shortcut -> `%LOCALAPPDATA%\ds5bridge\app\ds5bridge-tray.exe` | `phaseA-21-installed-app-doctor.txt` |
| 22 | final state | app + usbip + HidHide installed, HidHide RUNNING, `UpperFilters=HidHide`, 0 x Code 32, no installer processes, no MSI InProgress, adoption 0/0 | `phaseA-22-state-final.txt` |
| 25 | busy guard against a **real** process: a bare `msiexec.exe` (its help dialog; a client instance, no transaction) | PASS: `check-busy` -> `FAIL|other installers|running: msiexec.exe (pid 16364)...` exit 1; PASS exit 0 again once it was closed | `phaseA-25-guard-real-msiexec.txt` |

### Code changes

`app/packaging/setup-helper.ps1`
* New verbs `check-busy` (FAIL when msiexec [a client instance; the `/V` service host is ignored], `unins*`, `_iu*.tmp`, `_unins*`, `*setup*.tmp`, `devnode`, `nefconw`, `pnputil`, `USBip-*-x64`, `HidHide_*x64` is running -- the helper's own ancestor chain excluded -- or `HKLM\...\Installer\InProgress` exists; `-MockBusy` for tests) and `preflight` (= check-busy, then the orphan repair).
* `Repair-OrphanHidHideFilter`: `HidHide` in `UpperFilters` of HIDClass / XnaComposite / XboxComposite with no `Services\HidHide` -> the entry is removed (other filters kept) and devices with `CM_PROB_DISABLED_SERVICE` are restarted with `pnputil /restart-device`; WARN/PASS lines. Called from `preflight`, `verify-install`, `verify-removed`, `remove-hidhide`. `$ClassKeyRoot` / `$ServicesKeyRoot` exist only as test seams.
* `remove-usbip`: busy check -> stop the app if running -> `attach -X`, `detach -a` -> up to 15 s until `usbip port` is empty **and** no `USB\VID_054C&PID_0CE6\*` devnode is present -> vendor uninstaller under **180 s with `-NoKill`**; on timeout WARN + `REBOOT` "reboot required to finish removing the emulated controller" and **exit 3**. `remove-hidhide`: busy check, orphan repair after the MSI.
* `Run` gained `-NoKill`; `verify-removed` gained `-UsbipPendingReboot`.

`app/packaging/ds5bridge.iss`
* `PrepareToInstall` runs `preflight`; a busy machine aborts a driver install with a message (an app-only run continues); `check-busy` again right before each driver install, `[FAIL]`/`[warn]` + skip when busy.
* Adoption: `HKLM\SOFTWARE\ds5bridge\Setup\{Usbip,HidHide}InstalledByUs` (1 when this setup installed it, 0 when found there, an existing 1 is never downgraded). Uninstaller default = remove ours / keep adopted / remove when there is no record; `/REMOVEUSBIP=1 /REMOVEHIDHIDE=1` added, `/KEEP*` wins; dialog captions show the origin; records deleted only for a removed driver, empty keys cleaned up.
* Uninstaller: `check-busy` before `remove-hidhide` and `remove-usbip` (skipped with `[warn]` when busy); `remove-usbip` exit 3 -> `verify-removed -UsbipPendingReboot`.
* `[reboot] HidHide -- freshly installed...` summary line after a HidHide install; `app` component `fixed`; empty selection refused at wpReady; `-ExpectApp` always.
* Bundled builds also install `bundle\THIRD-PARTY-NOTICES.txt` into `{app}` (`#if Defined(BundleUsbip) || Defined(BundleHidHide)`).

`scripts/uninstall.ps1`
* The same busy guard (the whole script refuses to start; again before each driver), the same orphan repair (start and end), the same adoption records (`-RemoveUsbip` / `-RemoveHidHide` added), the same usbip sequencing and 180 s no-kill timeout with the reboot message; origin values cleaned up; the HKCU Run value is removed only when it points into the install root.

`docs/installer.md`, `docs/USER-GUIDE.md`: adoption defaults, the guards, the usbip timeout/reboot behaviour, the `/REMOVE*` switches, `app` always installed, Inno's previous-selection reuse, bundled notices.

Harness (`scratchpad/installer-agent-logs/`): `step.ps1` rewritten (array args, `-Components`, switch assertion against the Inno log, busy/InProgress refusal, lock holder `installer-recovery`); `state.ps1` fixed (`usbip port` capture, real admin check); `test-orphan-repair.ps1` added.

### Caveats / not proven

* The KEEP switches (runs 08, 12) are confirmed to *arrive* (Inno command line, options log line), but on this machine both drivers are adopted (origin 0), so "kept" would also have been the default. The discriminating test (origin 1 + `/KEEP*`) is safe only once removing a driver is allowed -> post-reboot matrix.
* **HidHide's record on this machine says adopted (0) although build 1 installed it** -- the bookkeeping did not exist yet. A full removal now needs `/REMOVEUSBIP=1 /REMOVEHIDHIDE=1` (exe) or `-RemoveUsbip -RemoveHidHide` (script), or set both values to 1 by hand first to test the "ours" default.
* The busy guard triggered with `-MockBusy` and with a bare `msiexec` help-dialog process; never against a real second installer mid-transaction. `isSetupTmp` matches any `*.tmp` process whose name contains "setup".
* Orphan repair proven on scratch keys only; never run against a real orphan (the live class key was healthy all session).
* usbip-win2 removal (the sequencing, the timeout, exit 3, the pnputil fallback) is **code only**; the vendor uninstaller was not run. `remove-hidhide` never run. Clean-machine usbip install + download never run (only the "already installed" skip path); restore point never created (skip path); interactive pages/dialogs never shown; bundled build never installed; `scripts/install.ps1` untouched and unexercised (it writes no adoption records -- its installs count as "no record" = ours).
* Inno reuses the previous run's component selection when `/COMPONENTS` is absent (`UsePreviousSetupType`); with `app` fixed that can no longer produce an empty install, but a silent test run should always pass the switch.

### Machine now

Fully installed: app 0.4.0 at `%LOCALAPPDATA%\ds5bridge\app`, Start Menu shortcut, Add/Remove "ds5bridge 0.4.0"; usbip-win2 0.9.7.7 RUNNING; HidHide 1.5.230 installed, service RUNNING, class filters registered, **not yet attached to the two already-paired pads -> a REBOOT is required before hide-while-bridged can work**. Two pads paired and present (one at 20 % battery). Nothing attached. HKCU Run is still the dev `dist\...` tray. `scratchpad\machine-lock` deleted at the end of the session.

### Remaining matrix (after the reboot, one step at a time, lock held, `step.ps1` with the switch asserted)

1. `state.ps1 -Title after-reboot-2`: HidHide RUNNING, pads OK, no Code 32; `ds5bridge.exe doctor`.
2. Bridge a pad with the **installed** tray (`%LOCALAPPDATA%\ds5bridge\app\ds5bridge-tray.exe`, non-elevated; the user presses PS); confirm the virtual `USB\VID_054C&PID_0CE6` appears and hide-while-bridged hides the BT devnode; quit the tray, confirm unhide/detach.
3. Set both values under `HKLM\SOFTWARE\ds5bridge\Setup` to 1 (they are ours in truth), then **uninstall `/KEEPUSBIP=1 /KEEPHIDHIDE=1`** -> the discriminating KEEP test; reinstall.
4. **Full uninstall** `step.ps1 -What uninstall -SetupArgs '/REMOVEUSBIP=1 /REMOVEHIDHIDE=1'` (or the default once the records say 1): expect `remove-hidhide` msiexec exit 0/3010 + `[reboot]`, then `remove-usbip`. If the vendor uninstaller returns: services "marked for deletion", `[reboot]`. If it hits the 180 s timeout: exit 3, `[reboot] usbip-win2 -- reboot required...`, `verify-removed -UsbipPendingReboot`; **do not kill devnode.exe; reboot**; afterwards `pnputil /remove-device ROOT\USB\0000 /subtree`, `pnputil /delete-driver oemNN.inf /uninstall` for `usbip2_ude` / `usbip2_filter`, remove `C:\Program Files\USBip` and its ARP entry if still there. Confirm UpperFilters holds no orphaned `HidHide` after the MSI (the helper checks; verify by hand too).
5. Reboot. `state.ps1`: everything absent, input devices OK.
6. **Clean-machine full install** (download of both, restore point, usbip install with the USB-hub blink, HidHide install): expect `[ok] restore point`, `[ok] usbip-win2 installer -- ran (exit 0)`, `[ok] HidHide installer`, `[reboot] HidHide`, records `1/1`. Reboot, verify, bridge a pad.
7. Default uninstall (no switches; records 1/1) -> both removed by default; reboot; reinstall.
8. Interactive: wizard pages (component captions "already installed..."), the Installation check page, the uninstaller options dialog with origin captions, the result dialog -- screenshots.
9. `scripts/install.ps1` end-to-end; a bundled build (`-BundleUsbip -BundleHidHide` from `app\packaging\bundle\vendor\`) installed once; `NOTICE` wording if bundling ships.
10. Optional: a concurrency test against a real second installer (an MSI repair of something harmless in the background) and confirm the exe aborts a driver install / skips a driver removal with the `[FAIL]`/`[warn]` lines.

---

## Phase B results (installer-recovery agent, 2026-09-05 06:20, after the reboot)

Updated as each step completes. Evidence: `scratchpad/installer-agent-logs/phaseB-*`.

| step | what | result | evidence |
|---|---|---|---|
| B0 | state after reboot (uptime 3 min) | HidHide RUNNING, `UpperFilters=HidHide`, usbip2_* RUNNING, 0 input devices in error (only the disabled vJoy node); the DEV tray had autostarted from HKCU Run and had **already bridged pad d42f4ba1485d** (usbip Port 01 in use, virtual `USB\VID_054C&PID_0CE6\2&3B7C36A2&0&1`, HidHide dev-list holding its BT HID devnode). `msiexec.exe /V` (session 0, SYSTEM, since boot) present with no InProgress key. | `phaseB-00-state-start.txt` |
| B1 | hide-while-bridged proof (dev tray) + clean teardown | A non-whitelisted PowerShell opening the BT pad's HID interface: **Access denied** while bridged; helper `teardown -AppDir dist\ds5bridge` stopped both dev processes (WM_CLOSE), `cleanup` unhid d42f4ba1485d, nothing attached, dev-list empty, virtual pad gone; the same open then **succeeded**. HKCU Run value untouched. | `phaseB-01-devtray-hide-proof-and-teardown.txt` |
| B2 | busy guard: msiexec service host vs client | `Test-MsiexecClient` added to the helper (and inline in `uninstall.ps1`): msiexec counts only with `/i /x /f* /p /a /j* /package /uninstall /update` (also `/X{code}` with no space) or `-Embedding`; `msiexec /V` and a bare `msiexec` never count; `-MockBusy` accepts `name:command line`. Unit test `test-busy-guard.ps1`: 10 cases (first run found the `/X{code}` gap, fixed). Real `check-busy` with the `/V` host alive: PASS. | `phaseB-02-busy-guard-unit-test.txt` |
| B3 | **installed tray** (`%LOCALAPPDATA%\ds5bridge\app\ds5bridge-tray.exe`, elevated via sudo) | bridged d42f4ba1485d within 4 s without any button press: API `http://127.0.0.1:8765/api/state` `state=running attached=True hide=True`, usbip Port 01 in use, virtual pad `USB\VID_054C&PID_0CE6\2&3B7C36A2&0&1` OK, dev-list holds the BT devnode, the app registered `...\app\ds5bridge.exe` + `ds5bridge-tray.exe` on the HidHide whitelist itself, non-whitelisted open **Access denied**. | `phaseB-03-installed-tray-bridge.txt` |
| B4 | clean unbridge + stop of the installed tray (helper `teardown -AppDir <installed app>`) | both processes stopped, `cleanup` unhid the pad, nothing attached, virtual pad gone, dev-list empty, non-whitelisted open OK, BT devnode OK, API down. | `phaseB-04-installed-tray-teardown.txt` |
| B5 | build 6 | SHA-256 `C5F4E2679954CA67F4EF5E505E676F7D9E67E7E4890B07474CAFDBE44682BF1E` (helper with `Test-MsiexecClient`) | `phaseB-05-build-6.log` |
| B6 | **discriminating KEEP test**: both `*InstalledByUs` set to 1 by hand, then uninstall `/KEEPUSBIP=1 /KEEPHIDHIDE=1` | PASS: `remove usbip-win2=no (origin 1), remove HidHide=no (origin 1)`; both drivers untouched; the two whitelist entries the app had registered were removed (`hidhide-clear`); records 1/1 survive the uninstall. | `phaseB-06-uninstall-keep-both-ours*` |
| B7 | full reinstall with build 6 | PASS; records stay 1/1 (an existing 1 is never downgraded to 0) | `phaseB-07-install-full-build6*` |
| B8 | **full uninstall `/REMOVEUSBIP=1 /REMOVEHIDHIDE=1`** (records 1/1) | Order as designed: teardown -> `hidhide-clear -All` (cloak off) -> `check-busy` ok -> **`remove-hidhide`: `msiexec /X{01E0AB21-...} exit 0`** -> `check-busy` ok -> **`remove-usbip`: nothing attached; vendor `unins000.exe` started 06:27:40, still running after 180 s -> exit 3**, `[warn] ... left running -- do not end it`, `[reboot] usbip-win2 -- reboot required to finish removing the emulated controller` -> `verify-removed -UsbipPendingReboot` -> app removed, settings kept. The ds5bridge uninstaller finished at 06:30:42 (exit 0, "Need to restart Windows? No" -- Inno's own flag; the `[reboot]` lines carry the truth). | `phaseB-08-uninstall-full.log`, `phaseB-08-uninstall-check.txt` |
| B9 | residue check after B8 | **HidHide: clean.** `UpperFilters` empty on all three classes (the MSI removed them itself; the orphan repair had nothing to do), service `Start=4 DeleteFlag=1` (gone at reboot), `HidHide.sys` file still present (in use until reboot), program dir / driver store (`oem80.inf`) / ARP entry / devnode / updater task all gone; **19 input devices present, all OK, 0 x Code 32** -- the case that stranded the user is handled. **usbip-win2: half removed, reboot-bound.** `devnode.exe remove ROOT\USBIP_WIN2\UDE root` (pid 4440) and the vendor uninstaller (pid 6360) still blocked in `DiUninstallDevice` since 06:27:40 (uncontended this time -- no other installer ran -- so the UDE controller removal on this build simply needs the reboot the vendor flags); `usbip2_filter` STOPPED, `usbip2_ude` RUNNING, `ROOT\USB\0000` still present OK, both driver packages (`oem310/oem314`) and `C:\Program Files\USBip` + ARP entry still present. The harness `step.ps1` also stayed blocked (`Start-Process -Wait` waits for orphaned descendants) -- harmless; its AFTER dump was taken by hand. | `phaseB-09-state-after-full-uninstall.txt` |
| B10 | **busy guard in the real world**, against the blocked `devnode.exe` | (1) helper `check-busy`: `FAIL|other installers|running: unins000.exe (pid 6360), _unins.tmp (pid 12116), devnode.exe (pid 4440)...` exit 1. (2) real elevated `scripts\uninstall.ps1`: `[FAIL] another installer is running (...). Nothing was changed.` exit 1. (3) `ds5bridge-setup-0.4.0.exe /VERYSILENT /SUPPRESSMSGBOXES /COMPONENTS="app,usbip,hidhide"`: downloaded + SHA-256-verified HidHide, `helper preflight exit 1`, `PrepareToInstall failed: Another installer is running on this PC...`, exit code 7, nothing written (no app dir, no ARP entry), devnode untouched. | `phaseB-10-guard-real-world.txt`, `phaseB-10-install-abort-busy.log` |
| B11 | `scripts\install.ps1 -DryRun` | runs, elevation check ok, usbip "already installed" (files still there), would install HidHide + the app; notes that `api.github.com/.../releases/latest` returns 404 (the repo has no release yet), so a real run cannot fetch the app until a release exists. | `phaseB-11-install-ps1-dryrun.txt` |
| B12 | interactive wizard, elevated, cancelled at Ready | Components page: `ds5bridge 0.4.0` greyed (fixed), `usbip-win2 0.9.7.7 -- already installed (will be verified, not reinstalled)`, `HidHide 1.5.230 -- optional ... (downloaded, ~5 MB)`; Tasks page; Ready page with the Plan memo (usbip skip, HidHide download URL, app path); "Exit Setup?" prompt; exit code 2, nothing written. The uninstaller dialogs could not be shown (app not installed; showing them would touch drivers). | `phaseB-ui-1-components.png` ... `phaseB-ui-4-exit-prompt.png`, `phaseB-12-ui-walk-console.txt`, `ui-walk.ps1` |
| B13 | bundled build (`-BundleUsbip -BundleHidHide` from `app\packaging\bundle\vendor\`, hashes verified by the build) | compiles: 72.9 MB, SHA-256 `132C0279FC92A41E46D5057ECF44943FD61E4ABAC53C0B10CBAB6C825027A187`, parked at `dist\bundle\ds5bridge-setup-0.4.0-bundled.exe`. **Not installed** (needs the clean machine after the reboot). | `phaseB-13-build-bundled.log` |
| B14 | final download-mode rebuild (build 7) | byte-identical to build 6: **`C5F4E2679954CA67F4EF5E505E676F7D9E67E7E4890B07474CAFDBE44682BF1E`** = `dist\ds5bridge-setup-0.4.0.exe`, the exe to use after the reboot. | `phaseB-14-build-7.log` |

### Code changes in Phase B

* `app/packaging/setup-helper.ps1`: `Test-MsiexecClient` -- msiexec is busy only with `/i /x /f* /p /a /j* /package /uninstall /update` (also `/X{code}`) or `-Embedding`; the service host `msiexec /V` (session 0, SYSTEM, idle for ~10 min after every boot/install) and a bare `msiexec` never count. `-MockBusy` accepts `name:command line`.
* `scripts/uninstall.ps1`: the same rule inline.
* Harness: `test-busy-guard.ps1` (10 cases), `ui-walk.ps1`.

### Machine at the end of Phase B (06:45, reboot REQUIRED)

* **ds5bridge**: not installed (no `%LOCALAPPDATA%\ds5bridge`, no ARP entry, `HKLM\SOFTWARE\ds5bridge` gone). Settings in `%APPDATA%\ds5bridge` kept. HKCU Run still points at the dev `dist\ds5bridge\ds5bridge-tray.exe` (untouched; it will autostart the dev tray again at the next login).
* **HidHide**: removed by its MSI (exit 0); only `Services\HidHide` with `DeleteFlag=1` and the in-use `HidHide.sys` file remain until the reboot; class filters clean; all input devices OK.
* **usbip-win2**: removal in progress and reboot-bound: `C:\Program Files\USBip\unins000.exe` (pid 6360) / `_unins.tmp` (pid 12116) / `devnode.exe remove ROOT\USBIP_WIN2\UDE root` (pid 4440) blocked since 06:27:40; `usbip2_filter` STOPPED, `usbip2_ude` RUNNING, `ROOT\USB\0000` present, driver store `oem310.inf`/`oem314.inf`, files and the "USBip version 0.9.7.7" ARP entry still present. **Do not end those processes.** The harness `step.ps1` (pid 4424) is blocked waiting on them too -- also harmless.
* Two pads paired; nothing attached; usbipd-win untouched; `scratchpad\machine-lock` deleted.

### After the next reboot (Phase C)

1. `state.ps1 -Title phaseC-start`, then check what the reboot finished: expect `Services\HidHide` gone; for usbip expect either everything gone (the vendor uninstaller resumed and completed its file/registry cleanup before the reboot -- unlikely, it was blocked in the first step) or `ROOT\USB\0000` gone but files/ARP/driver packages still present. In the second case, **alone**, run `"C:\Program Files\USBip\unins000.exe" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART` once more (its `devnode remove` is now a no-op) under the same 180 s rule; if `ROOT\USB\0000` is still listed first: `pnputil /remove-device ROOT\USB\0000 /subtree` (reports reboot-required instead of blocking), then `pnputil /delete-driver oem314.inf /uninstall` (`usbip2_ude`) and `oem310.inf` (`usbip2_filter`), then remove `C:\Program Files\USBip` and the `{199505b0-b93d-4521-a8c7-897818e0205a}_is1` ARP key by hand. Stop the autostarted dev tray first (`setup-helper.ps1 stop-app` / `teardown -AppDir dist\ds5bridge`).
2. Confirm clean: no `usbip2_*` services, no `ROOT\USB\0000`, no HidHide service, input devices OK, `UpperFilters` empty.
3. **Clean-machine full install** with `dist\ds5bridge-setup-0.4.0.exe` (`C5F4E267...`): `step.ps1 -What install -Components app,usbip,hidhide -Tag phaseC-install-clean` -- expect both downloads + SHA-256, `[ok] restore point`, `[ok] usbip-win2 installer -- ran (exit 0)` (USB 3 hubs blink), `[ok] HidHide installer -- ran (exit 0)`, `[reboot] HidHide`, records `1/1`. Then reboot for HidHide, bridge a pad with the installed tray (B3 recipe), confirm hiding.
4. Default uninstall with no switches (records 1/1) -> both removed by default; reboot; reinstall.
5. Bundled exe (`dist\bundle\ds5bridge-setup-0.4.0-bundled.exe`) installed once on the clean machine (no downloads expected in its log; `THIRD-PARTY-NOTICES.txt` in `%LOCALAPPDATA%\ds5bridge`), then the uninstaller's interactive options/result dialogs (screenshots) on that install.
6. `NOTICE` wording if bundling ships; a GitHub release so `scripts\install.ps1` can fetch the app.

---

## Phase C results, part 1 (installer-recovery agent, 2026-09-05 12:38 -> 13:15, after the reboot)

Single agent, `scratchpad\machine-lock` held for the session, one process at
a time, **no PnP removal call issued at all** (see below for why), no reboot.
Evidence: `scratchpad/installer-agent-logs/phaseC-*`.

### The finding that changes the design: usbip-win2 0.9.7.7 cannot be unloaded while Windows runs

| # | evidence | what it shows |
|---|---|---|
| 1 | `phaseC-00-state-start.txt` | After the 12:07 reboot usbip-win2 was **whole again**: `usbip2_filter`/`usbip2_ude` RUNNING, `ROOT\USB\0000` Started (`oem314.inf`), files + ARP entry present. The Phase B removal committed nothing. HidHide gone for good. Dev tray autostarted (pid 25044) -- stopped with `teardown -AppDir dist\ds5bridge` (`phaseC-01a-stop-devtray.txt`). |
| 2 | `phaseC-01b-eventlog-around-hangs.txt`, `phaseC-01d-bugcheck-history.txt` | **Both "reboots" were crashes, not shutdowns.** 2026-09-04 21:58 and 2026-09-05 12:07: `Kernel-Power 41` + `WER 1001: bugcheck 0x9F (0x4, 0x12C, ...)` = DRIVER_POWER_STATE_FAILURE, subcode 4 = "the power transition timed out (0x12C = 300 s) waiting to synchronize with the PnP subsystem". The user asked for a restart at 12:01:43; the machine hung five minutes and blue-screened at ~12:07. Minidumps `C:\WINDOWS\Minidump\090426-31015-01.dmp`, `090526-31046-01.dmp`. |
| 3 | `phaseC-01f-minidump-0905-analyze.txt`, `phaseC-01g-minidump-0904-analyze.txt` (cdb `!analyze -v` + `!thread` of arg3) | **Identical in both dumps.** The thread holding the PnP lock is a system worker thread (`nt!ExpWorkerThread`) at `usbip2_ude+0xbda1` -> `Wdf01000!FxDriver::Unload` -> `Wdf01000!FxDestroy` -> `KeWaitForSingleObject` on a NotificationEvent, waiting 5:00 at crash time. `FxDestroy` waits for the driver's last WDF object to be destroyed; usbip2_ude leaks one, so its **DriverUnload never returns**. Bucket `0x9F_4_ROOT_usbip2_ude!unknown_function`. |
| 4 | `setupapi.dev.log` (quoted in the agent transcript) | Both `[Uninstall device subtree (DiUninstallDevice) - ROOT\USB\0000]` sections (2026-09-04 21:33:38 contended, 2026-09-05 06:27:40 uncontended) contain only `cmd: devnode.exe remove ROOT\USBIP_WIN2\UDE root` and then `<ins>`: never closed, and no pending-removal replay at boot. |
| 5 | `phaseB-15-state-final.txt` (re-read) | While blocked, `usbip port` said `VHCI device not found` although the devnode still showed OK: the **device removal had succeeded**; it was the synchronous driver unload afterwards that hung. |
| 6 | `phaseC-01c-usbip-inf-and-tree.txt`, `phaseC-01h-filter-on-physical-hubs.txt` | `usbip2_filter.inf` is an Extension-class INF matching the generic HWID `USB\ROOT_HUB30`: it is registered under `Filters\*Upper` of **all three** root hubs, the two physical Intel ones included (`Extension Driver Names: oem310.inf`). Disabling or deleting the `usbip2_filter` service would Code-32 every USB 3.0 root hub at boot (keyboard, mouse, everything). Only the vendor's `pnputil /delete-driver oem310.inf /uninstall` may take it out. `usbip2_ude` is the function driver of `ROOT\USB\0000` alone. |
| 7 | `phaseC-01e-phantoms-and-enum-key.txt` | The non-present `USB\VID_054C&PID_0CE6\2&3b7c36a2&0&1..3` records under the emulated hub are the B3/B4 virtual pad (arrived 06:24, removed 06:25); the `5&24b2294e` ones belong to a physical hub (August). Not a cause. `ROOT\USB\0000` ConfigFlags 0, no pending flags. No process holds `libusbip.dll` except `usbipd` (its own copy). |
| 8 | vendor sources (`userspace/innosetup/setup.iss`, `userspace/devnode/main.cpp` at v.0.9.7.7) | `[UninstallRun]`: `devnode.exe remove {HWID} root` (Inno waits), then `pnputil /delete-driver <oem>.inf /uninstall` for both INFs; files/ARP are deleted only after that. `devnode remove` = `SetupDiGetClassDevs(DIGCF_ALLCLASSES)` + `DiUninstallDevice(..., 0, &NeedReboot)`, no handle to the device. So once the devnode is gone by other means the vendor uninstaller completes. Its README lists `pnputil /remove-device /deviceid ROOT\USBIP_WIN2\UDE /subtree` as the alternative -- which reaches the same unload. |

Conclusion: every removal route -- `devnode.exe remove`, `pnputil
/remove-device`, `/disable-device`, `/delete-driver oem314.inf /uninstall` --
ends in PnP unloading `usbip2_ude.sys`, which hangs forever, wedges PnP and
turns the next shutdown into a five-minute hang + 0x9F crash that commits
nothing. **Steps C1 a/b/c were therefore not run**: same kernel path, known
outcome, and each attempt costs the user another crash-reboot. The only
removal that does not reach that unload is one made while the driver is
**not loaded**, i.e. after a boot at which `usbip2_ude` does not start.
Step C1 d (files/ARP now) was also skipped: the vendor uninstaller is still
needed after the reboot for the filter package on the physical hubs.

### What was done

| step | what | result | evidence |
|---|---|---|---|
| C1 | `remove-usbip` redesigned (helper + `scripts/uninstall.ps1`), see Code changes | two halves: **stage A** while the driver is loaded = `sc config usbip2_ude start= disabled` (that service only), copy the helper to `%ProgramData%\ds5bridge\`, register the logon task *ds5bridge finish usbip-win2 removal* (BUILTIN\Administrators, RunLevel Highest, AtLogOn + 30 s, `ExecutionTimeLimit PT0S` = the scheduler never kills it, hidden window, log `%ProgramData%\ds5bridge\usbip-removal.txt`), `[reboot]` line, exit 3; **stage B** when the driver is not loaded = `pnputil /remove-device ROOT\USB\0000 /subtree` (120 s, no-kill), vendor `unins000.exe /VERYSILENT` (180 s, no-kill; its `devnode remove` is a no-op now), then leftovers (`pnputil /delete-driver <oem>.inf /uninstall /force` for INFs mentioning `usbip2_`, `C:\Program Files\USBip`, the `USBip version*` ARP key), task + copy removed. `preflight` and `verify-install` FAIL while `usbip2_ude` has `Start=4`. | parse 0 errors; `check-busy`, `verify-install`, `test-busy-guard.ps1` (10/10), `uninstall.ps1 -DryRun` all fine; `.iss` stub-compiles |
| C1-A | **stage A for real**, elevated `setup-helper.ps1 remove-usbip` | PASS in 2 s: `usbip2_ude Start 3 -> 4`, `usbip2_filter Start 3` (untouched), `%ProgramData%\ds5bridge\setup-helper.ps1` present, task `Ready` with the expected action/principal/trigger/`PT0S`, helper exit 3, **no pnputil/devnode process started**, driver still RUNNING (so the shutdown will be clean). | `phaseC-04-remove-usbip-stageA.txt`, `-lines.txt` |
| C1-B | build 8 (download mode) | `dist\ds5bridge-setup-0.4.0.exe` **SHA-256 `1E4EC1B3F8D70F848E0190A592ED81E37AC23AC269C9F0455C7ABEF1EBC2FDD6`** (40 554 645 B) -- the exe for the clean install. The bundled exe (`132C0279...`) still carries the old helper: rebuild it with `-BundleUsbip -BundleHidHide` before installing it. | `phaseC-05-build-8.log` |
| C1-C | pending-removal guard | `preflight` -> `FAIL|usbip-win2|a removal of usbip-win2 is waiting for a reboot ...` exit 1; `verify-install -ExpectUsbip` carries the same FAIL. | `phaseC-06-pending-guard.txt` |
| C1-D | **new exe against the pending state**: `step.ps1 -What install -Components app,usbip,hidhide` | exit 7 after 55 s: HidHide downloaded + `SHA-256 verified` (Inno's download page runs before PrepareToInstall -- 8 MB wasted, cosmetic), `helper preflight exit 1`, `PrepareToInstall failed: A driver cannot be installed right now: either another installer is running ... or a removal of usbip-win2 is waiting for a reboot ...`; nothing written (no app dir, no ARP entry), drivers untouched. | `phaseC-07-install-refused-pending*` |
| C2 / C3 | clean check + clean install | **not run**: reboot boundary reached by design (stage A). | `phaseC-08-state-end.txt` |

### Code changes in Phase C

* `app/packaging/setup-helper.ps1`: `Invoke-RemoveUsbip` split into `Invoke-UsbipRemovalStageA` / `-StageB` (above); `Test-UsbipDriverLoaded`, `Test-UsbipRemovalPending`, `Test-UsbipRemovalPendingFail` (called by `preflight` and `verify-install`); constants `$UsbipPnpTimeoutSec=120`, `$UsbipFinishDir`, `$UsbipFinishTask`, `$UsbipUdeService`, `$UsbipDevnodeId`; "not installed" only when exe, uninstaller, devnode and service are all absent; a missing vendor uninstaller is a WARN, not a stop; `verify-removed -UsbipPendingReboot` text; header help.
* `scripts/uninstall.ps1`: the same two halves inline in `Remove-Usbip`; `Get-UsbipFinisherScript` (helper copy from the install -- cached to `%TEMP%` before `Remove-App` -- or the repo checkout, or the raw file on GitHub; without one, stage A only prints the reboot + "run again" instructions); constants incl. `$RawBase`; `-DryRun` wording; "not installed" test widened.
* `app/packaging/ds5bridge.iss`: PrepareToInstall abort text covers the pending removal; comment at the `remove-usbip` call. No flow change (exit 3 -> `verify-removed -UsbipPendingReboot` as before).
* `docs/installer.md` (uninstall section rewritten around the two halves, sample output, "Things to know"), `docs/USER-GUIDE.md` (uninstall paragraph).
* Harness: `pnp-run.ps1` (detached timed runner for PnP commands; written, not needed in the end), `state-end-phaseC.ps1`.
* Tooling installed on the machine: **WinDbg 1.2606.22001.0** (per-user MSIX via `Add-AppxPackage` from `https://aka.ms/windbg/download` after `winget` stalled -- a stalled `winget.exe` pid 14256 may still be around, harmless), symbol cache `%LOCALAPPDATA%\symbols`. `cdb.exe`: `C:\Program Files\WindowsApps\Microsoft.WinDbg_1.2606.22001.0_x64__8wekyb3d8bbwe\amd64\cdb.exe`.

### Machine at the end of Phase C part 1 (13:15, reboot REQUIRED -- and it should be a clean one this time)

* **usbip-win2 0.9.7.7**: files, ARP entry "USBip version 0.9.7.7", both driver packages (`oem310.inf`, `oem314.inf`) present; `usbip2_filter` RUNNING Start=3 (on all three root hubs); `usbip2_ude` RUNNING **Start=4** (will not start at the next boot -> `ROOT\USB\0000` comes up Code 32 with no stack, so nothing has to be unloaded); `ROOT\USB\0000` still Started right now. **No process is blocked in PnP** (no devnode/pnputil/unins000), so the shutdown should be normal -- the previous two crashed only because of the blocked unload.
* **Scheduled task** `ds5bridge finish usbip-win2 removal` (Ready): at the next logon of an administrator, +30 s, hidden `powershell -File C:\ProgramData\ds5bridge\setup-helper.ps1 remove-usbip -Out C:\ProgramData\ds5bridge\usbip-removal.txt`. It first stops the dev tray (HKCU Run autostarts it; `stop-app` closes it -- relaunch it by hand afterwards), then stage B; USB 3.0 devices blink once when the filter package comes off the physical hubs. Expected log: `PASS|usbip-win2 devnode|pnputil /remove-device exit 0 ...`, `PASS|usbip-win2 uninstaller|ran (exit 0)`, then `usbip.exe` gone, services gone (or `marked for deletion` + a second reboot), `PASS|usbip-win2 removal task|removed`.
* **HidHide**: absent. **ds5bridge**: not installed; settings in `%APPDATA%\ds5bridge` kept; HKCU Run still the dev `dist\...` tray. usbipd-win / ViGEm / vJoy untouched. No pads on. `scratchpad\machine-lock` deleted.

### After the next reboot (Phase C, continued)

1. Wait about a minute after logon, then `state-end-phaseC.ps1 -Title phaseC-10-after-reboot` (it prints `C:\ProgramData\ds5bridge\usbip-removal.txt` too) **before anything else**. Expect: no `usbip.exe`, no ARP entry, `usbip2_ude` absent, `usbip2_filter` absent or `DeleteFlag=1` (then one more reboot before the clean install), `ROOT\USB\0000` gone, the task gone, all input devices OK. Also `Get-Process pnputil,devnode,unins000` -- if stage B blocked (it should not: nothing to unload), the log names the step and the pending-reboot rule applies (never kill; reboot).
   * If the task did **not** run (no log): `sudo powershell -File app\packaging\setup-helper.ps1 remove-usbip -Out <file>` -- it sees the driver is not loaded and does stage B directly.
   * If `usbip2_filter` is `marked for deletion` or pnputil reported 3010: reboot once more before C3.
2. **C2** `state.ps1 -Title phaseC-02-clean`: no `usbip2_*`, no `ROOT\USB\0000`, no HidHide, `UpperFilters` empty, input OK.
3. **C3** clean-machine full install with build 8 (`1E4EC1B3...`): `step.ps1 -What install -Components app,usbip,hidhide -Tag phaseC-03-install-clean` -- both downloads + SHA-256, `[ok] restore point` (first time; record the wording if System Protection is off for C:), `[ok] usbip-win2 installer -- ran (exit 0)`, `[ok] HidHide installer -- ran (exit 0)`, `[reboot] HidHide`, records 1/1; verify driver store, services, `UpperFilters=HidHide` on the three classes, `ROOT\USB\0000` OK, 0 x Code 32; installed `ds5bridge.exe doctor`. Reboot (HidHide).
4. Bridge a pad with the installed tray (B3 recipe; the user powers on the blue pad), then **default uninstall with no switches** (records 1/1): expect HidHide MSI exit 0 + the new usbip stage A lines + exit 3 path (`[reboot] usbip-win2 -- its driver can only be removed after a REBOOT ...`), uninstaller exit 0; reboot; confirm the task finished stage B (log). That is the full proof of the new path through the exe.
5. Rebuild the bundled exe (`build-installer.ps1 -BundleUsbip app\packaging\bundle\vendor\USBip-0.9.7.7-x64.exe -BundleHidHide app\packaging\bundle\vendor\HidHide_1.5.230_x64.exe`), install it on the clean machine (no downloads in its log, `THIRD-PARTY-NOTICES.txt` in `%LOCALAPPDATA%\ds5bridge`), then the uninstaller's interactive options/result dialogs with screenshots (`ui-walk.ps1` pattern) -- the result dialog now shows the stage A lines.
6. `NOTICE` wording if bundling ships; a GitHub release so `scripts\install.ps1` can fetch the app. Consider reporting the `FxDestroy` unload hang upstream (vadimgrn/usbip-win2 0.9.7.7; both dumps say `usbip2_ude+0xbda1`).
7. Cosmetic: run the driver downloads after `PrepareToInstall` (Inno's download page runs first, so a refused install still fetches HidHide).

---

## Phase C results, part 2 (installer-recovery agent, 2026-09-05 13:24 -> 13:45, after the second reboot)

Lock held, dev tray stopped first (`phaseC-09a-stop-devtray.txt`), no PnP
call issued, no reboot. Evidence `phaseC-09*`.

### The reboot was clean and stage B did its job

`phaseC-09-probe-after-reboot.txt`: shutdown at 13:20:58 via the normal
kernel power path, **no Kernel-Power 41 / WER 1001** (the first non-crash
reboot with usbip-win2 involved). `C:\ProgramData\ds5bridge\usbip-removal.txt`
(written by the logon task): `PASS|usbip-win2|nothing attached`,
`PASS|usbip-win2 devnode|pnputil /remove-device exit 0: ... Device removed
successfully`, `PASS|usbip-win2 uninstaller|ran (exit 0)`,
`PASS|usbip-win2 removal task|removed`. Result: `usbip.exe` absent, no
"USBip" Apps entry, `ROOT\USB\0000` gone (`Enum\ROOT\USB` empty), no
`oem*.inf` mentioning usbip2, no `usbip2*` DriverStore directories or
`.sys` files, both physical root hubs Started with **no `Filters` key** and
no extension driver, `driverquery` / `Win32_SystemDriver` show **no usbip2
module loaded**, task gone, `%ProgramData%\ds5bridge` holds only the log.
Two pads on and OK (2 HID devnodes). Only errored devices: the pre-existing
disabled vJoy node and the disabled Iriun webcam. `msiexec` pid 13700 is the
`/V` service host (session 0). **Two-half removal proven end to end.**

### But: the service names are still owned by the SCM

| what | result | evidence |
|---|---|---|
| `Services\usbip2_ude`, `Services\usbip2_filter` | both present as tombstones: `Start=4`, `DeleteFlag=1`, `DriverDelete`, `ImagePath` pointing at deleted DriverStore files, `sc query` = STOPPED (exit code 1077, never started this boot) | `phaseC-09-probe-after-reboot.txt` |
| **`sc create usbip2_ude` / `usbip2_filter`** (dummy path, would have been deleted at once) | **`CreateService FAILED 1073: The specified service already exists`** for both -- the Service Control Manager still holds the names until the next boot. The vendor installer's INF `AddService` would land on a service marked for deletion and fail (1072) -> half-installed usbip-win2. **A fresh install now is not safe.** | `phaseC-09b-createservice-probe.txt` |
| guard added: `Get-UsbipRemovalPending` -> `'deleting'` when either `usbip2_*` key has `DeleteFlag=1` (`'disabled'` = stage A's Start=4 as before); `preflight` / `verify-install` FAIL with `its driver services from a previous install are still marked for deletion ... Reboot, then run this setup again`; `verify-removed` keeps reporting them as PASS + `[reboot]` | `phaseC-09c-deleteflag-guard.txt` |
| build 9 | **`dist\ds5bridge-setup-0.4.0.exe` SHA-256 `7BDA54B8C19467BDE26E2F3EB916674C260757B30A192FC63A857F94777772AC`** -- the exe for the clean install | `phaseC-09e-build-9.log` |
| **build 9 vs. the tombstones**: `step.ps1 -What install -Components app,usbip,hidhide` | exit 7, nothing written, drivers untouched. Bonus: the **usbip-win2 download path is now proven** -- `downloaded USBip-0.9.7.7-x64.exe (33226344 bytes)`, `SHA-256 verified` (then HidHide, same). The run took 453 s only because Inno shows the PrepareToInstall error as a modal box in `/SILENT` mode (only `/VERYSILENT /SUPPRESSMSGBOXES` hides it); it was dismissed with an elevated `{ENTER}`. No PnP process was ever involved. | `phaseC-09d-install-refused-deleteflag*` |
| C2 / C3 | **not run**: a reboot must clear the SCM tombstones first. | `phaseC-09f-state-end.txt` |

### Side findings (app, not installer)

* Dev `ds5bridge.exe cleanup` with usbip.exe absent crashes: `stop_auto_reattach` -> `subprocess.run` -> `OSError: [WinError 193] %1 is not a valid Win32 application` (`ds5app/usbip.py:193/167`) -- the usbip path resolves to something that is not an exe when the install is gone. `ds5bridge unhide` says "HidHide is installed but would not tell us what it is hiding" while HidHide is absent. Both in `phaseC-09a-stop-devtray.txt`.

### Code changes in part 2

`app/packaging/setup-helper.ps1`: `Get-UsbipRemovalPending` (`''` / `'disabled'` / `'deleting'`), `Test-UsbipRemovalPending` now a wrapper, `Test-UsbipRemovalPendingFail` with the second message. `docs/installer.md`: the uninstall -> reboot -> reboot -> reinstall sequence and the `/SILENT` error-box note. Harness: `probe-pending-services.ps1`, `probe-createservice.ps1`.

### Machine at the end of part 2 (13:45, reboot REQUIRED, will be clean)

* usbip-win2: **gone** except the two SCM tombstones `Services\usbip2_ude` / `usbip2_filter` (`DeleteFlag=1`, nothing loaded, nothing on disk). Nothing blocked in PnP, no installer processes (`msiexec /V` host only), no MSI InProgress. Physical hubs clean.
* HidHide absent; ds5bridge not installed; `HKLM\SOFTWARE\ds5bridge` absent; settings kept; HKCU Run still the dev tray (stopped now, autostarts at logon); usbipd-win/ViGEm/vJoy untouched. Both pads ON and present. `scratchpad\machine-lock` deleted.

### After the next reboot (Phase C, continued)

1. `probe-pending-services.ps1 -Title phaseC-10-after-reboot` (elevated): expect **both `usbip2_*` keys ABSENT**; if a key is still there with `DeleteFlag=1`, stop and report (that would mean the SCM did not reap it -- then and only then consider deleting the tombstone key by hand after another `sc create` probe). Stop the dev tray (`teardown -AppDir dist\ds5bridge`).
2. **C2** `state.ps1 -Title phaseC-02-clean` + `preflight` -> PASS.
3. **C3** clean-machine full install with **build 9** (`7BDA54B8...`): `step.ps1 -What install -Components app,usbip,hidhide -Tag phaseC-03-install-clean`. Both downloads are already proven; expect `[ok] restore point` (first time), `[ok] usbip-win2 installer -- ran (exit 0)` (hubs blink), `[ok] HidHide installer -- ran (exit 0)`, `[reboot] HidHide`, records 1/1, `UpperFilters=HidHide` x3, `ROOT\USB\0000` OK, 0 x Code 32, installed `doctor`. Note the pads are ON: HidHide's install with pads present is fine (it only attaches after the reboot). Then reboot.
4. Bridge with the installed tray (pads are already on), then **default uninstall, no switches** (records 1/1): stage A lines + exit 3 + `[reboot]`; reboot; task log; **second reboot** for the tombstones; reinstall.
5. Bundled exe rebuilt from the current `.iss`/helper, installed once; uninstaller dialogs with screenshots; `NOTICE`; GitHub release; upstream report of the `FxDestroy` unload hang; move downloads after `PrepareToInstall`; fix the two app side findings.

---

## Phase C results, part 3 (installer-recovery agent, 2026-09-05 13:44 -> 13:55, after the third reboot) -- THE CLEAN-MACHINE INSTALL

Lock held, dev tray stopped first (`phaseC-10a-stop-devtray.txt`, same
`cleanup` traceback as before), no reboot. Evidence `phaseC-10*`..`13*`.

| step | what | result | evidence |
|---|---|---|---|
| C2 | clean check after the third (clean) reboot | `usbip2_filter`, `usbip2_ude`, `HidHide` services **ABSENT** (the SCM tombstones were reaped at boot as predicted), no devnode, no `oem*.inf`/DriverStore/`.sys` for usbip2, physical hubs without `Filters` key, HidHide class filters empty, only the pre-existing disabled vJoy/Iriun nodes, `msiexec` 11120 = `/V` host, both pads ON and OK. | `phaseC-10-clean-check.txt` |
| harness | `step.ps1` refused with `installer processes already running: msiexec#11120` | its own pre-check read `$_.CommandLine` from `Get-Process`, which does not exist in Windows PowerShell 5.1 (so the `/V` host was never recognised); fixed to read the command line via CIM. (The helper's `Test-MsiexecClient` was right all along.) | `step.ps1` |
| **C3** | **clean-machine full install, build 9** (`7BDA54B8...`), `step.ps1 -What install -Components app,usbip,hidhide` | **PASS, exit 0 in 63 s**: `components selected: app,usbip,hidhide; tasks selected: restorepoint`; `downloaded USBip-0.9.7.7-x64.exe (33226344 bytes)` + `SHA-256 verified`; `downloaded HidHide_1.5.230_x64.exe (8078016 bytes)` + `SHA-256 verified`; `preflight` PASS; `teardown` (nothing to do); **`[ok] restore point -- created: "Before usbip-win2 (ds5bridge installer)"`** (first ever; `Get-ComputerRestorePoint` #254, 13:47:18); `check-busy` PASS; **`[ok] usbip-win2 installer -- ran (exit 0)`** (13:47:27 -> 13:47:44, hubs blinked); `check-busy` PASS; **`[ok] HidHide installer -- ran (exit 0)`** (4 s); `[reboot] HidHide -- freshly installed; ...`; `verify-install` all `[ok]`/`[info]`. | `phaseC-11-install-clean.log`, `-console.txt` |
| C3 verify | elevated post-install check | adoption **`UsbipInstalledByUs=1 HidHideInstalledByUs=1`**; `UpperFilters=HidHide` on HIDClass, XnaComposite, XboxComposite (`LowerFilters=steamxbox` kept); `usbip2_filter`/`usbip2_ude`/`HidHide` RUNNING Start=3; driver store `oem80.inf` (usbip2_filter), `oem186.inf` (usbip2_ude), `oem303.inf` (HidHide); `ROOT\USB\0000` Started with child `USB\ROOT_HUB30\1&2b53a856&1&0`; extension `oem80.inf` back on both physical hubs; `ROOT\SYSTEM\0008` HidHide device OK; **19 input devices, 0 x Code 32** (only the disabled vJoy node); cloak off, dev-list empty, app-list = HidHideCLI only; updater task registered; installed `installer\setup-helper.ps1` hash == source; Start Menu shortcut; ARP entries HidHide 1.5.230, USBip 0.9.7.7, ds5bridge 0.4.0. | `phaseC-12-post-install-verify.txt` |
| C3 doctor | installed `ds5bridge.exe doctor` (non-elevated) | healthy: usbip 0.9.7.7, nothing attached, 3241 free, both pads (a0fa9c0dd8bb 90 %, d42f4ba1485d 70 %), HidHide 1.5.230.0 reachable, cloak off; the only warn is `granting -- not whitelisted yet` (expected until the first bridge; the app registers itself then, as B3 showed). `at login` still reports the dev tray path. | `phaseC-13-installed-doctor.txt` |

**The main cycle is now proven on this machine: full uninstall (two halves)
-> clean machine -> full install with restore point, both downloads and
both driver installs.** Not yet proven through the exe: the default
uninstall with records 1/1 (stage A via `unins000.exe`), and the bundled
build.

### Code changes in part 3

Harness only: `step.ps1` busy pre-check via CIM; `verify-post-install.ps1`
added (reusable after every install).

### Machine at the end of part 3 (13:55, reboot REQUIRED for HidHide)

* **Fully installed**: app 0.4.0 at `%LOCALAPPDATA%\ds5bridge\app` (+ `installer\`, `unins000.exe`), Start Menu shortcut, ARP "ds5bridge 0.4.0"; usbip-win2 0.9.7.7 RUNNING (`ROOT\USB\0000` OK); HidHide 1.5.230 RUNNING, class filters registered, records 1/1. **HidHide is not yet attached to the two already-paired pads -> reboot before any hide test.**
* Both pads ON and present (2 HID devnodes OK). Nothing attached. No installer processes (only `msiexec /V`), no MSI InProgress. HKCU Run still the dev tray (stopped; it autostarts at logon and will bridge a pad on its own -- stop it with `teardown -AppDir dist\ds5bridge` before using the installed tray). usbipd-win / ViGEm / vJoy untouched. Restore point #254 exists. `scratchpad\machine-lock` deleted.

### After the next reboot (Phase C, continued)

1. `verify-post-install.ps1 -Title phaseC-14-after-reboot` (elevated): HidHide RUNNING, pads OK, 0 x Code 32. Stop the dev tray (`teardown -AppDir dist\ds5bridge`) -- it will have autostarted and probably bridged a pad; confirm nothing attached afterwards.
2. **Bridge test with the installed tray** (B3 recipe: `%LOCALAPPDATA%\ds5bridge\app\ds5bridge-tray.exe`, API `http://127.0.0.1:8765/api/state`): needs the **blue pad d42f4ba1485d** on; expect `attached=True hide=True`, virtual `USB\VID_054C&PID_0CE6\...` OK, BT devnode in HidHide's dev-list, non-whitelisted open denied; then `teardown -AppDir %LOCALAPPDATA%\ds5bridge\app` and confirm unhide/detach.
3. **Default uninstall, no switches** (records 1/1): `step.ps1 -What uninstall -Tag phaseC-15-uninstall-default-ours` (use `/VERYSILENT /SUPPRESSMSGBOXES` in `-SetupArgs` only if a refusal is expected). Expect: teardown, `hidhide-clear -All`, `remove-hidhide` msiexec exit 0, `remove-usbip` **stage A** lines (`usbip2_ude set to not start...`, `scheduled for the next logon...`, `[reboot] usbip-win2 -- its driver can only be removed after a REBOOT ...`), exit 3 -> `verify-removed -UsbipPendingReboot`, app removed, uninstaller exit 0 -- the harness `step.ps1` will NOT hang this time (no orphaned vendor process). Then reboot; at logon the task runs stage B (log `C:\ProgramData\ds5bridge\usbip-removal.txt`); then **one more reboot** (SCM tombstones) before any reinstall -- confirm `preflight` says `deleting` in between and PASS after.
4. Bundled exe: `build-installer.ps1 -BundleUsbip app\packaging\bundle\vendor\USBip-0.9.7.7-x64.exe -BundleHidHide app\packaging\bundle\vendor\HidHide_1.5.230_x64.exe` (the old `132C0279...` has the pre-Phase-C helper), install it on the clean machine (no download lines, `THIRD-PARTY-NOTICES.txt` in `%LOCALAPPDATA%\ds5bridge`), then the uninstaller's interactive options dialog (origin captions "installed by this setup") and result dialog with screenshots (`ui-walk.ps1` pattern; the result dialog shows the stage A lines and the reboot sentence).
5. `NOTICE` wording if bundling ships; GitHub release for `scripts\install.ps1`; upstream report of the `FxDestroy` unload hang; move the downloads after `PrepareToInstall`; app fixes (`cleanup` with usbip.exe absent, `unhide` HidHide wording, `doctor` "at login" shows the dev path).

---

## Phase C results, part 4 (installer-recovery agent, 2026-09-05 14:18 -> 14:30, after the HidHide reboot) -- BRIDGE + DEFAULT UNINSTALL THROUGH THE EXE

Lock held, no PnP call issued by anything of ours, no reboot. Evidence `phaseC-14*`..`19*`. Probe script `bridge-probe.ps1` (API state, `usbip port`, virtual pad, BT devnodes, HidHide lists, a non-whitelisted `CreateFile` on each BT pad's HID interface, the hide journal).

| step | what | result | evidence |
|---|---|---|---|
| C-14 | HidHide attached after the reboot: the **dev** tray had autostarted and bridged the blue pad on its own | `cloak-on`, dev-list = the pad's BT devnode, `usbip port` Port 01 in use, virtual `USB\VID_054C&PID_0CE6\2&22E30EF8&0&1` OK, aggregate `attached:1, reports_per_s:250`; non-whitelisted open **REFUSED (Access denied)**. `teardown -AppDir dist\ds5bridge`: 2 processes stopped, `cleanup` unhid d42f4ba1485d, nothing attached, virtual pad gone, dev-list empty, API down, the same open then **OPENED OK** (CreateFile succeeds; .NET only rejects the device type afterwards). BT devnode stays OK throughout. | `phaseC-14-devtray-hide-proof-and-teardown.txt` |
| C-15 | **installed tray** (`%LOCALAPPDATA%\ds5bridge\app\ds5bridge-tray.exe`, elevated via sudo, no button press) | API up after 2 s, `attached:1` after 5 s; controller `d42f4ba1485d state=running attached=true hide_bluetooth=true battery 70 % reports_per_s 252`; Port 01 in use; virtual pad OK; dev-list holds the BT devnode; the app **registered `...\app\ds5bridge-tray.exe` and `...\app\ds5bridge.exe` on the whitelist itself**; open **REFUSED**; journal `d42f4ba1485d.json`. | `phaseC-15-installed-tray-bridge.txt` |
| C-16 | `teardown -AppDir <installed app>` | 2 processes stopped, unhid d42f4ba1485d, nothing attached, virtual pad gone, dev-list empty, journal empty, open **OPENED OK**, API down. | `phaseC-16-installed-tray-teardown.txt` |
| **C-17** | **DEFAULT uninstall through the exe, no switches, records 1/1**: `step.ps1 -What uninstall` | **PASS, exit 0 in 20 s, harness returned normally** (stage A spawns nothing). `uninstall options: remove usbip-win2=yes (origin 1), remove HidHide=yes (origin 1), purge settings=no` -> teardown (nothing running) -> `hidhide-clear -All`: `removed 2 ds5bridge entry(ies)`, `unhid 0 device(s), cloak off` -> `check-busy` -> **`remove-hidhide`: msiexec /X{01E0AB21-...} exit 0** -> `check-busy` -> **`remove-usbip` exit 3**: `nothing attached`, `usbip2_ude set to not start at the next boot`, `scheduled for the next logon (task ...)`, `[reboot] usbip-win2 -- its driver can only be removed after a REBOOT; ...` -> `verify-removed -UsbipPendingReboot -ExpectNoHidHide`: `[info] usbip-win2 -- its driver is disabled ...`, `[ok] HidHideCLI.exe -- gone`, `[ok] service HidHide -- marked for deletion`, `[reboot] HidHide`, `[ok] Bluetooth DualSense -- HID devnode(s) present and OK` -> `[ok] app -- %LOCALAPPDATA%\ds5bridge removed`, `[info] settings -- kept`. | `phaseC-17-uninstall-default.log`, `-console.txt` |
| C-17 verify | elevated | `HKLM\SOFTWARE\ds5bridge` **gone** (records removed with the drivers); `UpperFilters` **empty on all three classes** (Lower too -- `steamxbox` is gone as well: the HidHide MSI rewrites the class filters; it was `steamxbox` before the install and is absent now; note for Steam Input users, not ours to fix); HidHide program dir, ARP entry, devnode, updater task gone, `HidHide.sys` still on disk (in use until reboot), service `RUNNING (DeleteFlag=1)`; **18 input devices, 0 x Code 32**; task `ds5bridge finish usbip-win2 removal` Ready; `%ProgramData%\ds5bridge\setup-helper.ps1` staged; HKCU Run dev entry untouched; no `msiexec` client / PnP process; no MSI InProgress. | `phaseC-17b-post-uninstall-verify.txt`, `phaseC-19-state-end.txt` |
| C-18 | bundled exe rebuilt with the build-9 helper | `build-installer.ps1 -BundleUsbip ... -BundleHidHide ...` writes to `dist\ds5bridge-setup-0.4.0.exe` (it **overwrote build 9**); moved by hand to **`dist\bundle\ds5bridge-setup-0.4.0-bundled.exe`, 76 479 940 B, SHA-256 `94511B0B9DD9B1D6DC9BB1578A18CBB9B2DF3DBDDFACEFA7868530547CADFA92`**; download mode rebuilt afterwards and **byte-identical to build 9 (`7BDA54B8...`)** -- builds are deterministic. The old `132C0279...` bundled exe is gone; `ds5bridge-bundle-setup.exe` (bundle-agent's) untouched. Not installed. | `phaseC-18-build-bundled.log`, `phaseC-18b-build-9-again.log` |

**Now proven through the exe, on this machine: clean install (restore
point, both downloads, both drivers, records 1/1) -> bridge with hide ->
default uninstall removing both drivers (two-half usbip path, exit 3,
`[reboot]`).** Remaining: the post-reboot task run for *this* uninstall,
the bundled install, the interactive uninstaller dialogs.

### Code changes in part 4

Harness only: `bridge-probe.ps1` (new). Everything else unchanged since part 3.

### Machine at the end of part 4 (14:30, reboot REQUIRED -- clean, nothing blocked)

* **ds5bridge**: not installed (no `%LOCALAPPDATA%\ds5bridge`, no ARP entry, no shortcut, `HKLM\SOFTWARE\ds5bridge` gone). Settings kept in `%APPDATA%\ds5bridge`. HKCU Run still the dev tray (stopped; autostarts at logon).
* **HidHide**: removed by its MSI (exit 0); `Services\HidHide` RUNNING `DeleteFlag=1` and `HidHide.sys` on disk until the reboot; class filters clean; all input devices OK.
* **usbip-win2 0.9.7.7**: stage A done -- `usbip2_ude` **Start=4** (still RUNNING now), `usbip2_filter` RUNNING Start=3, `ROOT\USB\0000` OK, files + "USBip" ARP entry present; logon task armed (`%ProgramData%\ds5bridge\setup-helper.ps1`, log `usbip-removal.txt` -- the previous run's log is still there and gets appended). **No process is blocked in PnP.**
* Blue pad d42f4ba1485d ON and present (a0fa9c0dd8bb off). usbipd-win / ViGEm / vJoy untouched. `dist\ds5bridge-setup-0.4.0.exe` = build 9, `dist\bundle\ds5bridge-setup-0.4.0-bundled.exe` = `94511B0B...`. `scratchpad\machine-lock` deleted.

### After the next reboot (Phase C, continued)

1. **Reboot 1** (this one): at logon (+30 s) the task runs stage B; the dev tray autostarts too and the task's `stop-app` closes it. Then `probe-pending-services.ps1 -Title phaseC-20-after-reboot` (elevated): expect `usbip-removal.txt` with the four PASS lines again, `usbip.exe` absent, devnode gone, hubs without `Filters`, task gone, `Services\HidHide` gone, `usbip2_*` as `DeleteFlag=1` tombstones, input OK. `preflight` -> FAIL `deleting`.
2. **Reboot 2** (tombstones): `probe-pending-services.ps1 -Title phaseC-21-clean`: `usbip2_*` ABSENT; `preflight` PASS. Stop the dev tray.
3. **Bundled install**: `step.ps1` uses `dist\ds5bridge-setup-0.4.0.exe`; either point it at the bundled exe (add a `-Setup <path>` parameter) or copy the bundled exe over it for that run and restore build 9 afterwards. Expect: no `Downloading temporary file` lines, `ExtractTemporaryFile` of both vendor exes, `THIRD-PARTY-NOTICES.txt` in `%LOCALAPPDATA%\ds5bridge`, otherwise the same summary as C3 (restore point, both installers exit 0, `[reboot] HidHide`, records 1/1).
4. Interactive uninstaller dialogs on that install (elevated `unins000.exe` without `/SILENT`, `ui-walk.ps1` pattern): options dialog with the origin captions ("installed by this setup"), the result dialog with the stage A lines and the reboot sentence -- screenshots. Then the same two reboots.
5. `NOTICE` wording; GitHub release; upstream report of the `FxDestroy` hang; downloads after `PrepareToInstall`; app fixes (`cleanup` traceback with usbip.exe absent, `unhide` wording, `doctor` "at login" shows the dev path); consider whether the HidHide MSI dropping `steamxbox` from `LowerFilters` deserves a note in the user guide.

---

## Phase C results, part 5 (installer-recovery agent, 2026-09-05 15:16 -> 15:50, after two more reboots) -- BUNDLED INSTALL, INTERACTIVE DIALOGS, FINAL STATE

Lock held, no PnP call issued by anything of ours, no reboot. Evidence `phaseC-20*`..`29*`.

| step | what | result | evidence |
|---|---|---|---|
| C-20 | clean check after reboots 1 (logon task: second PASS block in `usbip-removal.txt`) and 2 (tombstones) | all three services ABSENT, no devnode, no INFs/DriverStore/`.sys` for usbip2, hubs without `Filters`, class filters empty, only the disabled vJoy/Iriun nodes, 1 pad on. Leftover: `C:\Windows\System32\drivers\HidHide.sys` (HidHide's MSI leaves it). | `phaseC-20-clean-check.txt` |
| **C-21** | **bundled exe** `94511B0B...` on the clean machine (`step.ps1 -Setup ... -Components app,usbip,hidhide`; `-Setup` added to the harness) | **PASS, exit 0 in 46 s**: **0 `Downloading` lines**, `Extracting temporary file: ...USBip-0.9.7.7-x64.exe` and `...HidHide_1.5.230_x64.exe`, both `SHA-256 verified`, restore point #255, usbip installer exit 0, HidHide installer exit 0, `[reboot] HidHide`, verify all ok, **`THIRD-PARTY-NOTICES.txt` (4632 B) in `%LOCALAPPDATA%\ds5bridge`**. | `phaseC-21-install-bundled.log`, `-console.txt` |
| C-22 | verification | records 1/1, `UpperFilters=HidHide` x3 (`steamxbox` lower filter back), services RUNNING, driver store `oem80`/`oem186`/`oem303`, extension on both physical hubs, HidHide devnode OK, **22 input devices, 0 x Code 32**, helper hash == source, restore point #255. | `phaseC-22-post-bundled-install-verify.txt` |
| C-23 | **interactive uninstaller, options dialog, Cancel** (elevated `unins000.exe`, no switches) | dialog "Uninstall ds5bridge": *Also remove usbip-win2 (installed by ds5bridge setup)* ticked, *Also remove HidHide (installed by ds5bridge setup; asks for a reboot)* ticked, *Delete my settings too* unticked; Cancel -> `InitializeUninstall returned False; aborting.`, nothing removed. Note: the options dialog comes **before** Inno's own "Are you sure?" box. | `phaseC-23-ui-cancel-1-options.png`, `phaseC-23-ui-cancel.log`, `-console.txt` |
| C-24 | **interactive uninstaller with `/KEEPUSBIP=1 /KEEPHIDHIDE=1`** | boxes pre-unticked (origin captions unchanged); Uninstall -> Inno "Are you sure you want to completely remove ds5bridge and all of its components?" -> Yes -> **result dialog** "ds5bridge is uninstalled. All checks passed." with the `[info] usbip-win2 -- kept`, `[info] HidHide -- kept`, `[ok] app -- ... removed`, `[info] settings -- kept` lines -> Close -> Inno "removed" box. App removed, drivers untouched, records 1/1 survive. **Bug found and fixed**: the result dialog's memo swallowed Enter (multi-line `TNewMemo`), so Close needed a click/Alt+F4; now `WantReturns := False`, `Ok.Cancel := True`, focus on Close. | `phaseC-24-ui-keep-1-options.png`, `-2-confirm.png`, `-3-result.png`, `-6-removed.png` (misaligned, Inno stock box), `phaseC-24-ui-keep.log`, `-console.txt` |
| C-25 | **build 10** (helper: HidHide.sys tidy-up; `.iss`: result dialog fix) | download-mode **`C985159517050B2FA0B62166EF8C59E51545C6142FAD3E7B87198F0D01E4B60B`** = `dist\ds5bridge-setup-0.4.0.exe`; bundled **`EC186E8CDB1FB4F4C4FCD6B58FD1D27C07D758E7DB3C12CEA5ABC254BC25627F`** = `dist\bundle\ds5bridge-setup-0.4.0-bundled.exe` (not installed; same `.iss`/helper as build 10). | `phaseC-25-build-10.log`, `phaseC-25-build-10-bundled.log` |
| **C-26** | **interactive wizard, build 10, all-already-installed run** (walker `ui-walk2.ps1`: page detection by control text) | Components page: `ds5bridge 0.4.0 -- the app` greyed/ticked, `usbip-win2 0.9.7.7 -- already installed (will be verified, not reinstalled)`, `HidHide -- already installed (...)`; Tasks; Ready; Install (app refreshed, drivers verified only, 10 s); **"Installation check" page**: "All checks passed." + the 14 lines; Finished page; Finish launched the tray (the "Start ds5bridge now" default). Window caption is `Setup - ds5bridge 0.4.0`. | `phaseC-26-ui-wizard-1-components.png` ... `-4-installation-check.png`, `-5-finished.png`, `phaseC-26-ui-wizard.log`, `-console.txt` |
| C-27 | the tray Finish started (non-elevated) had bridged **a0fa9c0dd8bb** (the pad that is on now) on port 3242, `hide_bluetooth=true`, dev-list holding it -- **but the non-whitelisted open succeeded**: HidHide was installed at 15:20 and has not had its reboot, so it is not attached to the already-connected pad. Exactly what the `[reboot] HidHide` line warns about. `teardown -AppDir <installed app>`: clean. | `phaseC-27-teardown-after-wizard.txt` |
| C-28/29 | final verification + doctor | see "Machine now". | `phaseC-28-final-verify.txt`, `phaseC-29-final-doctor.txt` |

### Code changes in part 5

* `app/packaging/setup-helper.ps1`: `Remove-HidHideLeftoverSys` (INFO line; deletes `System32\drivers\HidHide.sys` only when no `Services\HidHide` key exists; called from `verify-removed` when HidHide is gone/expected gone and from usbip stage B after the reboot).
* `scripts/uninstall.ps1`: the same tidy-up before the summary.
* `app/packaging/ds5bridge.iss`: result dialog -- `Memo.WantReturns := False`, `Ok.Cancel := True`, `Form.ActiveControl := Ok`.
* `NOTICE`: usbip-win2 paragraph rewritten -- the standard installer/scripts download, a `-bundled.exe` build embeds both vendor installers unmodified, notices in `THIRD-PARTY-NOTICES.txt` next to the uninstaller.
* `docs/installer.md`: bundled-build paragraph (proven, log signature, the output-name caveat), HidHide.sys note, "not hidden until HidHide's reboot" note.
* Harness: `step.ps1 -Setup`, `unins-walk2.ps1`, `ui-walk2.ps1`, `shot-window.ps1` (DPI-aware). Lessons: from a sudo-elevated shell `FindWindow` does not see the elevated dialogs but `WScript.Shell.AppActivate` does; screenshots work from the non-elevated shell (`EnumWindows` + `SetProcessDPIAware`).

### Machine now (15:50) -- FULLY INSTALLED, HidHide reboot pending

* **ds5bridge 0.4.0** at `%LOCALAPPDATA%\ds5bridge\app` (build 10 files; `installer\setup-helper.ps1` == source; `unins000.exe`; no `THIRD-PARTY-NOTICES.txt` -- the C-24 uninstall removed the bundled install's directory and the download-mode wizard run does not ship the file, as designed), Start Menu shortcut, ARP "ds5bridge 0.4.0"; tray not running (stopped after the wizard). HKCU Run still the **dev** tray (`dist\...`) -- it autostarts at logon and bridges on its own; stop it before using the installed one.
* **usbip-win2 0.9.7.7** RUNNING (`ROOT\USB\0000` OK, extension on all three hubs); **HidHide 1.5.230** RUNNING, class filters registered, cloak on, dev-list empty, whitelist = HidHideCLI + the two installed exes; **records 1/1**. **HidHide needs its reboot** before hide-while-bridged works for the already-paired pads (C-27 shows it not hiding yet). `HidHide.sys` in `System32\drivers` is the live driver again.
* Pads: a0fa9c0dd8bb ON, present, OK (90 %). **d42f4ba1485d: connected over Bluetooth but its HID devnode is missing** (`doctor`: "HidHide is not hiding it -- the HID device itself is missing"); it went through hide/unhide cycles while HidHide was freshly installed without its reboot. A power-cycle of the pad (hold PS ~10 s, on again) or the pending reboot re-creates it. usbipd-win / ViGEm / vJoy untouched. Restore points #254, #255. No installer/PnP processes, no MSI InProgress. `scratchpad\machine-lock` deleted.
* Builds: `dist\ds5bridge-setup-0.4.0.exe` = build 10 `C9851595...`; `dist\bundle\ds5bridge-setup-0.4.0-bundled.exe` = `EC186E8C...`; `dist\bundle\ds5bridge-bundle-setup.exe` = the bundle-agent's, untouched.

### Still unproven

* Build 10 itself has only run as an app-refresh (C-26); its helper differs from build 9's by `Remove-HidHideLeftoverSys` only -- the deletion branch (no service key + file present) has never executed for real (the next full uninstall + two reboots would exercise it from stage B).
* The `.iss` result-dialog fix (Enter/Esc closes it) is compiled, not seen.
* `scripts/install.ps1` end to end (blocked on a GitHub release: `api.github.com/.../releases/latest` is 404); `scripts/uninstall.ps1` real run of the new two-half path (only `-DryRun` and the helper's identical logic were run).
* A driver install with a foreign usbip-win2 version present (the "replaces the installed x.y.z" caption and path); the `0.9.7.8` warning caption.
* System Protection OFF wording of the restore-point step (it was on; `[ok]` both times).
* Concurrency against a real second installer mid-transaction (only mocks, a bare msiexec, and the blocked devnode of Phase B).
* `usbip2_filter` unload on the physical hubs happens inside the vendor's `pnputil /delete-driver` after the reboot; it worked twice, but nothing of ours guards it beyond the timeout/no-kill rule.
* A machine where the logon task cannot run (no admin logon, task disabled by policy): the fallback is the message + `preflight` guard, untested.
