#ifndef AppVersion
  #define AppVersion "3.16"
#endif

#define AppName "曜衡"
#define AppExeName "曜衡.exe"
#define AppPublisher "alokxfox"
#define ProjectUrl "https://github.com/alokxfox/yaoheng"

[Setup]
AppId={{49A035BF-7BEC-4FE1-84C4-EEBFD503A917}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#ProjectUrl}
AppSupportURL={#ProjectUrl}/issues
AppUpdatesURL={#ProjectUrl}/releases/latest
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
AllowNoIcons=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\release
OutputBaseFilename=Yaoheng-{#AppVersion}-Windows-x64-Setup
SetupIconFile=..\app.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName} {#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
VersionInfoVersion={#AppVersion}.0.0
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} Windows 安装程序
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}
VersionInfoCopyright=Copyright (C) 2026 {#AppPublisher}
MissingMessagesWarning=yes
NotRecognizedMessagesWarning=yes

[Languages]
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\曜衡\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent

[CustomMessages]
chinesesimplified.RemoveUserDataPrompt=是否同时删除曜衡应用目录内的本机设置、计算历史与行情缓存？%n%n选择“否”可保留这些数据，方便以后重新安装。自定义数据目录不会由卸载程序删除。
english.RemoveUserDataPrompt=Also remove local settings, calculation history, and market cache stored inside the application directory?%n%nChoose No to preserve this data for a future installation. The uninstaller never deletes a custom data directory.

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    if SuppressibleMsgBox(
      ExpandConstant('{cm:RemoveUserDataPrompt}'),
      mbConfirmation,
      MB_YESNO,
      IDNO) = IDYES then
    begin
      DeleteFile(ExpandConstant('{app}\app_settings.json'));
      DeleteFile(ExpandConstant('{app}\app_settings.json.bak'));
      DelTree(ExpandConstant('{app}\data'), True, True, True);
    end;
  end;
end;
