; Установщик Пульта МЭШ для Windows (Inno Setup 6).
; Ставится в профиль пользователя — права администратора не нужны.
; Сборка: ISCC /DAppVer=1.1.0 cross-platform\installer.iss  (после pyinstaller → dist\MeshPult)
#ifndef AppVer
  #define AppVer "0.0.0"
#endif

[Setup]
AppId={{8F2C5B1E-3D4A-4C6B-9E7F-1A2B3C4D5E6F}
AppName=Пульт МЭШ
AppVersion={#AppVer}
AppPublisher=MeshPult (неофициальная утилита)
AppPublisherURL=https://github.com/myratmyradov1997/mesh-pult
DefaultDirName={localappdata}\Programs\MeshPult
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=MeshPult-Windows-Setup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\MeshPult.exe
UninstallDisplayName=Пульт МЭШ
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=force

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Ярлык на рабочем столе"; GroupDescription: "Дополнительно:"
Name: "autostart"; Description: "Запускать при входе в Windows"; GroupDescription: "Дополнительно:"; Flags: unchecked

[Files]
Source: "..\dist\MeshPult\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; старые файлы предыдущей версии (у PyInstaller меняется состав _internal)
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{userprograms}\Пульт МЭШ"; Filename: "{app}\MeshPult.exe"
Name: "{userdesktop}\Пульт МЭШ"; Filename: "{app}\MeshPult.exe"; Tasks: desktopicon
Name: "{userstartup}\Пульт МЭШ"; Filename: "{app}\MeshPult.exe"; Tasks: autostart

[Run]
Filename: "{app}\MeshPult.exe"; Description: "Запустить Пульт МЭШ"; Flags: nowait postinstall skipifsilent
