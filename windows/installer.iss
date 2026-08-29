#define AppName "JAV Search"
#ifndef AppVersion
  #define AppVersion "1.4.6.23"
#endif

[Setup]
AppId={{90E23E8B-B4E1-4FBA-B22B-B0CE07182713}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={autopf}\JAV Search
DefaultGroupName=JAV Search
OutputDir=..\dist\installer
OutputBaseFilename=JAV-Search-v{#AppVersion}-Windows-x64-Setup
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
WizardStyle=modern
UninstallDisplayIcon={app}\JAV Search.exe

[Files]
Source: "..\dist\JAV Search\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\JAV Search"; Filename: "{app}\JAV Search.exe"
Name: "{autodesktop}\JAV Search"; Filename: "{app}\JAV Search.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加快捷方式："
Name: "startup"; Description: "登录 Windows 后自动启动"; GroupDescription: "启动选项："

[Registry]
Root: HKA; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "JAV Search"; ValueData: """{app}\JAV Search.exe"""; Flags: uninsdeletevalue; Tasks: startup

[Run]
Filename: "{app}\JAV Search.exe"; Description: "启动 JAV Search"; Flags: nowait postinstall skipifsilent

