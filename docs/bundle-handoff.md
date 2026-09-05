# Bundling usbip-win2 + HidHide into one ds5bridge installer — handoff

> **Retired (2026-09-05).** This was a parallel attempt; the shipping
> installer is `app/packaging/ds5bridge.iss`, and its bundled build is
> `build-installer.ps1 -Bundle` (see `docs/installer.md`). What survives from
> here: §1's licence findings, `THIRD-PARTY-NOTICES.txt`, and the sequencing
> rules of §4. `ds5bridge-bundle.iss` and `build-bundle.ps1` are reference
> only and are not built or tested any more.

*bundle-agent, 2026-09-04. Session ended by a machine reboot mid-test; this is
the state of the work, the evidence, and what to do next.*

## 1. Feasibility verdict

| package | legal | technical | verdict |
|---|---|---|---|
| **usbip-win2 0.9.7.7** | **Yes.** `LICENSE.txt` at tag `v.0.9.7.7` is BSD-2-Clause, © 2021-2026 Vadym Hrynchyshyn; binary redistribution is permitted if the notice is reproduced "in the documentation and/or other materials provided with the distribution". The installer/tools are Authenticode-signed by Cloudyne Systems (OSSign) and the drivers are WHLK-signed by Microsoft; nothing is modified or re-signed. No stated anti-bundling policy anywhere in the repo, README or release notes. | Embedding the official `USBip-0.9.7.7-x64.exe` (Inno Setup 6.7) and running it `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /NOICONS /COMPONENTS="main,client" /MERGETASKS="!desktopicon"` works. Its own `setup.iss` (`userspace/innosetup/setup.iss`) does: vc_redist, `pnputil /add-driver usbip2_filter.inf /install`, `devnode.exe install usbip2_ude.inf ROOT\USBIP_WIN2\UDE`; uninstall: `devnode.exe remove ROOT\USBIP_WIN2\UDE root` then `pnputil /delete-driver oemNN.inf /uninstall` for both INFs; it auto-uninstalls a previous version (UninsIS.dll) and always flags a reboot. | **Bundle it (route a).** |
| **HidHide 1.5.230** | **Yes.** MIT (repo LICENSE: © 2020 Eric Korff de Gidts, © 2021-2024 Benjamin Höglinger-Stelzer; the package's own `LICENSE.rtf` carries only the 2020 line). README "Package integration" section explicitly anticipates third-party deployment ("Installation packages and third-party applications can rely on…", "Third-party software deployment may benefit from the HidHide CLI"). Nefarius's only relevant public statement is on the ViGEmBus install page: programs that "bundle" the driver are *unsupported by them* ("This method is out of our reach; please contact the distributor of said software") — a support-scope statement, not a prohibition. Installer signed by Nefarius Software Solutions e.U.; driver attestation-signed by Microsoft. | Embedding `HidHide_1.5.230_x64.exe` (Advanced Installer bootstrapper around `HidHide.msi`, ProductCode `{01E0AB21-D1CC-42B4-9DFF-84FFE4F26DAF}`, UpgradeCode `{8822CC70-E2A5-4CB7-8F14-E27101150A1D}`) with winget's own switches `/exenoui /qn /norestart` works. The MSI does real work a raw-INF install would have to replicate: `nefconw --install-driver`, `--create-device-node root\HidHide`, `--add-class-filter` ×3 (HIDClass, XnaComposite, XboxComposite), Watchdog service, `nefarius_HidHide_Updater --install` (scheduled task), ETW manifest, `REBOOT=Force`. Uninstall: `msiexec /x {ProductCode} /qn /norestart` (MSI cached at `C:\Windows\Installer\`). | **Bundle it (route a); keep it optional; mention support scope in docs.** |

Pinned hashes re-verified against what is online on 2026-09-04 (both match; both Authenticode `Valid`):
`USBip-0.9.7.7-x64.exe` = `51620FA5F9F8BE5932BC9D786DEEE557CE06D5407A99CAB490DCFAC71F185FEA`, `HidHide_1.5.230_x64.exe` = `F4BBBCB82E6258641B887C74BC81C4C5F66E4AA811808DFC304347687B7605F6` (= winget manifest `Nefarius.HidHide 1.5.230`). usbip-win2 0.9.7.8 release notes still carry the maintainer's memory-corruption warning; 0.9.7.7 remains correct. HidHide has no newer published installer than 1.5.230 (newer git tags only).

Routes rejected: **(b) raw driver packages via pnputil** — legal but we would own the devnode creation, class-filter registration, watchdog/updater omission and the vendors' uninstall logic, and lose the vendors' own upgrade paths; **(c) winget dependencies / MSIX** — winget cannot pin usbip-win2 to 0.9.7.7 (only `latest`, which is the bad 0.9.7.8) and MSIX cannot carry kernel drivers.

## 2. What was built (all under `app/packaging/bundle/`)

| file | purpose |
|---|---|
| `ds5bridge-bundle.iss` | Inno Setup script. Embeds both vendor installers as `dontcopy` files, runs them silently from `[Code]` (`InstallUsbip`, `InstallHidHide`), components checkboxes (`app` fixed, `usbip`, `hidhide`), **adoption** of pre-existing installs (recorded in `HKLM\SOFTWARE\ds5bridge\Bundle`: `UsbipInstalledByUs`, `HidHideInstalledByUs`), hides the vendor Add/Remove entries it installed (`SystemComponent=1`, un-hidden again if kept), uninstaller checkbox page (grafted onto `UninstallProgressForm`) plus `/KEEPUSBIP=1 /KEEPHIDHIDE=1 /REMOVEUSBIP=1 /REMOVEHIDHIDE=1` for silent runs, teardown before removal (`taskkill`, `ds5bridge cleanup`, `ds5bridge unhide`, `HidHideCLI --cloak-off`). |
| `build-bundle.ps1` | Downloads the two installers into `vendor/` (git-ignored), refuses to build on SHA-256 or Authenticode mismatch, runs ISCC. `-SkipApp` reuses `dist\ds5bridge`. |
| `THIRD-PARTY-NOTICES.txt` | Shipped into `{app}`; reproduces both licence texts, hashes and signers (the BSD-2 "materials provided with the distribution" obligation). Also shown as the InfoBefore page. |
| `.gitignore` | `app/packaging/bundle/vendor/` added. |

Build: `powershell -File app\packaging\bundle\build-bundle.ps1 [-SkipApp]` → `dist\bundle\ds5bridge-bundle-setup.exe` (78.1 MB; Inno Setup 6 per-user install at `%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe`).

Silent install: `ds5bridge-bundle-setup.exe /SILENT /NORESTART /RESTARTEXITCODE=3010 /LOG=<file>` (`/COMPONENTS="app,usbip"` to skip HidHide).
Silent uninstall: `"C:\Program Files\ds5bridge\unins000.exe" /SILENT /NORESTART /RESTARTEXITCODE=3010 [/KEEPUSBIP=1] [/KEEPHIDHIDE=1] [/REMOVEUSBIP=1] [/REMOVEHIDHIDE=1] /LOG=<file>`. Default: remove what the bundle installed, keep what it adopted.

Known cosmetic gap: app dir is `{autopf}\ds5bridge` and no HKCU autostart — the sibling's `app/packaging/ds5bridge.iss` owns those decisions (`{localappdata}\ds5bridge\app`, `autostart` task).

## 3. Verified results (evidence in `scratchpad/bundle-agent/`)

- **Baseline `S0.txt`** (21:28): usbip 0.9.7.7 + HidHide 1.5.230 present, cloak on, no hides, nothing attached, both pads visible to a non-whitelisted process.
- **Install #1, adoption path** (`ds5b-install-1.log`): exit **0**, 8 s. Log: `usbip-win2 0.9.7.7 already installed -- adopting, not reinstalling`, `HidHide 1.5.230 already installed -- adopting, not reinstalling`, `Need to restart Windows? No`.
- **`S1.txt`** after it: one new ARP entry `ds5bridge (includes usbip-win2 and HidHide drivers)`; vendor entries untouched (adopted ⇒ not hidden); `HKLM\SOFTWARE\ds5bridge\Bundle` = `UsbipInstalledByUs=0, HidHideInstalledByUs=0`; `C:\Program Files\ds5bridge\ds5bridge.exe doctor` → usbip 0.9.7.7 ok, HidHide 1.5.230.0 ok, both controllers seen, "nothing attached" (only warn: the new exe is not on the HidHide whitelist yet — expected until first bridge).
- **Uninstall #1, `/REMOVEUSBIP=1 /REMOVEHIDHIDE=1`** (`ds5b-uninstall-1-full.log`): teardown ran (`cleanup` 0, `unhide` 0, `--cloak-off` 0); HidHide `msiexec /x` exit **0** in 4 s (`ds5bridge-uninstall-hidhide.log`; setupapi: "Reboot required to stop service 'HidHide'" — driver stays loaded until reboot, expected); then `USBip unins000.exe /VERYSILENT` **hung** in its first step, see §4. `stuck.png` is the screen at that moment.
- **Not reached** (reboot intervened): clean-machine install (`InstalledByUs=1`, hidden vendor ARP entries), uninstall-with-keep, interactive screenshots of the components page and the uninstaller checkbox page. The first-run compile error (a `[`-at-line-start in `[Code]`) and the version-regex bug (`"5bridge 0.4.0"` in ARP) are fixed in the tree; the rebuilt exe in `dist\bundle\` has the fix.

## 4. The hang, and how to sequence usbip removal safely

`ds5bridge-uninstall-usbip.log` ends at `Running Exec: devnode.exe remove ROOT\USBIP_WIN2\UDE root` (21:33:38.449); `C:\Windows\INF\setupapi.dev.log` has an unclosed `[Uninstall device subtree (DiUninstallDevice) - ROOT\USB\0000]` section from 21:33:38.520. State: `usbip2_filter` Stopped (emulated root hub already removed), `usbip2_ude` Running, `ROOT\USB\0000` still present. No message box (only my `/SILENT` progress form and the sibling's wizard were on screen). Nothing was attached and the dev tray had been stopped cleanly ten minutes earlier.

Contributing factor: the sibling installer (started 21:33:49, ignoring the machine lock) ran a HidHide MSI install (`nefconw --install-driver` / `--create-device-node`, 21:34:16-18) concurrently, contending for the PnP device-install serialization while `DiUninstallDevice` needed the tree lock; a further `pnputil.exe` queued at 21:43:28. But the controller removal also had ~40 s uncontended and did not finish, so UDE controller teardown on this build may itself be reboot-bound (the vendor sets `UninstallNeedRestart := true` and its 0.9.7.3 notes say a reboot is required after removal).

**Proposed sequencing for any uninstaller that removes usbip-win2:**
1. Never run it while another driver install is in flight: take a global mutex (`Global\ds5bridge-driver-ops`) around every vendor install/uninstall in both installers, and check `HKLM\...\Installer\InProgress` / a running `msiexec`/`nefconw`/`pnputil` before starting.
2. Order: stop tray → `ds5bridge cleanup` → `usbip.exe detach -all` → verify `usbip port` empty and no `USB\VID_054C&PID_0CE6` present → HidHide removal last (it needs a reboot anyway and restarts all HID devices) → **usbip last of all**.
3. Wrap the usbip uninstaller with a timeout (e.g. 180 s). On timeout do **not** kill `devnode.exe`; instead tell the user a reboot is required to finish removing the emulated controller, set `NeedRestart`, and run the vendor's documented fallback after reboot (`pnputil /remove-device ROOT\USB\0000 /subtree`, then `pnputil /delete-driver oemNN.inf /uninstall` for `usbip2_ude`/`usbip2_filter`, then `rd /S /Q "C:\Program Files\USBip"`).
4. Alternatively skip `devnode.exe remove` entirely and do the removal ourselves with `pnputil /remove-device ROOT\USB\0000 /subtree` (documented Win11 21H2+ alternative in the vendor README), which reports "reboot required" instead of blocking.

## 5. Post-reboot recovery (machine state was mid-uninstall)

Expected after reboot: HidHide driver gone (package unstaged, service deleted, class filters removed by the MSI) but possibly re-installed by the sibling's run; usbip: emulated root hub removed, controller `ROOT\USB\0000` and both driver packages possibly still present, `C:\Program Files\USBip` still present with its ARP entry, `C:\Program Files\ds5bridge` still present with its ARP entry (my uninstaller never reached file deletion).

1. Inventory first (read-only): `Get-PnpDevice | ? InstanceId -match 'ROOT\\USB\\0000|ROOT\\SYSTEM\\0007'`, `sc query usbip2_ude`, `sc query usbip2_filter`, `sc query HidHide`, `pnputil /enum-drivers | findstr /i "usbip2 hidhide"`, and the ARP query in §3 to see what survived.
2. If `ROOT\USB\0000` is still present and `unins000.exe` for USBip still exists: `sudo "C:\Program Files\USBip\unins000.exe" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART` **alone**, nothing else running; if it hangs again, `sudo pnputil /remove-device ROOT\USB\0000 /subtree`, then `pnputil /delete-driver <oem>.inf /uninstall` for both usbip INFs (find them with `pnputil /enum-drivers`).
3. If `C:\Program Files\ds5bridge\unins000.exe` exists: `sudo "C:\Program Files\ds5bridge\unins000.exe" /SILENT /NORESTART /KEEPUSBIP=1 /KEEPHIDHIDE=1` removes just the bundle's app + ARP entry (un-hides nothing, since it adopted).
4. Reinstall the drivers the way the machine had them: `USBip-0.9.7.7-x64.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART` (copies in `app\packaging\bundle\vendor\`, hashes verified), then `HidHide_1.5.230_x64.exe /exenoui /qn /norestart`, one at a time, then reboot once more for HidHide's filter to attach to existing pads.
5. Restart the dev tray: `sudo powershell -File .\app\tools\dev_tray.ps1`; power-cycle both pads; confirm `http://127.0.0.1:8765/api/state` shows both.
6. The HidHide whitelist lost nothing (registry `Parameters` key was preserved with `FLG_ADDREG_NOCLOBBER`), but re-check `HidHideCLI --app-list` after reinstall.

## 6. Folding into the sibling's installer

The sibling's `app/packaging/ds5bridge.iss` already has `#ifdef BundleUsbip` / `#ifdef BundleHidHide` `dontcopy` hooks and a `GetInstallerFile()` indirection. To bundle: run `build-bundle.ps1`'s fetch+verify step (or lift `$Pins` from it), then `ISCC /DBundleUsbip=app\packaging\bundle\vendor\USBip-0.9.7.7-x64.exe /DBundleHidHide=...\HidHide_1.5.230_x64.exe ...`. Port from this prototype: adoption flags in `HKLM\SOFTWARE\ds5bridge\Bundle` (their uninstaller defaults to "everything goes"; adopted packages should default to *keep*), the `SystemComponent` hide/un-hide for a single ARP entry, the uninstaller checkbox page, `THIRD-PARTY-NOTICES.txt` into `{app}` plus a `NOTICE` update (usbip-win2's paragraph currently says "No usbip-win2 code, header or binary is redistributed" — that becomes false the moment the bundle ships), and §4's sequencing/timeout rules. Docs: add to `docs/provenance.md` §1 that the two installers are redistributed unmodified with their hashes, and to USER-GUIDE that HidHide support for bundled copies goes to us, not Nefarius.
