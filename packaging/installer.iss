; OrcMesh — Inno Setup installer script.
;
; Build with (after scripts\build.ps1 has produced dist\OrcMesh\):
;   & "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" packaging\installer.iss
; (or wherever ISCC.exe landed for your Inno Setup install — winget installs
; per-user by default, not to Program Files.)
;
; Produces dist\OrcMesh-Setup-<version>.exe: a single installer with a
; Start Menu shortcut, optional desktop icon, and a standard uninstaller
; registered in Windows' "Apps & features". No code signing — OrcMesh
; isn't signed yet (see ROADMAP.md), so Windows SmartScreen will show an
; "unrecognized publisher" warning on first run until it builds up
; enough download reputation.
;
; MyAppVersion must be bumped in lockstep with pyproject.toml's
; [project] version — there's no automated single-sourcing between them
; (pyproject.toml drives what gets baked into the exe via
; importlib.metadata; this drives the installer filename/registry entry).

#define MyAppName "OrcMesh"
#define MyAppVersion "0.2.0"
#define MyAppPublisher "hardcoreerik"
#define MyAppURL "https://github.com/hardcoreerik/OrcMesh"
#define MyAppExeName "OrcMesh.exe"

[Setup]
AppId={{6E9C2C6D-6B7B-4F2E-9C2C-6D6B7B4F2E9C}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
; Portable-friendly: installs per-machine by default (autopf = Program
; Files) but doesn't require an admin-elevated install directory choice.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=OrcMesh-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; Everything PyInstaller collected — the exe, its _internal/ dependency
; tree, and the QtWebEngineProcess.exe/icudtl.dat build.ps1 copies in
; alongside it.
Source: "..\dist\OrcMesh\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; OrcMesh retains its pre-rebrand data under %LOCALAPPDATA%\MeshChat (see
; platformdirs usage in app.py) — deliberately NOT removed here. An
; uninstall shouldn't silently delete a user's chat history/node
; database; that's what "Reset" inside the app (once it exists) or a
; manual folder delete is for.
