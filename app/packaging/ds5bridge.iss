; ds5bridge-setup.exe -- Inno Setup script. Build with app\packaging\build-installer.ps1.
;
; What this installer is, in one paragraph: a wizard around the same four
; steps scripts\install.ps1 performs -- restore point, usbip-win2 0.9.7.7,
; HidHide, the app -- with a checkbox for each, the two driver installers
; DOWNLOADED at install time from their pinned URLs and verified by SHA-256
; -- or, in the "-bundled" build, carried inside the exe unmodified, with the
; vendors' notices (THIRD-PARTY-NOTICES.txt; see NOTICE) -- a verification
; page at the end, an entry in Settings > Apps, and an uninstaller that takes
; the drivers it installed back out (ones it found already there are kept
; by default) unless told otherwise.
;
; Two rules learned the hard way on 2026-09-04, enforced by the helper:
; never touch a driver while another installer (msiexec, an Inno setup,
; devnode, nefconw, pnputil) is at work -- two at once wedged Plug and Play
; -- and never leave HidHide registered as a class filter without its
; service, which left the machine with no keyboard, mouse or pad.
;
; And one learned on 2026-09-06 on a second machine: a refusal that says
; "reboot and run this again" is not enough. usbip-win2's removal needs TWO
; reboots (docs/installer.md), and between them this setup refused with a
; message the person had already obeyed. So the preflight now finishes a
; half-done removal itself where it can, waits for a busy installer instead
; of failing on sight, and when a reboot really must come first it says so
; with a "restart now" prompt and relaunches itself after the restart
; (RegisterResume, a RunOnce value pointing at a copy of this exe).
;
; Division of labour: this file is the user interface and the download step.
; Everything that looks at or changes the machine -- services, devnodes,
; HidHide's lists, the two vendors' uninstallers -- is in setup-helper.ps1,
; which is embedded here, run with powershell.exe, and reports one
; "KIND|label|detail" line per fact. See that file's header.
;
; Bundling instead of downloading: pass /DBundleUsbip=<path to
; USBip-0.9.7.7-x64.exe> and/or /DBundleHidHide=<path to HidHide_1.5.230_x64.exe>
; to ISCC. The files are then carried inside the setup exe and the download
; step is skipped for them; the SHA-256 is checked either way. Nothing else
; changes, which is the point of the GetInstallerFile() indirection below.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef AppBuildDir
  #define AppBuildDir "..\..\dist\ds5bridge"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist"
#endif

; -- pinned facts: keep identical to scripts\install.ps1 and setup-helper.ps1 --
#define UsbipVersion   "0.9.7.7"
#define UsbipFile      "USBip-0.9.7.7-x64.exe"
#define UsbipUrl       "https://github.com/vadimgrn/usbip-win2/releases/download/v.0.9.7.7/USBip-0.9.7.7-x64.exe"
#define UsbipSha256    "51620fa5f9f8be5932bc9d786deee557ce06d5407a99cab490dcfac71f185fea"
#define HidHideVersion "1.5.230"
#define HidHideFile    "HidHide_1.5.230_x64.exe"
#define HidHideUrl     "https://github.com/nefarius/HidHide/releases/download/v1.5.230.0/HidHide_1.5.230_x64.exe"
#define HidHideSha256  "f4bbbcb82e6258641b887c74bc81c4c5f66e4aa811808dfc304347687b7605f6"
#define Helper         "setup-helper.ps1"
#define RepoUrl        "https://github.com/Macle57/ds5-virtual-cable"

; A bundled build gets its own file name, so the two flavours can sit side by
; side in dist and on a release page (build-installer.ps1 relies on this).
#if Defined(BundleUsbip) || Defined(BundleHidHide)
  #define OutputName   "ds5bridge-setup-" + AppVersion + "-bundled"
#else
  #define OutputName   "ds5bridge-setup-" + AppVersion
#endif
; How each driver package arrives, for the component captions. The sizes are
; the vendors' installers as published (33 MB and 8 MB).
#ifdef BundleUsbip
  #define UsbipHow     "included in this setup"
#else
  #define UsbipHow     "downloaded, ~32 MB"
#endif
#ifdef BundleHidHide
  #define HidHideHow   "included in this setup"
#else
  #define HidHideHow   "downloaded, ~8 MB"
#endif

