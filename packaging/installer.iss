#define AppVersion GetEnv("BOARDVIEWER_APP_VERSION")
#define NumericVersion GetEnv("BOARDVIEWER_NUMERIC_VERSION")
#define PayloadDir GetEnv("BOARDVIEWER_PAYLOAD_DIR")
#define InstallerOutputDir GetEnv("BOARDVIEWER_INSTALLER_OUTPUT_DIR")
#define InstallerBaseName GetEnv("BOARDVIEWER_INSTALLER_BASENAME")
#define SetupIcon GetEnv("BOARDVIEWER_ICON_PATH")

#if AppVersion == ""
  #error BOARDVIEWER_APP_VERSION is required
#endif
#if PayloadDir == ""
  #error BOARDVIEWER_PAYLOAD_DIR is required
#endif
#if NumericVersion == ""
  #error BOARDVIEWER_NUMERIC_VERSION is required
#endif
#if InstallerOutputDir == ""
  #error BOARDVIEWER_INSTALLER_OUTPUT_DIR is required
#endif
#if InstallerBaseName == ""
  #error BOARDVIEWER_INSTALLER_BASENAME is required
#endif
#if SetupIcon == ""
  #error BOARDVIEWER_ICON_PATH is required
#endif

[Setup]
AppId={{A7A2E492-C1EF-47AD-A634-84454A32A2A1}
AppName=Thermetery Boardviewer
AppVersion={#AppVersion}
AppPublisher=Thermetery Technology LLC
AppPublisherURL=https://github.com/jiacheng-thermetery/thermetery-boardview
AppSupportURL=https://github.com/jiacheng-thermetery/thermetery-boardview/issues
AppUpdatesURL=https://github.com/jiacheng-thermetery/thermetery-boardview/releases
VersionInfoVersion={#NumericVersion}
VersionInfoCompany=Thermetery Technology LLC
VersionInfoDescription=Thermetery Boardviewer installer
VersionInfoProductName=Thermetery Boardviewer
DefaultDirName={localappdata}\Programs\Thermetery Boardviewer
DefaultGroupName=Thermetery Boardviewer
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#InstallerOutputDir}
OutputBaseFilename={#InstallerBaseName}
SetupIconFile={#SetupIcon}
LicenseFile={#PayloadDir}\licenses\LICENSE
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern dynamic
SetupLogging=yes
UninstallDisplayIcon={app}\ThermeteryBoardviewer.exe
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Thermetery Boardviewer"; Filename: "{app}\ThermeteryBoardviewer.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\Thermetery Boardviewer"; Filename: "{app}\ThermeteryBoardviewer.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\ThermeteryBoardviewer.exe"; Description: "Launch Thermetery Boardviewer"; Flags: nowait postinstall skipifsilent
