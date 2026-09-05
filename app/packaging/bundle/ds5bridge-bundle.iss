; ds5bridge "bundle" installer -- ONE exe, ONE Add/Remove Programs entry,
; ONE uninstall, for the app plus the two kernel-driver packages it stands on.
;
;   usbip-win2 0.9.7.7   the official Inno Setup installer, embedded byte-for-byte
;   HidHide    1.5.230   the official Advanced Installer exe (MSI inside), embedded
;
; Approach ("route a" in the feasibility study): the vendor installers are
; embedded unmodified and driven silently. Their signatures stay intact, their
; own uninstallers stay registered (so `winget`/Apps can still find them if the
; user keeps them), and NOTHING about how the drivers land on the machine is
; re-implemented here. What this script adds on top:
;
;   * components checkboxes to opt out of either driver at install time
;   * ADOPTION: a package that is already installed is left alone and marked
;     "not ours"; the uninstaller then defaults to keeping it
;   * one Add/Remove entry: the vendor entries of packages WE installed are
;     flagged SystemComponent=1 (hidden from Apps & Features, fully functional);
;     the flag is removed again if the user keeps a package at uninstall
;   * a keep/remove checkbox page in the uninstaller, and /KEEPUSBIP=1,
;     /KEEPHIDHIDE=1, /REMOVEUSBIP=1, /REMOVEHIDHIDE=1 for silent runs
;   * teardown before removal: quit the tray, `ds5bridge cleanup`,
;     `ds5bridge unhide`, HidHide cloak off -- so no pad is left hidden
;
; Build:   app\packaging\bundle\build-bundle.ps1   (fetches + SHA-256-verifies
;          the vendor installers into vendor\, then runs ISCC on this file)
;
; This is designed to slot into the conventional installer being written in
; app\packaging\: the [Files] `dontcopy` entries and Install* procedures are the
; embedded-file replacement for a "download at install time" step.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef DistDir
  #define DistDir "..\..\..\dist\ds5bridge"
#endif
#ifndef VendorDir
  #define VendorDir "vendor"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\..\dist\bundle"
#endif

; ---- pinned vendor facts (a release bump edits these and build-bundle.ps1) ----
#define UsbipVersion       "0.9.7.7"
#define UsbipSetup         "USBip-0.9.7.7-x64.exe"
; usbip-win2's own AppId (userspace/innosetup/setup.iss) + Inno's "_is1" suffix
#define UsbipUninstKey     "SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{199505b0-b93d-4521-a8c7-897818e0205a}_is1"
#define HidHideVersion     "1.5.230"
#define HidHideSetup       "HidHide_1.5.230_x64.exe"
; ProductCode of HidHide.msi 1.5.230 (read from its Property table)
#define HidHideProductCode "{01E0AB21-D1CC-42B4-9DFF-84FFE4F26DAF}"
#define HidHideUninstKey   "SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\" + HidHideProductCode
; written by the MSI's Registry table: Software\[Manufacturer]\[ProductName]
#define HidHideInfoKey     "SOFTWARE\Nefarius Software Solutions e.U.\HidHide"
#define HidHideCli         "{commonpf64}\Nefarius Software Solutions\HidHide\x64\HidHideCLI.exe"
; where this bundle records what it did (uninsdeletekey below)
#define BundleKey          "SOFTWARE\ds5bridge\Bundle"
; 1 = hide the vendor Add/Remove entries of packages we installed
#define HideVendorArp      1