[Setup]
AppId={{7B6E2D6A-3C39-4E0B-9C7E-2F8B1C2D5A61}
AppName=ds5bridge
AppVersion={#AppVersion}
AppVerName=ds5bridge {#AppVersion}
AppPublisher=ds5bridge contributors
AppPublisherURL={#RepoUrl}
AppSupportURL={#RepoUrl}/issues
AppUpdatesURL={#RepoUrl}/releases
; %LOCALAPPDATA%\ds5bridge\app is the layout the in-app auto-updater owns
; (app/ds5app/update.py: it swaps <root>\app as a whole). The path is fixed,
; so there is no directory page. It is a per-user location written by an
; elevated process, which Inno warns about; that is deliberate and the same
; choice install.ps1 makes -- the app must be able to update itself without
; administrator rights. If a standard user elevates with a different
; administrator account, the app lands in that administrator's profile.
DefaultDirName={localappdata}\ds5bridge
DisableDirPage=yes
UsedUserAreasWarning=no
DisableProgramGroupPage=yes
; The two drivers need admin. Since 0.5.0 so does the tray (ds5bridge.spec:
; uac_admin, for the devnode restarts that make hiding and unhiding true), so
; this elevated process may launch it directly -- see [Run].
PrivilegesRequired=admin
; Windows 10 1903 (build 18362) x64 is the floor usbip-win2 documents.
MinVersion=10.0.18362
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename={#OutputName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=ds5bridge {#AppVersion}
UninstallDisplayIcon={app}\app\ds5bridge-tray.exe
; Always keep a log in %TEMP% (Setup Log *.txt); /LOG="file" names it.
SetupLogging=yes
; We stop the app ourselves (PrepareToInstall), with the tray's own teardown.
CloseApplications=no
; Never reboot on our own after an INSTALL. A fresh HidHide is attached to the
; connected controllers by restarting their devnodes (helper: hidhide-attach),
; so a reboot is only ever recommended, and the summary page says when. The
; UNINSTALLER is different: removing a driver does need one, and
; UninstallNeedRestart() below makes Inno ask (interactive runs only).
AlwaysRestart=no
RestartIfNeededByRun=no
ShowComponentSizes=no
AllowNoIcons=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
; Shown under PrepareToInstall's own text when it sets NeedsRestart (the
; preflight's exit 4), with Inno's yes/no restart radios. Inno never re-runs
; Setup on its own; RegisterResume below does.
PrepareToInstallNeedsRestart=After the restart, Setup starts again on its own to finish installing [name]; approve its User Account Control prompt when it does.%n%nWould you like to restart now?

[Types]
Name: "full"; Description: "Everything (recommended)"
Name: "custom"; Description: "Custom"; Flags: iscustom

[Components]
; "fixed": the installer of ds5bridge always installs ds5bridge. Without it,
; a mistyped /COMPONENTS selected nothing, Setup wrote that empty selection
; into the registry as the "previous" one, and the next run without a switch
; reused it and installed nothing (2026-09-04, run phaseA-09).
Name: "app"; Description: "ds5bridge {#AppVersion} -- the app (tray icon and command line)"; Types: full custom; Flags: fixed
Name: "usbip"; Description: "usbip-win2 {#UsbipVersion} -- the virtual-USB driver the bridge needs ({#UsbipHow})"; Types: full custom
Name: "hidhide"; Description: "HidHide {#HidHideVersion} -- optional: hides the Bluetooth pad while it is bridged ({#HidHideHow})"; Types: full custom

[Tasks]
Name: "restorepoint"; Description: "Create a System Restore point before installing the driver (recommended)"; Components: usbip
; Start with Windows: two exclusive ways (radio buttons under one box). The
; service is the default -- it starts at boot, before anyone signs in, like
; Chrome Remote Desktop's host -- and the logon task is what 0.5 offered.
; Choosing one removes the other (helper: service-install / autostart-enable).
; Unticking the box removes both. The selection is remembered by Inno for the
; next run, so the in-app updater's silent run keeps whichever was chosen; a
; 0.5 install (task, no service) remembered plain 'autostart', which now means
; the service -- the recommended mode. Nothing here reads the machine's
; current task/service to pre-select a radio: that would override /TASKS and
; /MERGETASKS (CurPageChanged runs in silent mode too), and did, once.
Name: "autostart"; Description: "Start ds5bridge with Windows"; Components: app
Name: "autostart\service"; Description: "when Windows starts, before anyone signs in -- as a Windows service (recommended)"; Components: app; Flags: exclusive
Name: "autostart\task"; Description: "when you sign in -- a scheduled task with administrator rights, as before"; Components: app; Flags: exclusive unchecked
; Shortcuts that open the dashboard (http://127.0.0.1:<dashboard_port>/) in
; the default browser; the port is read from config.json (DashboardUrl).
Name: "dashstartmenu"; Description: "Start menu entry"; GroupDescription: "Open the dashboard from:"; Components: app
Name: "dashdesktop"; Description: "Desktop shortcut"; GroupDescription: "Open the dashboard from:"; Components: app

[Files]
; Our own code is embedded: it is ours to redistribute, it is the thing being
; installed, and an installer that downloads the program it installs would
; be an installer for a download link.
Source: "{#AppBuildDir}\*"; DestDir: "{app}\app"; Flags: recursesubdirs ignoreversion; Components: app
; The worker, twice: once for this run ({tmp}), once for the uninstaller.
Source: "{#Helper}"; DestDir: "{tmp}"; Flags: dontcopy
Source: "{#Helper}"; DestDir: "{app}\installer"; Flags: ignoreversion
#ifdef BundleUsbip
Source: "{#BundleUsbip}"; DestDir: "{tmp}"; DestName: "{#UsbipFile}"; Flags: dontcopy
#endif
#ifdef BundleHidHide
Source: "{#BundleHidHide}"; DestDir: "{tmp}"; DestName: "{#HidHideFile}"; Flags: dontcopy
#endif
#if Defined(BundleUsbip) || Defined(BundleHidHide)
; A bundled build redistributes a vendor installer, and BSD-2 asks for the
; notice "in the materials provided with the distribution". Next to the
; uninstaller, not under app\ (the updater swaps that directory whole).
Source: "bundle\THIRD-PARTY-NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion
#endif

[Icons]
Name: "{userprograms}\ds5bridge"; Filename: "{app}\app\ds5bridge-tray.exe"; WorkingDir: "{app}\app"; Comment: "ds5bridge -- a Bluetooth DualSense, presented to Windows as a wired one"; Components: app
; Internet shortcuts (.url): Windows opens them with the default browser, so
; no elevated process is involved (the tray hands URLs to explorer for the
; same reason). The tray's own icon, so they are recognisable.
Name: "{userprograms}\ds5bridge dashboard"; Filename: "{code:DashboardUrl}"; IconFilename: "{app}\app\ds5bridge-tray.exe"; Comment: "The ds5bridge dashboard, in your browser"; Components: app; Tasks: dashstartmenu
Name: "{userdesktop}\ds5bridge dashboard"; Filename: "{code:DashboardUrl}"; IconFilename: "{app}\app\ds5bridge-tray.exe"; Comment: "The ds5bridge dashboard, in your browser"; Components: app; Tasks: dashdesktop

; No [Registry] section any more: start-with-Windows is the service (helper:
; service-install, run in ssPostInstall) or the scheduled task (helper:
; autostart-enable) -- the same task the tray's own "Start at login" switch
; creates. The HKCU Run value the 0.4 installers wrote cannot start an
; elevated program, and the tray is one now.

[Run]
; "Start ds5bridge now" on the Finish page: in service mode that means
; starting the SERVICE (its supervisor starts the tray in this session; a
; tray started by hand next to it would be a second instance), otherwise the
; tray exe itself. The tray carries requireAdministrator (ds5bridge.spec), so
; it is launched from this elevated process directly -- no runasoriginaluser,
; which would put a UAC prompt behind the Finish button. skipifsilent: a
; silent install does not start anything unless told to with /STARTTRAY=1
; (CurStepChanged/ssDone), which is how the in-app updater gets its new tray
; -- or service -- back.
Filename: "{app}\app\ds5bridge.exe"; Parameters: "service start"; WorkingDir: "{app}\app"; Description: "Start ds5bridge now"; Flags: postinstall nowait skipifsilent runhidden; Components: app; Tasks: autostart\service
Filename: "{app}\app\ds5bridge-tray.exe"; WorkingDir: "{app}\app"; Description: "Start ds5bridge now"; Flags: postinstall nowait skipifsilent; Components: app; Tasks: not autostart\service

[UninstallDelete]
; Files the updater put there after us (a swapped app dir, staged updates).
Type: filesandordirs; Name: "{app}\app"
; The service's logs. Its settings (config.json, the hide journal) stay
; unless "delete my settings" is ticked, like the per-user ones.
Type: filesandordirs; Name: "{commonappdata}\ds5bridge\logs"
Type: filesandordirs; Name: "{app}\updates"
Type: filesandordirs; Name: "{app}\installer"
; The copy of this setup a restart-and-resume left behind (RegisterResume).
Type: filesandordirs; Name: "{commonappdata}\ds5bridge\resume"

[Code]
const
  UsbipVersion = '{#UsbipVersion}';
  // Where this installer records which driver packages IT installed
  // (UsbipInstalledByUs / HidHideInstalledByUs = 1) as opposed to found
  // already there (0). The uninstaller defaults to removing the former and
  // keeping the latter; a value survives an uninstall that keeps its driver.
  SetupKey = 'SOFTWARE\ds5bridge\Setup';
  // Where RegisterResume asks Windows to start this setup again after the
  // restart the preflight found necessary, and the copy of the exe it runs.
  ResumeRunOnceKey = 'SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce';
  ResumeRunOnceValue = 'ds5bridge-setup';
  ResumeDir = '{commonappdata}\ds5bridge\resume';

var
  // machine state at wizard start
  HaveUsbipVer: String;     // '' when not installed
  HaveHidHideCli: String;   // '' when not installed
  HaveService: Boolean;     // the ds5bridge Windows service is registered
  HaveTask: Boolean;        // the ds5bridge logon task is registered
  // what this run will actually do
  NeedUsbip, NeedHidHide: Boolean;
  UsbipInstaller, HidHideInstaller: String;
  // results
  Summary: TStringList;
  RebootNote: Boolean;
  FailCount: Integer;
  DownloadPage: TDownloadWizardPage;
  SummaryPage: TOutputMsgMemoWizardPage;
  HelperPath: String;
  LastFail: String;         // the last FAIL line a helper wrote ('label -- detail')

// ---------------------------------------------------------------------------
// looking at the machine (the cheap checks the wizard needs before the helper
// is even extracted)
// ---------------------------------------------------------------------------

// First line of a program's stdout, or '' -- and -1 when it could not run.
function RunCapture(const Exe, Args: String; var Output: String): Integer;
var
  RC: Integer;
  Res: TExecOutput;
begin
  Output := '';
  RC := -1;
  try
    if ExecAndCaptureOutput(Exe, Args, '', SW_HIDE, ewWaitUntilTerminated, RC, Res) then
      if GetArrayLength(Res.StdOut) > 0 then
        Output := Res.StdOut[0];
  except
    Log('RunCapture ' + Exe + ': ' + GetExceptionMessage);
    RC := -1;
  end;
  Result := RC;
end;

function UsbipExePath(): String;
var
  Loc: String;
begin
  Result := '';
  // The Inno uninstall key usbip-win2 writes; narrow so usbipd-win never matches.
  if RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{199505b0-b93d-4521-a8c7-897818e0205a}_is1',
                         'InstallLocation', Loc) then
    if FileExists(AddBackslash(Loc) + 'usbip.exe') then
      Result := AddBackslash(Loc) + 'usbip.exe';
  if Result = '' then
    if FileExists(ExpandConstant('{commonpf64}\USBip\usbip.exe')) then
      Result := ExpandConstant('{commonpf64}\USBip\usbip.exe');
end;

// '' when usbip-win2's driver is usable; otherwise why an install found on
// disk must not be adopted: its service was set not to start (an uninstall's
// stage A, waiting for its reboot) or is marked for deletion (stage B ran,
// the name is released at boot). The preflight puts both right (helper:
// Resolve-UsbipRemovalPending); the wizard only has to want a fresh install.
function UsbipDriverUnusable(): String;
var
  D: Cardinal;
begin
  Result := '';
  if RegQueryDWordValue(HKLM, 'SYSTEM\CurrentControlSet\Services\usbip2_ude', 'DeleteFlag', D) and (D = 1) then
    Result := 'service usbip2_ude is marked for deletion'
  else if RegQueryDWordValue(HKLM, 'SYSTEM\CurrentControlSet\Services\usbip2_filter', 'DeleteFlag', D) and (D = 1) then
    Result := 'service usbip2_filter is marked for deletion'
  else if RegQueryDWordValue(HKLM, 'SYSTEM\CurrentControlSet\Services\usbip2_ude', 'Start', D) and (D = 4) then
    Result := 'service usbip2_ude is disabled (a removal waiting for its reboot)';
end;

function InstalledUsbipVersion(): String;
var
  Exe, Txt, Why: String;
begin
  Result := '';
  Exe := UsbipExePath();
  if Exe = '' then Exit;
  Why := UsbipDriverUnusable();
  if Why <> '' then
  begin
    Log('usbip-win2 found at ' + Exe + ' but not counted as installed: ' + Why);
    Exit;
  end;
  if RunCapture(Exe, '--version', Txt) = 0 then
    Result := Trim(Txt)
  else
    Result := '?';
end;

function HidHideCliPath(): String;
var
  P: String;
begin
  Result := '';
  P := ExpandConstant('{commonpf64}\Nefarius Software Solutions\HidHide\x64\HidHideCLI.exe');
  if FileExists(P) then begin Result := P; Exit; end;
  P := ExpandConstant('{commonpf64}\Nefarius Software Solutions e.U\HidHide\x64\HidHideCLI.exe');
  if FileExists(P) then begin Result := P; Exit; end;
  P := ExpandConstant('{commonpf64}\Nefarius Software Solutions\HidHide\HidHideCLI.exe');
  if FileExists(P) then begin Result := P; Exit; end;
end;

// How the machine starts ds5bridge today (for the Tasks page and the plan).
function ServiceRegistered(): Boolean;
var
  Txt: String;
begin
  Result := RunCapture(ExpandConstant('{sys}\sc.exe'), 'query ds5bridge', Txt) = 0;
end;

function TaskRegistered(): Boolean;
var
  Txt: String;
begin
  Result := RunCapture(ExpandConstant('{sys}\schtasks.exe'), '/Query /TN ds5bridge', Txt) = 0;
end;

// "dashboard_port": N out of a config.json, without a JSON parser: the key,
// then digits. '' when the file or the key is not there.
function ReadDashboardPort(const Path: String): String;
var
  Txt: AnsiString;
  S: String;
  I: Integer;
begin
  Result := '';
  if not FileExists(Path) then Exit;
  if not LoadStringFromFile(Path, Txt) then Exit;
  S := String(Txt);
  I := Pos('"dashboard_port"', S);
  if I = 0 then Exit;
  I := I + Length('"dashboard_port"');
  while (I <= Length(S)) and ((S[I] = ' ') or (S[I] = ':') or (S[I] = #9)) do I := I + 1;
  while (I <= Length(S)) and (S[I] >= '0') and (S[I] <= '9') do
  begin
    Result := Result + S[I];
    I := I + 1;
  end;
end;

// The dashboard's address for the shortcuts: the service's settings first
// (they win once service mode is installed), then this user's, else the
// default port (config.DEFAULT_DASHBOARD_PORT).
function DashboardUrl(Param: String): String;
var
  Port: String;
begin
  Port := ReadDashboardPort(ExpandConstant('{commonappdata}\ds5bridge\config.json'));
  if Port = '' then
    Port := ReadDashboardPort(ExpandConstant('{userappdata}\ds5bridge\config.json'));
  if Port = '' then Port := '8765';
  Result := 'http://127.0.0.1:' + Port + '/';
end;

function ServiceModeSelected(): Boolean;
begin
  // The parent box with the service radio -- or the parent alone, which is
  // what a remembered 0.5 selection ('autostart' = the task then) becomes:
  // the recommended mode, documented in docs/installer.md.
  Result := WizardIsTaskSelected('autostart') and not WizardIsTaskSelected('autostart\task');
end;

// Adoption bookkeeping. Name is 'Usbip' or 'HidHide'. "Installed by us"
// is written unconditionally; "found already there" only when nothing is
// recorded yet, so a package we installed on a previous run stays ours.
procedure RecordOrigin(const Name: String; InstalledByUs: Boolean);
var
  D: Cardinal;
begin
  if InstalledByUs then
    RegWriteDWordValue(HKLM, SetupKey, Name + 'InstalledByUs', 1)
  else if not RegQueryDWordValue(HKLM, SetupKey, Name + 'InstalledByUs', D) then
    RegWriteDWordValue(HKLM, SetupKey, Name + 'InstalledByUs', 0);
end;

// 1 = ours, 0 = adopted, -1 = no record (installed before this bookkeeping
// existed, or by scripts\install.ps1).
function RecordedOrigin(const Name: String): Integer;
var
  D: Cardinal;
begin
  if RegQueryDWordValue(HKLM, SetupKey, Name + 'InstalledByUs', D) then
    Result := Integer(D)
  else
    Result := -1;
end;

// ---------------------------------------------------------------------------
// the helper
// ---------------------------------------------------------------------------

procedure AddSummary(const Line: String);
begin
  Summary.Add(Line);
  Log('summary: ' + Line);
end;

// Run one helper verb, fold its result lines into the summary. Returns the
// helper's exit code (1 when it wrote a FAIL line).
function RunHelper(const Verb, ExtraArgs: String): Integer;
var
  ResFile, Args, Line, Kind, Rest, Tag: String;
  Lines: TArrayOfString;
  I, P, RC: Integer;
begin
  ResFile := ExpandConstant('{tmp}\helper-' + Verb + '.txt');
  DeleteFile(ResFile);
  // /HELPERARGS="..." on the setup command line goes to every helper call:
  // how the tests walk the refusal and restart paths on a healthy machine
  // (-MockBusy, -MockPending, -BusyWaitSec). Not for real installs.
  Args := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + HelperPath + '" ' + Verb +
          ' -Out "' + ResFile + '" -AppDir "' + ExpandConstant('{app}\app') + '" ' + ExtraArgs +
          ' ' + ExpandConstant('{param:HELPERARGS|}');
  Log('helper: powershell.exe ' + Args);
  RC := -1;
  try
    // Its stdout goes to the setup log line by line as it happens.
    if not ExecAndLogOutput(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Args, '',
                            SW_HIDE, ewWaitUntilTerminated, RC, nil) then
      RC := -1;
  except
    Log('helper: ' + GetExceptionMessage);
    RC := -1;
  end;
  if RC = -1 then
  begin
    AddSummary('[FAIL] ' + Verb + ' -- could not start powershell.exe');
    Result := 1;
    Exit;
  end;
  Log('helper ' + Verb + ' exit ' + IntToStr(RC));
  if LoadStringsFromFile(ResFile, Lines) then
    for I := 0 to GetArrayLength(Lines) - 1 do
    begin
      Line := Lines[I];
      P := Pos('|', Line);
      if P = 0 then Continue;
      Kind := Copy(Line, 1, P - 1);
      Rest := Copy(Line, P + 1, Length(Line));
      P := Pos('|', Rest);
      if P > 0 then Rest := Copy(Rest, 1, P - 1) + ' -- ' + Copy(Rest, P + 1, Length(Rest));
      if Kind = 'REBOOT' then
      begin
        RebootNote := True;
        Tag := '[reboot]';
      end
      else if Kind = 'PASS' then Tag := '[ok]'
      else if Kind = 'FAIL' then begin Tag := '[FAIL]'; FailCount := FailCount + 1; LastFail := Rest; end
      else if Kind = 'WARN' then Tag := '[warn]'
      else Tag := '[' + Lowercase(Kind) + ']';
      AddSummary(Tag + ' ' + Rest);
    end
  else
    AddSummary('[warn] ' + Verb + ' -- wrote no report (exit ' + IntToStr(RC) + ')');
  Result := RC;
end;

// ---------------------------------------------------------------------------
// wizard
// ---------------------------------------------------------------------------

function OnDownloadProgress(const Url, FileName: String; const Progress, ProgressMax: Int64): Boolean;
begin
  if Progress = ProgressMax then
    Log('downloaded ' + FileName + ' (' + IntToStr(ProgressMax) + ' bytes)');
  Result := True;
end;

// Ask Windows to run this setup again at the next logon (a RunOnce value,
// which Windows deletes as it fires), from a copy of the exe under
// ProgramData: the original may be in a Downloads folder that is cleaned, or
// on a drive that is not there after the restart. Silent runs are not
// resumed -- whoever scripted one reruns it, and a window at logon is not
// what a scripted install expects.
procedure RegisterResume();
var
  Src, Dir, Dst, Cmd: String;
  RC: Integer;
begin
  if WizardSilent then
  begin
    Log('resume after restart: not registered in a silent run; run this setup again after the restart');
    Exit;
  end;
  Src := ExpandConstant('{srcexe}');
  Dir := ExpandConstant(ResumeDir);
  Dst := AddBackslash(Dir) + ExtractFileName(Src);
  if CompareText(Src, Dst) <> 0 then
  begin
    ForceDirectories(Dir);
    // FileCopy returns False for the running setup exe (measured with
    // Inno 6.7.3: no Windows error behind it); cmd's copy does it fine.
    if not FileCopy(Src, Dst, False) then
      Exec(ExpandConstant('{cmd}'), '/C copy /Y "' + Src + '" "' + Dst + '"', '', SW_HIDE, ewWaitUntilTerminated, RC);
    if not FileExists(Dst) then
    begin
      Log('resume after restart: could not copy ' + Src + ' to ' + Dst + '; the original path is used');
      Dst := Src;
    end;
  end;
  Cmd := '"' + Dst + '" /RESUMEAFTERRESTART=1';
  if RegWriteStringValue(HKLM, ResumeRunOnceKey, ResumeRunOnceValue, Cmd) then
    Log('resume after restart: registered RunOnce ' + ResumeRunOnceValue + ' = ' + Cmd)
  else
    Log('resume after restart: could not write RunOnce ' + ResumeRunOnceValue + '; run this setup again after the restart');
end;

function InitializeSetup(): Boolean;
var
  Dir: String;
begin
  Result := True;
  if ExpandConstant('{param:RESUMEAFTERRESTART|0}') = '1' then
    Log('resumed after a restart (RunOnce)');
  // Whatever run registered a relaunch, this run supersedes it: a RunOnce
  // deletes its value as it fires, this covers a manual rerun before the
  // restart. The copy it points at is removed once nothing runs from it.
  RegDeleteValue(HKLM, ResumeRunOnceKey, ResumeRunOnceValue);
  Dir := ExpandConstant(ResumeDir);
  if DirExists(Dir) and (Pos(Lowercase(AddBackslash(Dir)), Lowercase(ExpandConstant('{srcexe}'))) = 0) then
    DelTree(Dir, True, True, True);
end;

procedure InitializeWizard;
begin
  Summary := TStringList.Create;
  HaveUsbipVer := InstalledUsbipVersion();
  HaveHidHideCli := HidHideCliPath();
  HaveService := ServiceRegistered();
  HaveTask := TaskRegistered();
  Log('found usbip-win2: "' + HaveUsbipVer + '"; HidHideCLI: "' + HaveHidHideCli + '"' +
      '; service: ' + IntToStr(Integer(HaveService)) + '; logon task: ' + IntToStr(Integer(HaveTask)));
  DownloadPage := CreateDownloadPage(SetupMessage(msgWizardPreparing),
                                     SetupMessage(msgPreparingDesc), @OnDownloadProgress);
  DownloadPage.ShowBaseNameInsteadOfUrl := True;
  SummaryPage := CreateOutputMsgMemoPage(wpInfoAfter, 'Installation check',
    'What was verified on this machine after installing',
    'Each line is one check the installer ran. A copy is in the setup log.', '');
end;

procedure CurPageChanged(CurPageID: Integer);
var
  Text: String;
  I: Integer;
begin
  if CurPageID = wpSelectComponents then
  begin
    // Say what is already there. The box stays ticked -- "I want usbip-win2"
    // is still the right answer -- but the work is skipped.
    if HaveUsbipVer = UsbipVersion then
      WizardForm.ComponentsList.ItemCaption[1] := 'usbip-win2 ' + UsbipVersion + ' -- already installed (will be verified, not reinstalled)'
    else if HaveUsbipVer = '0.9.7.8' then
      WizardForm.ComponentsList.ItemCaption[1] := 'usbip-win2 ' + UsbipVersion + ' -- REPLACES the installed 0.9.7.8, which its maintainer warns can corrupt memory'
    else if HaveUsbipVer <> '' then
      WizardForm.ComponentsList.ItemCaption[1] := 'usbip-win2 ' + UsbipVersion + ' -- replaces the installed ' + HaveUsbipVer + ' ({#UsbipHow})';
    if HaveHidHideCli <> '' then
      WizardForm.ComponentsList.ItemCaption[2] := 'HidHide -- already installed (will be verified, not reinstalled)';
  end
  else if CurPageID = SummaryPage.ID then
  begin
    Text := '';
    for I := 0 to Summary.Count - 1 do
      Text := Text + Summary[I] + #13#10;
    if FailCount > 0 then
      Text := IntToStr(FailCount) + ' check(s) FAILED. Run "ds5bridge.exe doctor" for more detail, or re-run this installer.' + #13#10#13#10 + Text
    else
      Text := 'All checks passed.' + #13#10#13#10 + Text;
    if RebootNote then
      Text := Text + #13#10 + 'A REBOOT is recommended before hide-while-bridged is relied on (see the [reboot] line above). Nothing will restart on its own.' + #13#10;
    SummaryPage.RichEditViewer.Lines.Text := Text;
  end
  else if CurPageID = wpFinished then
  begin
    if RebootNote then
      WizardForm.FinishedLabel.Caption := WizardForm.FinishedLabel.Caption + #13#10#13#10 +
        'Reboot recommended: HidHide''s filter could not be attached to every connected controller ' +
        '(see the Installation check page); a reboot, or switching the controller off and on, finishes that. ' +
        'Everything else works now.';
  end;
end;

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo,
                         MemoComponentsInfo, MemoGroupInfo, MemoTasksInfo: String): String;
var
  S: String;
begin
  S := '';
  if MemoComponentsInfo <> '' then S := S + MemoComponentsInfo + NewLine + NewLine;
  if MemoTasksInfo <> '' then S := S + MemoTasksInfo + NewLine + NewLine;
  S := S + 'Plan:' + NewLine;
  if WizardIsComponentSelected('usbip') then
  begin
    if HaveUsbipVer = UsbipVersion then
      S := S + Space + 'usbip-win2 ' + UsbipVersion + ': already installed, skip' + NewLine
    else
    begin
      S := S + Space + 'usbip-win2 ' + UsbipVersion + ': ';
      #ifdef BundleUsbip
      S := S + 'install the bundled copy';
      #else
      S := S + 'download ' + '{#UsbipUrl}';
      #endif
      S := S + NewLine + Space + Space + '(SHA-256 verified, then run silently; your USB 3.0 hubs restart briefly)' + NewLine;
    end;
  end;
  if WizardIsComponentSelected('hidhide') then
  begin
    if HaveHidHideCli <> '' then
      S := S + Space + 'HidHide: already installed, skip' + NewLine
    else
    begin
      S := S + Space + 'HidHide {#HidHideVersion}: ';
      #ifdef BundleHidHide
      S := S + 'install the bundled copy';
      #else
      S := S + 'download ' + '{#HidHideUrl}';
      #endif
      S := S + NewLine + Space + Space + '(SHA-256 verified, then run silently; its filter is then attached to any connected DualSense, so usually no reboot)' + NewLine;
    end;
  end;
  if WizardIsComponentSelected('app') then
    S := S + Space + 'ds5bridge {#AppVersion}: install to ' + ExpandConstant('{app}\app') + NewLine;
  if WizardIsTaskSelected('autostart\task') then
    S := S + Space + 'start at sign-in: the scheduled task ''ds5bridge'' (any ds5bridge service is removed)' + NewLine
  else if WizardIsTaskSelected('autostart') then
    S := S + Space + 'start with Windows: the service ''ds5bridge'', before anyone signs in (settings in ' +
         ExpandConstant('{commonappdata}\ds5bridge') + '; any logon task is removed)' + NewLine
  else
    S := S + Space + 'start with Windows: off (any ds5bridge service or logon task is removed)' + NewLine;
  if WizardIsTaskSelected('dashstartmenu') or WizardIsTaskSelected('dashdesktop') then
    S := S + Space + 'dashboard shortcut(s) -> ' + DashboardUrl('') + NewLine;
  S := S + Space + 'then verify everything and show the result' + NewLine;
  Result := S;
end;

// The download step. After the Ready page, before installing. This is the
// ONLY place that knows whether an installer is downloaded or bundled.
function GetInstallerFile(const BaseName, Url, Sha256: String; Bundled: Boolean; var Path: String): Boolean;
var
  Actual: String;
begin
  Result := False;
  Path := ExpandConstant('{tmp}\') + BaseName;
  if Bundled then
  begin
    ExtractTemporaryFile(BaseName);
  end
  else
  begin
    DownloadPage.Clear;
    DownloadPage.Add(Url, BaseName, Sha256);
    DownloadPage.Show;
    try
      try
        DownloadPage.Download;   // raises on failure, including a hash mismatch
      except
        Log('download failed: ' + GetExceptionMessage);
        DeleteFile(Path);
        Exit;
      end;
    finally
      DownloadPage.Hide;
    end;
  end;
  // Trust nothing until the file on disk answers for itself.
  Actual := Lowercase(GetSHA256OfFile(Path));
  if Actual <> Lowercase(Sha256) then
  begin
    Log(BaseName + ': SHA-256 mismatch (got ' + Actual + ', expected ' + Sha256 + '); discarded');
    DeleteFile(Path);
    Exit;
  end;
  Log(BaseName + ': SHA-256 verified');
  Result := True;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Bundled: Boolean;
begin
  Result := True;
  if CurPageID <> wpReady then Exit;

  // On the record, for silent runs especially: what was actually selected.
  Log('components selected: ' + WizardSelectedComponents(False) +
      '; tasks selected: ' + WizardSelectedTasks(False));
  // Belt to the "fixed" braces above: an install that installs nothing must
  // not end with "all checks passed".
  if not WizardIsComponentSelected('app') then
  begin
    Log('the app component is not selected -- refusing to continue (check /COMPONENTS)');
    SuppressibleMsgBox('Nothing is selected to install. The ds5bridge app is always required; ' +
      'check the /COMPONENTS switch (names: app, usbip, hidhide).', mbError, MB_OK, IDOK);
    Result := False;
    Exit;
  end;
  NeedUsbip := WizardIsComponentSelected('usbip') and (HaveUsbipVer <> UsbipVersion);
  NeedHidHide := WizardIsComponentSelected('hidhide') and (HaveHidHideCli = '');

  if NeedUsbip then
  begin
    #ifdef BundleUsbip
    Bundled := True;
    #else
    Bundled := False;
    #endif
    if not GetInstallerFile('{#UsbipFile}', '{#UsbipUrl}', '{#UsbipSha256}', Bundled, UsbipInstaller) then
    begin
      SuppressibleMsgBox('usbip-win2 ' + UsbipVersion + ' could not be downloaded and verified. ' +
        'Without it there is nothing for the virtual controller to attach to, so the install stops here. ' +
        'Check your connection and try again, or untick usbip-win2 to install only the rest.' + #13#10#13#10 +
        '{#UsbipUrl}', mbError, MB_OK, IDOK);
      Result := False;
      Exit;
    end;
  end;

  if NeedHidHide then
  begin
    #ifdef BundleHidHide
    Bundled := True;
    #else
    Bundled := False;
    #endif
    if not GetInstallerFile('{#HidHideFile}', '{#HidHideUrl}', '{#HidHideSha256}', Bundled, HidHideInstaller) then
    begin
      // A failure here is a warning, not a stop: the bridge works without it.
      AddSummary('[warn] HidHide -- could not be downloaded and verified; skipped. Everything else still works; ' +
                 'only hide-while-bridged needs it. Install it later from https://github.com/nefarius/HidHide/releases');
      NeedHidHide := False;
    end;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  RC: Integer;
begin
  Result := '';
  ExtractTemporaryFile('{#Helper}');
  HelperPath := ExpandConstant('{tmp}\{#Helper}');
  // Before anything changes: is another installer at work (two driver
  // installers at once can wedge Plug and Play; the helper waits a minute
  // for it first), was a usbip-win2 removal left half-done (the helper
  // finishes it, or says a restart must come first: exit 4), and is there
  // an orphaned HidHide filter entry to repair (done on its own). A busy
  // machine stops a driver install here; an app-only run goes on. The
  // usbip-win2 part is skipped when usbip-win2 is not being installed:
  // nothing of it may be touched then.
  LastFail := '';
  if NeedUsbip then
    RC := RunHelper('preflight', '')
  else
    RC := RunHelper('preflight', '-NoUsbip');
  if (RC <> 0) and (NeedUsbip or NeedHidHide) then
  begin
    if LastFail = '' then LastFail := 'see the setup log';
    if RC = 4 then
    begin
      // The helper's FAIL line says why and that Setup continues after the
      // restart; RegisterResume makes that true (interactive runs). The
      // wizard shows this text, then [Messages] PrepareToInstallNeedsRestart
      // with the restart-now radios; a silent run exits with Inno's code
      // for "restart required" (or restarts, unless /NORESTART).
      NeedsRestart := True;
      Result := 'Windows must restart before the driver can be installed: ' + LastFail;
      RegisterResume();
    end
    else
      Result := 'A driver cannot be installed right now: ' + LastFail + #13#10#13#10 +
                'Installing a driver in that state could leave Windows unable to enumerate devices. ' +
                'Once it is done, click Back, then Next to try again.';
    Exit;
  end;
  // A running tray holds locks on the app dir and, if usbip-win2 is about to
  // be replaced, an attachment on the driver being replaced. Its own
  // teardown (detach, unhide) runs when it is asked to close.
  if NeedUsbip then
    RC := RunHelper('teardown', '')
  else
    RC := RunHelper('stop-app', '');
  if RC <> 0 then
    Result := 'ds5bridge is still running and could not be stopped. Quit it from its tray icon (right-click > Quit) and run this installer again.';
end;

procedure InstallDrivers;
var
  RC: Integer;
  DoUsbip: Boolean;
begin
  if NeedUsbip then
  begin
    DoUsbip := True;
    if WizardIsTaskSelected('restorepoint') then
    begin
      WizardForm.StatusLabel.Caption := 'Creating a System Restore point (this can take a minute) ...';
      // The helper has already written a [warn] line when this fails. A
      // silent install goes on (there is nobody to ask); a person is asked.
      if RunHelper('restore-point', '') <> 0 then
        if not WizardSilent then
          if SuppressibleMsgBox('A System Restore point could not be created (System Restore may be disabled). ' +
               'usbip-win2 installs two kernel drivers and its own README recommends one first.' + #13#10#13#10 +
               'Install the driver anyway?', mbConfirmation, MB_YESNO, IDNO) <> IDYES then
          begin
            AddSummary('[FAIL] usbip-win2 -- not installed: you chose to stop when no restore point could be made');
            FailCount := FailCount + 1;
            DoUsbip := False;
          end;
    end;
    // Checked again right before the driver goes in: the preflight was a
    // download and a restore point ago.
    if DoUsbip and (RunHelper('check-busy', '') <> 0) then
    begin
      AddSummary('[FAIL] usbip-win2 -- not installed: another installer is running; run this setup again when it is done');
      DoUsbip := False;
    end;
    if DoUsbip then
    begin
    WizardForm.StatusLabel.Caption := 'Installing usbip-win2 ' + UsbipVersion + ' (USB 3.0 devices will blink out and come back) ...';
    if Exec(UsbipInstaller, '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART', '', SW_SHOW, ewWaitUntilTerminated, RC) then
    begin
      Log('usbip-win2 installer exit ' + IntToStr(RC));
      if RC = 0 then
      begin
        AddSummary('[ok] usbip-win2 installer -- ran (exit 0)');
        RecordOrigin('Usbip', True);
      end
      else if RC = 3010 then
      begin
        AddSummary('[ok] usbip-win2 installer -- ran (exit 3010: reboot flagged)');
        RecordOrigin('Usbip', True);
        RebootNote := True;
      end
      else
      begin
        AddSummary('[FAIL] usbip-win2 installer -- exited with ' + IntToStr(RC));
        FailCount := FailCount + 1;
      end;
    end
    else
    begin
      AddSummary('[FAIL] usbip-win2 installer -- could not be started');
      FailCount := FailCount + 1;
    end;
    end;
  end
  else if WizardIsComponentSelected('usbip') then
  begin
    AddSummary('[ok] usbip-win2 ' + UsbipVersion + ' -- was already installed');
    RecordOrigin('Usbip', False);
  end;

  if NeedHidHide then
  begin
    if RunHelper('check-busy', '') <> 0 then
    begin
      AddSummary('[warn] HidHide -- not installed: another installer is running; the bridge works without it, install it later');
      NeedHidHide := False;
    end;
  end;
  if NeedHidHide then
  begin
    WizardForm.StatusLabel.Caption := 'Installing HidHide {#HidHideVersion} ...';
    // Silent switches per winget's manifest for this exact package.
    if Exec(HidHideInstaller, '/exenoui /qn /norestart', '', SW_SHOW, ewWaitUntilTerminated, RC) then
    begin
      Log('HidHide installer exit ' + IntToStr(RC));
      if (RC = 0) or (RC = 1641) or (RC = 3010) then
      begin
        AddSummary('[ok] HidHide installer -- ran (exit ' + IntToStr(RC) + ')');
        RecordOrigin('HidHide', True);
        // Its service starts at once, but the filter only joins HID stacks
        // built after it was registered, so a pad that is already paired
        // is NOT hidden until its stack is rebuilt. Instead of asking for
        // a reboot, the helper restarts the connected DualSenses' HIDClass
        // devnodes (only those) and proves the attachment from
        // DEVPKEY_Device_Stack; it writes the [reboot] line only for a pad
        // it could not fix, and RunHelper sets RebootNote from that.
        WizardForm.StatusLabel.Caption := 'Attaching HidHide to the connected controllers ...';
        RunHelper('hidhide-attach', '');
      end
      else
        AddSummary('[warn] HidHide installer -- exited with ' + IntToStr(RC) + '; the bridge works without it');
    end
    else
      AddSummary('[warn] HidHide installer -- could not be started; the bridge works without it');
  end
  else if WizardIsComponentSelected('hidhide') and (HaveHidHideCli <> '') then
  begin
    AddSummary('[ok] HidHide -- was already installed');
    RecordOrigin('HidHide', False);
  end
  else if not WizardIsComponentSelected('hidhide') then
    AddSummary('[info] HidHide -- not selected; hide-while-bridged will be unavailable');
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Expect: String;
  OutFile: String;
  RC: Integer;
begin
  if CurStep = ssInstall then
    InstallDrivers
  else if CurStep = ssPostInstall then
  begin
    // Start with Windows: the service or the logon task, each helper verb
    // removing the other (they are exclusive); neither ticked removes both.
    // Before the verification so the result lines are in the summary. The
    // service is registered here and STARTED by [Run] / STARTTRAY=1 below.
    if WizardIsTaskSelected('autostart\task') then
    begin
      WizardForm.StatusLabel.Caption := 'Registering start-at-login ...';
      RunHelper('autostart-enable', '');
    end
    else if WizardIsTaskSelected('autostart') then
    begin
      WizardForm.StatusLabel.Caption := 'Registering the ds5bridge service ...';
      RunHelper('service-install', '');
    end
    else
    begin
      RunHelper('autostart-disable', '');
      RunHelper('service-uninstall', '');
    end;
    WizardForm.StatusLabel.Caption := 'Verifying the installation ...';
    // The app is always expected (it is a fixed component): a missing
    // ds5bridge.exe is a [FAIL], never an [info].
    Expect := ' -ExpectApp';
    if ServiceModeSelected() then Expect := Expect + ' -ExpectService';
    if WizardIsComponentSelected('usbip') then Expect := Expect + ' -ExpectUsbip';
    if WizardIsComponentSelected('hidhide') and ((HaveHidHideCli <> '') or NeedHidHide) then Expect := Expect + ' -ExpectHidHide';
    RunHelper('verify-install', Expect);
    // A machine-readable copy next to the app, for people who installed
    // silently and for bug reports.
    OutFile := ExpandConstant('{app}\installer\last-install-check.txt');
    ForceDirectories(ExtractFileDir(OutFile));
    Summary.SaveToFile(OutFile);
    if FailCount > 0 then
      Log(IntToStr(FailCount) + ' verification check(s) FAILED')
    else
      Log('all verification checks passed');
  end
  else if CurStep = ssDone then
  begin
    // /STARTTRAY=1: our own parameter, for the in-app updater (update.py),
    // which runs this installer /VERYSILENT and therefore gets no [Run]
    // "Start ds5bridge now". This process is elevated and so is the tray,
    // so the launch needs no prompt. Never in an interactive run: the
    // Finish page's checkbox is the user's to tick.
    if WizardSilent and (ExpandConstant('{param:STARTTRAY|0}') = '1') and (FailCount = 0) then
    begin
      if ServiceModeSelected() then
      begin
        // Service mode: the service starts the tray (in the console
        // session, as SYSTEM); starting the exe here would make two.
        Log('STARTTRAY=1: starting the ds5bridge service');
        if Exec(ExpandConstant('{app}\app\ds5bridge.exe'), 'service start', ExpandConstant('{app}\app'),
                SW_HIDE, ewWaitUntilTerminated, RC) then
          Log('ds5bridge.exe service start: exit ' + IntToStr(RC))
        else
          Log('the service could not be started (' + SysErrorMessage(RC) + ')');
      end
      else
      begin
        Log('STARTTRAY=1: starting the tray');
        if not Exec(ExpandConstant('{app}\app\ds5bridge-tray.exe'), '', ExpandConstant('{app}\app'),
                    SW_SHOWNORMAL, ewNoWait, RC) then
          Log('the tray could not be started (' + SysErrorMessage(RC) + ')');
      end;
    end;
  end;
end;

// ---------------------------------------------------------------------------
// uninstaller
// ---------------------------------------------------------------------------

var
  RemoveUsbip, RemoveHidHide, PurgeSettings: Boolean;
  UsbipOrigin, HidHideOrigin: Integer;   // RecordedOrigin(): 1 ours, 0 adopted, -1 unknown
  UsbipPendingReboot: Boolean;            // remove-usbip returned 3

function UninstallParam(const Name: String): Boolean;
begin
  Result := ExpandConstant('{param:' + Name + '|0}') = '1';
end;

function BoolStr(B: Boolean): String;
begin
  if B then Result := 'yes' else Result := 'no';
end;

function OriginText(Origin: Integer): String;
begin
  case Origin of
    1: Result := 'installed by ds5bridge setup';
    0: Result := 'was already installed before ds5bridge; kept unless ticked';
  else
    Result := 'origin unknown';
  end;
end;

function AskUninstallOptions(): Boolean;
var
  Form: TSetupForm;
  Info: TNewStaticText;
  CbUsbip, CbHidHide, CbPurge: TNewCheckBox;
  Ok, Cancel: TNewButton;
begin
  Form := CreateCustomForm(ScaleX(470), ScaleY(230), False, True);
  try
    Form.Caption := 'Uninstall ds5bridge';

    Info := TNewStaticText.Create(Form);
    Info.Parent := Form;
    Info.Left := ScaleX(16); Info.Top := ScaleY(12); Info.Width := ScaleX(440);
    Info.WordWrap := True;
    Info.AutoSize := True;
    Info.Caption := 'ds5bridge itself (the app, its shortcuts, and its start-with-Windows service or logon task) will be removed. ' +
      'Before that, any bridged controller is detached and any hidden controller is made visible again.' + #13#10#13#10 +
      'The two drivers are separate products. Ticked = removed too; untick to keep one ' +
      '(keep it if another program, such as DS4Windows, uses it). Removing either needs ONE restart afterwards:';

    CbUsbip := TNewCheckBox.Create(Form);
    CbUsbip.Parent := Form;
    CbUsbip.Left := ScaleX(24); CbUsbip.Top := ScaleY(112); CbUsbip.Width := ScaleX(430);
    CbUsbip.Caption := 'Also remove usbip-win2 (' + OriginText(UsbipOrigin) + ')';
    CbUsbip.Checked := RemoveUsbip;
    CbUsbip.Enabled := UsbipExePath() <> '';
    if not CbUsbip.Enabled then CbUsbip.Caption := 'Also remove usbip-win2 -- not installed';

    CbHidHide := TNewCheckBox.Create(Form);
    CbHidHide.Parent := Form;
    CbHidHide.Left := ScaleX(24); CbHidHide.Top := ScaleY(136); CbHidHide.Width := ScaleX(430);
    CbHidHide.Caption := 'Also remove HidHide (' + OriginText(HidHideOrigin) + '; asks for a reboot)';
    CbHidHide.Checked := RemoveHidHide;
    CbHidHide.Enabled := HidHideCliPath() <> '';
    if not CbHidHide.Enabled then CbHidHide.Caption := 'Also remove HidHide -- not installed';

    CbPurge := TNewCheckBox.Create(Form);
    CbPurge.Parent := Form;
    CbPurge.Left := ScaleX(24); CbPurge.Top := ScaleY(160); CbPurge.Width := ScaleX(430);
    CbPurge.Caption := 'Delete my settings too (' + ExpandConstant('{userappdata}\ds5bridge') + ' and ' + ExpandConstant('{commonappdata}\ds5bridge') + ')';
    CbPurge.Checked := PurgeSettings;

    Ok := TNewButton.Create(Form);
    Ok.Parent := Form;
    Ok.Width := ScaleX(90); Ok.Height := ScaleY(25);
    Ok.Left := Form.ClientWidth - ScaleX(200); Ok.Top := Form.ClientHeight - ScaleY(36);
    Ok.Caption := 'Uninstall';
    Ok.ModalResult := mrOk;
    Ok.Default := True;

    Cancel := TNewButton.Create(Form);
    Cancel.Parent := Form;
    Cancel.Width := ScaleX(90); Cancel.Height := ScaleY(25);
    Cancel.Left := Form.ClientWidth - ScaleX(104); Cancel.Top := Form.ClientHeight - ScaleY(36);
    Cancel.Caption := 'Cancel';
    Cancel.ModalResult := mrCancel;
    Cancel.Cancel := True;

    Result := Form.ShowModal = mrOk;
    if Result then
    begin
      RemoveUsbip := CbUsbip.Checked and CbUsbip.Enabled;
      RemoveHidHide := CbHidHide.Checked and CbHidHide.Enabled;
      PurgeSettings := CbPurge.Checked;
    end;
  finally
    Form.Free;
  end;
end;

function InitializeUninstall(): Boolean;
begin
  Summary := TStringList.Create;
  // An install that stopped for a restart and was never resumed must not
  // pop up after this uninstall's own reboot.
  RegDeleteValue(HKLM, ResumeRunOnceKey, ResumeRunOnceValue);
  // Defaults: a driver this setup installed goes; one that was already on
  // the machine (adopted, recorded as 0) stays; no record at all (an install
  // older than the bookkeeping, or scripts\install.ps1) counts as ours.
  // Silent switches: /KEEPUSBIP=1 /KEEPHIDHIDE=1 force keep, /REMOVEUSBIP=1
  // /REMOVEHIDHIDE=1 force remove (keep wins), /PURGESETTINGS=1. The dialog
  // offers the same boxes with the origin spelled out.
  UsbipOrigin := RecordedOrigin('Usbip');
  HidHideOrigin := RecordedOrigin('HidHide');
  RemoveUsbip := UsbipOrigin <> 0;
  RemoveHidHide := HidHideOrigin <> 0;
  if UninstallParam('REMOVEUSBIP') then RemoveUsbip := True;
  if UninstallParam('REMOVEHIDHIDE') then RemoveHidHide := True;
  if UninstallParam('KEEPUSBIP') then RemoveUsbip := False;
  if UninstallParam('KEEPHIDHIDE') then RemoveHidHide := False;
  PurgeSettings := UninstallParam('PURGESETTINGS');
  if UsbipExePath() = '' then RemoveUsbip := False;
  if HidHideCliPath() = '' then RemoveHidHide := False;
  Result := True;
  if not UninstallSilent then
    Result := AskUninstallOptions();
  Log('uninstall options: remove usbip-win2=' + BoolStr(RemoveUsbip) + ' (origin ' + IntToStr(UsbipOrigin) +
      '), remove HidHide=' + BoolStr(RemoveHidHide) + ' (origin ' + IntToStr(HidHideOrigin) +
      '), purge settings=' + BoolStr(PurgeSettings));
end;

// Inno asks "restart now?" at the end of an interactive uninstall when this
// answers True -- after our result dialog, which is the order we want: read
// what happened, then be asked. True whenever a [reboot] line was written
// (HidHide removed: its filter unloads at boot; usbip-win2 stage A armed: its
// removal proper runs at the next logon). ONE reboot finishes both.
//
// Never in a silent run. Inno would otherwise restart the machine on its own
// when /NORESTART was forgotten (and reboot without asking under /VERYSILENT);
// a silent uninstaller that reboots the PC is exactly what /NORESTART exists
// to prevent, and this does not rely on the caller remembering it. Silent
// callers read the [reboot] lines in %TEMP%\ds5bridge-uninstall-check.txt.
function UninstallNeedRestart(): Boolean;
begin
  Result := RebootNote and not UninstallSilent;
  Log('UninstallNeedRestart: ' + BoolStr(Result));
end;

procedure ShowUninstallSummary;
var
  Form: TSetupForm;
  Memo: TNewMemo;
  Head: TNewStaticText;
  Ok: TNewButton;
  Text: String;
  I, Top: Integer;
begin
  Text := '';
  for I := 0 to Summary.Count - 1 do
    Text := Text + Summary[I] + #13#10;
  if RebootNote then
    Text := 'What the restart does: HidHide''s filter driver, if it was removed, unloads at boot; ' +
            'usbip-win2''s driver, if it was removed, is disabled now and taken out by the ' +
            '''ds5bridge finish usbip-win2 removal'' task at your next logon (USB 3.0 devices blink ' +
            'out and back once; "USBip" stays in Settings > Apps until then). ' +
            'ONE reboot finishes both halves. Windows will offer to restart when you close this window; ' +
            'nothing restarts on its own before that.' + #13#10#13#10 + Text;
  if FailCount > 0 then
    Text := IntToStr(FailCount) + ' check(s) FAILED -- see below.' + #13#10#13#10 + Text
  else
    Text := 'ds5bridge is uninstalled. All checks passed.' + #13#10#13#10 + Text;

  Form := CreateCustomForm(ScaleX(560), ScaleY(400), False, False);
  try
    Form.Caption := 'ds5bridge uninstall -- result';
    Top := ScaleY(12);
    if RebootNote then
    begin
      // The one line that must not be missed, in bold above the list: the
      // 2026-09-05 feedback was that the [reboot] line got lost in it.
      Head := TNewStaticText.Create(Form);
      Head.Parent := Form;
      Head.Left := ScaleX(12); Head.Top := Top;
      Head.Width := Form.ClientWidth - ScaleX(24);
      Head.WordWrap := True;
      Head.AutoSize := True;
      Head.Font.Style := [fsBold];
      Head.Font.Size := Head.Font.Size + 1;
      Head.Caption := 'A RESTART IS REQUIRED to finish removing the drivers.';
      Top := Head.Top + Head.Height + ScaleY(8);
    end;
    Memo := TNewMemo.Create(Form);
    Memo.Parent := Form;
    Memo.Left := ScaleX(12); Memo.Top := Top;
    Memo.Width := Form.ClientWidth - ScaleX(24); Memo.Height := Form.ClientHeight - Top - ScaleY(48);
    Memo.ReadOnly := True;
    Memo.ScrollBars := ssVertical;
    Memo.WordWrap := True;
    // The memo has focus; without this it swallows Enter instead of letting
    // the default button close the form (seen 2026-09-05).
    Memo.WantReturns := False;
    Memo.Text := Text;
    Ok := TNewButton.Create(Form);
    Ok.Parent := Form;
    Ok.Width := ScaleX(90); Ok.Height := ScaleY(25);
    Ok.Left := Form.ClientWidth - ScaleX(104); Ok.Top := Form.ClientHeight - ScaleY(36);
    Ok.Caption := 'Close';
    Ok.ModalResult := mrOk;
    Ok.Default := True;
    Ok.Cancel := True;
    Form.ActiveControl := Ok;
    Form.ShowModal;
  finally
    Form.Free;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Expect, RunValue, OutFile: String;
  RC: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    // Before a single file goes: the teardown verbs live in the exe that is
    // about to be deleted, and the helper lives next to it.
    HelperPath := ExpandConstant('{app}\installer\{#Helper}');
    if not FileExists(HelperPath) then
    begin
      AddSummary('[warn] helper -- ' + HelperPath + ' is missing; the drivers were left in place. Remove them from Settings > Apps (USBip, HidHide).');
      RemoveUsbip := False;
      RemoveHidHide := False;
    end
    else
    begin
      // Order (docs/bundle-handoff.md section 4): stop the bridge and
      // detach, clear HidHide's lists, HidHide's MSI, usbip-win2 last --
      // each driver step only if no other installer is at work right then.
      UninstallProgressForm.StatusLabel.Caption := 'Stopping ds5bridge and detaching the virtual controller ...';
      RunHelper('teardown', '');
      // The service (stopped by the teardown already; deleted here), the
      // start-at-login task (ours, or the tray's -- same task), and the
      // pre-0.5.0 Run value if it is still there.
      RunHelper('service-uninstall', '');
      RunHelper('autostart-disable', '');
      UninstallProgressForm.StatusLabel.Caption := 'Clearing HidHide entries ...';
      if RemoveHidHide then
        RunHelper('hidhide-clear', '-All')
      else
        RunHelper('hidhide-clear', '');
      if RemoveHidHide then
      begin
        UninstallProgressForm.StatusLabel.Caption := 'Uninstalling HidHide ...';
        if RunHelper('check-busy', '') <> 0 then
        begin
          AddSummary('[warn] HidHide -- left installed: another installer is running. Remove it later from Settings > Apps');
          RemoveHidHide := False;
        end
        else
          RunHelper('remove-hidhide', '');
      end;
      if RemoveUsbip then
      begin
        UninstallProgressForm.StatusLabel.Caption := 'Uninstalling usbip-win2 (USB 3.0 devices will blink out and come back) ...';
        if RunHelper('check-busy', '') <> 0 then
        begin
          AddSummary('[warn] usbip-win2 -- left installed: another installer is running. Remove it later from Settings > Apps (USBip)');
          RemoveUsbip := False;
        end
        else
        begin
          RC := RunHelper('remove-usbip', '');
          // 3: the driver was loaded, so the helper only disabled it and
          // scheduled the removal proper for the next logon (usbip2_ude
          // 0.9.7.7 hangs Windows if unloaded while running -- see the
          // helper). Verify the rest without expecting usbip gone.
          if RC = 3 then UsbipPendingReboot := True;
        end;
      end;
      UninstallProgressForm.StatusLabel.Caption := 'Verifying ...';
      Expect := '';
      if UsbipPendingReboot then Expect := Expect + ' -UsbipPendingReboot'
      else if RemoveUsbip then Expect := Expect + ' -ExpectNoUsbip';
      if RemoveHidHide then Expect := Expect + ' -ExpectNoHidHide';
      RunHelper('verify-removed', Expect);
    end;
  end
  else if CurUninstallStep = usPostUninstall then
  begin
    // The origin records: gone for a driver that was removed, kept for one
    // that stays (so a later reinstall still knows whose it is).
    if RemoveUsbip then RegDeleteValue(HKLM, SetupKey, 'UsbipInstalledByUs');
    if RemoveHidHide then RegDeleteValue(HKLM, SetupKey, 'HidHideInstalledByUs');
    RegDeleteKeyIfEmpty(HKLM, SetupKey);
    RegDeleteKeyIfEmpty(HKLM, 'SOFTWARE\ds5bridge');
    // Belt to the helper's braces: a pre-0.5.0 Run value pointing at this
    // install (the helper may have been missing).
    if RegQueryStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'ds5bridge', RunValue) then
      if Pos(Lowercase(ExpandConstant('{app}')), Lowercase(RunValue)) > 0 then
      begin
        RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'ds5bridge');
        AddSummary('[ok] start-at-login -- entry removed');
      end;
    // Everything under {app} is ours (the app, staged updates, this helper);
    // settings live in %APPDATA% and go only when asked.
    DelTree(ExpandConstant('{app}'), True, True, True);
    if DirExists(ExpandConstant('{app}\app')) then
    begin
      AddSummary('[FAIL] app -- ' + ExpandConstant('{app}\app') + ' could not be removed completely');
      FailCount := FailCount + 1;
    end
    else
      AddSummary('[ok] app -- ' + ExpandConstant('{app}') + ' removed');
    if PurgeSettings then
    begin
      DelTree(ExpandConstant('{userappdata}\ds5bridge'), True, True, True);
      if DirExists(ExpandConstant('{userappdata}\ds5bridge')) then
        AddSummary('[warn] settings -- ' + ExpandConstant('{userappdata}\ds5bridge') + ' could not be removed completely')
      else
        AddSummary('[ok] settings -- ' + ExpandConstant('{userappdata}\ds5bridge') + ' removed');
      // The service's settings. Only ours: the directory also holds the
      // usbip-win2 removal log/task copy and the resume copy of this setup.
      DeleteFile(ExpandConstant('{commonappdata}\ds5bridge\config.json'));
      DeleteFile(ExpandConstant('{commonappdata}\ds5bridge\config.json.bad'));
      DeleteFile(ExpandConstant('{commonappdata}\ds5bridge\update-cache.json'));
      DelTree(ExpandConstant('{commonappdata}\ds5bridge\hidden'), True, True, True);
      DelTree(ExpandConstant('{commonappdata}\ds5bridge\logs'), True, True, True);
      if FileExists(ExpandConstant('{commonappdata}\ds5bridge\config.json')) then
        AddSummary('[warn] service settings -- ' + ExpandConstant('{commonappdata}\ds5bridge\config.json') + ' could not be removed')
      else
        AddSummary('[ok] service settings -- ' + ExpandConstant('{commonappdata}\ds5bridge') + ' cleared');
    end
    else
    begin
      if DirExists(ExpandConstant('{userappdata}\ds5bridge')) then
        AddSummary('[info] settings -- kept at ' + ExpandConstant('{userappdata}\ds5bridge') + ' (run with /PURGESETTINGS=1 or tick the box to remove them)');
      if FileExists(ExpandConstant('{commonappdata}\ds5bridge\config.json')) then
        AddSummary('[info] service settings -- kept at ' + ExpandConstant('{commonappdata}\ds5bridge') + ' (same switch/box removes them)');
    end;

    OutFile := ExpandConstant('{%TEMP}\ds5bridge-uninstall-check.txt');
    Summary.SaveToFile(OutFile);
    Log('summary written to ' + OutFile);
    if not UninstallSilent then
      ShowUninstallSummary;
  end;
end;
