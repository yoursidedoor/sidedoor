; Sidedoor's Windows installer (owner, WI1 a: Inno Setup, per user). Compiled by tools/build_windows.py:
;   ISCC.exe /DAppVersion=<N.N.N> /DSourceDir=<the built Sidedoor folder> /DOutputDir=<output>
;            /DIconFile=<sidedoor.ico>
;            /DOutputName=Sidedoor-windows-x64-setup [/DSign /Ssidedoor=<signtool command>] sidedoor.iss
; No admin prompt: installs for the current user only, into %LOCALAPPDATA%\Programs\Sidedoor, with a
; Start-menu entry and an uninstaller. It sets up no start at login (owner S3: off by default; the
; app's own setting turns it on). The uninstaller removes only the files it installed: the user's
; Sidedoor home (~\.config\sidedoor) and everything Sidedoor saved there stay.

#ifndef AppVersion
  #error AppVersion is required
#endif
#ifndef SourceDir
  #error SourceDir is required
#endif
#ifndef IconFile
  #error IconFile is required
#endif
#ifndef OutputDir
  #error OutputDir is required
#endif
#ifndef OutputName
  #define OutputName "Sidedoor-windows-x64-setup"
#endif

[Setup]
AppId={{6F0B3C2E-5D7A-4E8B-9C1F-2A4D6E8F0B13}
AppName=Sidedoor
AppVersion={#AppVersion}
AppVerName=Sidedoor {#AppVersion}
AppPublisher=Sidedoor
VersionInfoVersion={#AppVersion}.0
PrivilegesRequired=lowest
DefaultDirName={userpf}\Sidedoor
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename={#OutputName}
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\sidedoor.exe
UninstallDisplayName=Sidedoor
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
#ifdef Sign
SignTool=sidedoor
SignedUninstaller=yes
#endif

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\Sidedoor"; Filename: "{app}\sidedoor.exe"; WorkingDir: "{app}"

[Run]
Filename: "{app}\sidedoor.exe"; Description: "Open Sidedoor"; Flags: nowait postinstall skipifsilent