[Setup]
AppId={{6F1C7D52-0B6E-4C0F-9B0B-6A4B9E1B7D51}
AppName=ds5bridge
AppVersion={#AppVersion}
AppVerName=ds5bridge {#AppVersion} (bundle)
AppPublisher=ds5bridge contributors
AppPublisherURL=https://github.com/Macle57/ds5-virtual-cable
AppSupportURL=https://github.com/Macle57/ds5-virtual-cable/issues
DefaultDirName={autopf}\ds5bridge
DefaultGroupName=ds5bridge
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Windows 10 1903 -- the floor usbip-win2 documents
MinVersion=10.0.18362
OutputDir={#OutputDir}
OutputBaseFilename=ds5bridge-bundle-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\ds5bridge-tray.exe
UninstallDisplayName=ds5bridge (includes usbip-win2 and HidHide drivers)
SetupLogging=yes
CloseApplications=no
RestartApplications=no
LicenseFile=..\..\..\LICENSE
InfoBeforeFile=THIRD-PARTY-NOTICES.txt
; the drivers decide; see NeedRestart / UninstallNeedRestart below
AlwaysRestart=no

[Types]
Name: "full";   Description: "Everything (recommended)"
Name: "custom"; Description: "Custom"; Flags: iscustom

[Components]
Name: "app";     Description: "ds5bridge (the app)"; Types: full custom; Flags: fixed
Name: "usbip";   Description: "usbip-win2 {#UsbipVersion} driver -- needed to present a wired pad (installing it restarts every USB 3 hub briefly)"; Types: full custom
Name: "hidhide"; Description: "HidHide {#HidHideVersion} driver -- optional: hide the Bluetooth pad while bridged (needs a reboot to activate)"; Types: full custom

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion; Components: app
Source: "THIRD-PARTY-NOTICES.txt"; DestDir: "{app}"; Components: app
; The vendor installers: carried inside setup.exe, extracted to {tmp} only when
; their component is selected, never copied into {app}. `nocompression`
; because both are already LZMA-packed.
Source: "{#VendorDir}\{#UsbipSetup}";   Flags: dontcopy nocompression
Source: "{#VendorDir}\{#HidHideSetup}"; Flags: dontcopy nocompression

[Dirs]
Name: "{app}\install-logs"

[Icons]
Name: "{group}\ds5bridge"; Filename: "{app}\ds5bridge-tray.exe"; WorkingDir: "{app}"
Name: "{group}\Uninstall ds5bridge"; Filename: "{uninstallexe}"

[Registry]
Root: HKLM; Subkey: "{#BundleKey}"; Flags: uninsdeletekey
Root: HKLM; Subkey: "{#BundleKey}"; ValueType: string; ValueName: "AppDir"; ValueData: "{app}"

[Run]
; De-elevated launch: `runasoriginaluser` is exactly the explorer.exe trick
; scripts\install.ps1 uses, done properly.
Filename: "{app}\ds5bridge-tray.exe"; Description: "Start ds5bridge now"; WorkingDir: "{app}"; Flags: postinstall nowait runasoriginaluser skipifsilent

[Code]
const
  UsbipUninstKey   = '{#UsbipUninstKey}';
  HidHideUninstKey = '{#HidHideUninstKey}';
  HidHideInfoKey   = '{#HidHideInfoKey}';
  HidHideSvcKey    = 'SYSTEM\CurrentControlSet\Services\HidHide';
  UsbipSvcKey      = 'SYSTEM\CurrentControlSet\Services\usbip2_ude';
  BundleKey        = '{#BundleKey}';

var
  NeedReboot: Boolean;
  // uninstall-side state
  RemoveUsbip, RemoveHidHide: Boolean;
  UsbipOurs, HidHideOurs: Boolean;
  CbUsbip, CbHidHide: TNewCheckBox;

// ---------------------------------------------------------------------------
// detection
// ---------------------------------------------------------------------------

function UsbipInstalledVersion(): String;
begin
  Result := '';
  if not RegQueryStringValue(HKLM, UsbipUninstKey, 'DisplayVersion', Result) then
    if RegKeyExists(HKLM, UsbipSvcKey) then
      Result := '?';   // driver service present but no Inno entry: a manual/partial install
end;

function UsbipUninstaller(): String;
var
  s: String;
begin
  Result := '';
  if RegQueryStringValue(HKLM, UsbipUninstKey, 'UninstallString', s) then
    Result := RemoveQuotes(s);
end;

function HidHideInstalledVersion(): String;
begin
  Result := '';
  if RegQueryStringValue(HKLM, HidHideInfoKey, 'Version', Result) then exit;
  if RegQueryStringValue(HKLM, HidHideUninstKey, 'DisplayVersion', Result) then exit;
  if RegKeyExists(HKLM, HidHideSvcKey) then
    Result := '?';
end;

function HidHideProductCodeInstalled(): String;
var
  names: TArrayOfString;
  i: Integer;
  key, name, pub: String;
begin
  // Prefer the pinned code; fall back to scanning for a newer HidHide (winget
  // may have upgraded it -- its ProductCode changes per version).
  Result := '';
  if RegKeyExists(HKLM, HidHideUninstKey) then
  begin
    Result := '{#HidHideProductCode}';
    exit;
  end;
  if RegGetSubkeyNames(HKLM, 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall', names) then
    for i := 0 to GetArrayLength(names) - 1 do
    begin
      key := 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\' + names[i];
      if RegQueryStringValue(HKLM, key, 'DisplayName', name) and (name = 'HidHide') then
        if RegQueryStringValue(HKLM, key, 'Publisher', pub) and (Pos('Nefarius', pub) > 0) then
        begin
          Result := names[i];
          exit;
        end;
    end;
end;

function LogDir(): String;
begin
  Result := ExpandConstant('{app}\install-logs');
  ForceDirectories(Result);
end;

function RunWait(const Exe, Params: String; var Code: Integer): Boolean;
begin
  Log(Format('exec: "%s" %s', [Exe, Params]));
  Result := Exec(Exe, Params, '', SW_HIDE, ewWaitUntilTerminated, Code);
  if Result then
    Log(Format('  -> exit %d', [Code]))
  else
    Log(Format('  -> could not start (%s)', [SysErrorMessage(Code)]));
end;

procedure SetArpHidden(const Key: String; Hidden: Boolean);
begin
  if not RegKeyExists(HKLM, Key) then exit;
  if Hidden then
    RegWriteDWordValue(HKLM, Key, 'SystemComponent', 1)
  else
    RegDeleteValue(HKLM, Key, 'SystemComponent');
end;

// Quit a running tray/bridge politely (WM_CLOSE via taskkill runs its
// teardown: detach the virtual pad, unhide the real one), then insist.
procedure StopDs5bridge();
var
  code: Integer;
begin
  RunWait(ExpandConstant('{sys}\taskkill.exe'), '/IM ds5bridge-tray.exe', code);
  RunWait(ExpandConstant('{sys}\taskkill.exe'), '/IM ds5bridge.exe', code);
  Sleep(4000);
  RunWait(ExpandConstant('{sys}\taskkill.exe'), '/F /IM ds5bridge-tray.exe', code);
  RunWait(ExpandConstant('{sys}\taskkill.exe'), '/F /IM ds5bridge.exe', code);
end;

// ---------------------------------------------------------------------------
// install
// ---------------------------------------------------------------------------

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  StopDs5bridge();
end;

procedure InstallUsbip();
var
  have, setup, params: String;
  code: Integer;
begin
  have := UsbipInstalledVersion();
  if have = '{#UsbipVersion}' then
  begin
    Log('usbip-win2 {#UsbipVersion} already installed -- adopting, not reinstalling');
    RegWriteDWordValue(HKLM, BundleKey, 'UsbipInstalledByUs', 0);
    RegWriteStringValue(HKLM, BundleKey, 'UsbipVersion', have);
    exit;
  end;
  if have <> '' then
    Log('usbip-win2 ' + have + ' found; the {#UsbipVersion} installer will replace it (it uninstalls the old package itself)');

  setup := ExpandConstant('{tmp}\{#UsbipSetup}');
  ExtractTemporaryFile('{#UsbipSetup}');
  // main+client are the driver, usbip.exe, devnode.exe, libusbip.dll; gui/sdk/pdb
  // are not needed by ds5bridge. vcredist stays on (usbip.exe needs it),
  // the desktop icon does not. /NOICONS: no "USBip" Start Menu folder.
  params := '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /RESTARTEXITCODE=3010 /NOCANCEL /NOICONS'
          + ' /COMPONENTS="main,client" /MERGETASKS="!desktopicon"'
          + ' /LOG="' + LogDir() + '\usbip-win2-setup.log"';
  if not RunWait(setup, params, code) then
    RaiseException('usbip-win2 installer could not be started: ' + SysErrorMessage(code));
  case code of
    0: ;
    3010: NeedReboot := True;
  else
    RaiseException(Format('usbip-win2 installer failed with exit code %d (see install-logs\usbip-win2-setup.log)', [code]));
  end;
  if UsbipInstalledVersion() <> '{#UsbipVersion}' then
    RaiseException('usbip-win2 installer reported success but version {#UsbipVersion} is not registered');
  RegWriteDWordValue(HKLM, BundleKey, 'UsbipInstalledByUs', 1);
  RegWriteStringValue(HKLM, BundleKey, 'UsbipVersion', '{#UsbipVersion}');
  if have <> '' then
    RegWriteStringValue(HKLM, BundleKey, 'UsbipReplacedVersion', have);
  #if HideVendorArp
  SetArpHidden(UsbipUninstKey, True);
  #endif
end;

procedure InstallHidHide();
var
  have, setup, params: String;
  code: Integer;
begin
  have := HidHideInstalledVersion();
  if have <> '' then
  begin
    // Any version: HidHide is shared with DS4Windows and friends and updates
    // through winget; never downgrade or replace somebody else's copy.
    Log('HidHide ' + have + ' already installed -- adopting, not reinstalling');
    RegWriteDWordValue(HKLM, BundleKey, 'HidHideInstalledByUs', 0);
    RegWriteStringValue(HKLM, BundleKey, 'HidHideVersion', have);
    exit;
  end;

  setup := ExpandConstant('{tmp}\{#HidHideSetup}');
  ExtractTemporaryFile('{#HidHideSetup}');
  // The switches winget's manifest for Nefarius.HidHide uses, plus an MSI log.
  params := '/exenoui /qn /norestart /L*v "' + LogDir() + '\hidhide-msi.log"';
  if not RunWait(setup, params, code) then
    RaiseException('HidHide installer could not be started: ' + SysErrorMessage(code));
  case code of
    0: ;
    1641, 3010: NeedReboot := True;
  else
    RaiseException(Format('HidHide installer failed with exit code %d (see install-logs\hidhide-msi.log)', [code]));
  end;
  if HidHideInstalledVersion() = '' then
    RaiseException('HidHide installer reported success but HidHide is not registered');
  // The filter only enters HID stacks built after it was registered: a
  // reboot is what makes existing pads hideable. Say so.
  NeedReboot := True;
  RegWriteDWordValue(HKLM, BundleKey, 'HidHideInstalledByUs', 1);
  RegWriteStringValue(HKLM, BundleKey, 'HidHideVersion', '{#HidHideVersion}');
  #if HideVendorArp
  SetArpHidden(HidHideUninstKey, True);
  #endif
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    if WizardIsComponentSelected('usbip') then
      InstallUsbip()
    else
      Log('usbip component deselected -- not installing usbip-win2');
    if WizardIsComponentSelected('hidhide') then
      InstallHidHide()
    else
      Log('hidhide component deselected -- not installing HidHide');
  end;
end;

function NeedRestart(): Boolean;
begin
  Result := NeedReboot;
end;

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo,
  MemoComponentsInfo, MemoGroupInfo, MemoTasksInfo: String): String;
var
  s, v: String;
begin
  s := '';
  if MemoDirInfo <> '' then s := s + MemoDirInfo + NewLine + NewLine;
  if MemoComponentsInfo <> '' then s := s + MemoComponentsInfo + NewLine + NewLine;
  s := s + 'Driver packages:' + NewLine;
  v := UsbipInstalledVersion();
  if not WizardIsComponentSelected('usbip') then
    s := s + Space + 'usbip-win2: skipped (deselected)'
  else if v = '{#UsbipVersion}' then
    s := s + Space + 'usbip-win2 ' + v + ': already installed -- will be kept as is'
  else if v <> '' then
    s := s + Space + 'usbip-win2 ' + v + ': will be replaced by {#UsbipVersion}'
  else
    s := s + Space + 'usbip-win2 {#UsbipVersion}: will be installed (USB 3 hubs restart briefly)';
  s := s + NewLine;
  v := HidHideInstalledVersion();
  if not WizardIsComponentSelected('hidhide') then
    s := s + Space + 'HidHide: skipped (deselected)'
  else if v <> '' then
    s := s + Space + 'HidHide ' + v + ': already installed -- will be kept as is'
  else
    s := s + Space + 'HidHide {#HidHideVersion}: will be installed (reboot needed to activate)';
  Result := s;
end;

// ---------------------------------------------------------------------------
// uninstall
// ---------------------------------------------------------------------------

function ParamFlag(const Name: String): Boolean;
begin
  Result := ExpandConstant('{param:' + Name + '|0}') = '1';
end;

function InitializeUninstall(): Boolean;
var
  d: Cardinal;
begin
  Result := True;
  UsbipOurs := RegQueryDWordValue(HKLM, BundleKey, 'UsbipInstalledByUs', d) and (d = 1);
  HidHideOurs := RegQueryDWordValue(HKLM, BundleKey, 'HidHideInstalledByUs', d) and (d = 1);
  // Defaults: remove what this setup installed, keep what it merely found.
  RemoveUsbip := UsbipOurs and (UsbipInstalledVersion() <> '');
  RemoveHidHide := HidHideOurs and (HidHideInstalledVersion() <> '');
  // Silent overrides: /KEEP*=1 wins over /REMOVE*=1.
  if ParamFlag('REMOVEUSBIP') then RemoveUsbip := UsbipInstalledVersion() <> '';
  if ParamFlag('REMOVEHIDHIDE') then RemoveHidHide := HidHideInstalledVersion() <> '';
  if ParamFlag('KEEPUSBIP') then RemoveUsbip := False;
  if ParamFlag('KEEPHIDHIDE') then RemoveHidHide := False;
  Log(Format('uninstall plan: usbip ours=%d remove=%d; hidhide ours=%d remove=%d', [
    Integer(UsbipOurs), Integer(RemoveUsbip), Integer(HidHideOurs), Integer(RemoveHidHide)]));
end;

function Origin(Ours: Boolean): String;
begin
  if Ours then
    Result := 'installed by the ds5bridge setup'
  else
    Result := 'was already on this PC before ds5bridge (kept unless you tick it)';
end;

// A checkbox page inside the uninstaller: shown modally before anything is
// removed. (The uninstaller has no wizard, so the page is grafted onto the
// progress form's notebook -- the standard Inno idiom for this.)
procedure InitializeUninstallProgressForm();
var
  page: TNewNotebookPage;
  prev: TNewNotebookPage;
  prevName, prevDesc: String;
  prevCancel: Boolean;
  lbl: TNewStaticText;
  btn: TNewButton;
  v: String;
begin
  if UninstallSilent then exit;

  page := TNewNotebookPage.Create(UninstallProgressForm);
  page.Notebook := UninstallProgressForm.InnerNotebook;
  page.Parent := UninstallProgressForm.InnerNotebook;
  page.Align := alClient;

  lbl := TNewStaticText.Create(page);
  lbl.Parent := page;
  lbl.Left := ScaleX(0); lbl.Top := ScaleY(0); lbl.Width := page.Width; lbl.WordWrap := True;
  lbl.AutoSize := True;
  lbl.Caption := 'ds5bridge itself (the app, its shortcut and this entry) will be removed.' + #13#10
    + 'The two driver packages are separate products that other software may also use.' + #13#10
    + 'Tick the ones you want removed as well; untick to keep them installed.';

  CbUsbip := TNewCheckBox.Create(page);
  CbUsbip.Parent := page;
  CbUsbip.Left := ScaleX(0); CbUsbip.Top := lbl.Top + lbl.Height + ScaleY(16);
  CbUsbip.Width := page.Width; CbUsbip.Height := ScaleY(34);
  v := UsbipInstalledVersion();
  CbUsbip.Enabled := v <> '';
  if v <> '' then
    CbUsbip.Caption := 'Remove usbip-win2 ' + v + '  (' + Origin(UsbipOurs) + ')'
  else
    CbUsbip.Caption := 'usbip-win2: not installed';
  CbUsbip.Checked := RemoveUsbip;

  CbHidHide := TNewCheckBox.Create(page);
  CbHidHide.Parent := page;
  CbHidHide.Left := ScaleX(0); CbHidHide.Top := CbUsbip.Top + CbUsbip.Height + ScaleY(8);
  CbHidHide.Width := page.Width; CbHidHide.Height := ScaleY(34);
  v := HidHideInstalledVersion();
  CbHidHide.Enabled := v <> '';
  if v <> '' then
    CbHidHide.Caption := 'Remove HidHide ' + v + '  (' + Origin(HidHideOurs) + '; also used by DS4Windows etc.; asks for a reboot)'
  else
    CbHidHide.Caption := 'HidHide: not installed';
  CbHidHide.Checked := RemoveHidHide;

  lbl := TNewStaticText.Create(page);
  lbl.Parent := page;
  lbl.Left := ScaleX(0); lbl.Top := CbHidHide.Top + CbHidHide.Height + ScaleY(16);
  lbl.Width := page.Width; lbl.WordWrap := True; lbl.AutoSize := True;
  lbl.Caption := 'Before anything is removed, ds5bridge detaches its virtual pad and un-hides your Bluetooth pad. Removing usbip-win2 restarts every USB 3 hub briefly.';

  prev := UninstallProgressForm.InnerNotebook.ActivePage;
  prevName := UninstallProgressForm.PageNameLabel.Caption;
  prevDesc := UninstallProgressForm.PageDescriptionLabel.Caption;
  prevCancel := UninstallProgressForm.CancelButton.Enabled;

  UninstallProgressForm.InnerNotebook.ActivePage := page;
  UninstallProgressForm.PageNameLabel.Caption := 'What to remove';
  UninstallProgressForm.PageDescriptionLabel.Caption := 'ds5bridge installed two driver packages alongside itself';

  btn := TNewButton.Create(UninstallProgressForm);
  btn.Parent := UninstallProgressForm;
  btn.Width := UninstallProgressForm.CancelButton.Width;
  btn.Height := UninstallProgressForm.CancelButton.Height;
  btn.Top := UninstallProgressForm.CancelButton.Top;
  btn.Left := UninstallProgressForm.CancelButton.Left - btn.Width - ScaleX(10);
  btn.Anchors := UninstallProgressForm.CancelButton.Anchors;
  btn.Caption := '&Uninstall';
  btn.ModalResult := mrOk;
  btn.Default := True;
  UninstallProgressForm.CancelButton.Enabled := True;
  UninstallProgressForm.CancelButton.ModalResult := mrCancel;

  if UninstallProgressForm.ShowModal = mrCancel then Abort;

  RemoveUsbip := CbUsbip.Enabled and CbUsbip.Checked;
  RemoveHidHide := CbHidHide.Enabled and CbHidHide.Checked;
  Log(Format('uninstall page: remove usbip=%d hidhide=%d', [Integer(RemoveUsbip), Integer(RemoveHidHide)]));

  btn.Visible := False;
  UninstallProgressForm.CancelButton.Enabled := prevCancel;
  UninstallProgressForm.InnerNotebook.ActivePage := prev;
  UninstallProgressForm.PageNameLabel.Caption := prevName;
  UninstallProgressForm.PageDescriptionLabel.Caption := prevDesc;
end;

procedure UninstallHidHide();
var
  pc, cli, logf: String;
  code: Integer;
begin
  pc := HidHideProductCodeInstalled();
  if pc = '' then
  begin
    Log('HidHide: no MSI product registered; nothing to remove');
    exit;
  end;
  // The driver may stay attached to already-built HID stacks until reboot,
  // and it would keep enforcing the last blacklist it loaded. Switch the
  // cloak off first so a pad can never be left invisible by an uninstall.
  cli := ExpandConstant('{#HidHideCli}');
  if FileExists(cli) then
    RunWait(cli, '--cloak-off', code);
  logf := ExpandConstant('{%TEMP}\ds5bridge-uninstall-hidhide.log');
  if not RunWait(ExpandConstant('{sys}\msiexec.exe'), '/x ' + pc + ' /qn /norestart /L*v "' + logf + '"', code) then
    exit;
  case code of
    0, 1605: ;
    1641, 3010: NeedReboot := True;
  else
    Log(Format('HidHide uninstall returned %d (log: %s)', [code, logf]));
  end;
end;

procedure UninstallUsbip();
var
  unins: String;
  code: Integer;
begin
  unins := UsbipUninstaller();
  if (unins = '') or not FileExists(unins) then
  begin
    Log('usbip-win2: no uninstaller registered; nothing to remove');
    exit;
  end;
  RunWait(unins, '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /RESTARTEXITCODE=3010 /LOG="'
    + ExpandConstant('{%TEMP}\ds5bridge-uninstall-usbip.log') + '"', code);
  if code = 3010 then NeedReboot := True;
  // usbip-win2's uninstaller always asks for a restart (UninstallNeedRestart)
  NeedReboot := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  exe: String;
  code: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    // 1. put the machine back while our exe still exists
    StopDs5bridge();
    exe := ExpandConstant('{app}\ds5bridge.exe');
    if FileExists(exe) then
    begin
      RunWait(exe, 'cleanup', code);
      RunWait(exe, 'unhide', code);
    end;
    // 2. the drivers, per the user's choice
    if RemoveHidHide then
      UninstallHidHide()
    else
      SetArpHidden(HidHideUninstKey, False);   // kept: make it visible in Apps again
    if RemoveUsbip then
      UninstallUsbip()
    else
      SetArpHidden(UsbipUninstKey, False);
  end;
end;

function UninstallNeedRestart(): Boolean;
begin
  Result := NeedReboot;
end;
