; Per-user installer: copies the folder built by build.ps1 and adds it to the user's PATH (removed again on uninstall).
#ifndef Version
  #error Pass the version with /DVersion=x.y.z
#endif

[Setup]
AppId={{6C0B6B64-6E0B-4B8E-9F3E-5C1D2E7A4F10}
AppName=localcode
AppVersion={#Version}
AppPublisher=swaft
AppPublisherURL=https://github.com/swaftmc/localcode
DefaultDirName={autopf}\localcode
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
ChangesEnvironment=yes
LicenseFile=..\LICENSE
OutputDir=..\dist
OutputBaseFilename=localcode-{#Version}-windows-x64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Files]
Source: "stage\localcode\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Code]
const
  EnvKey = 'Environment';

// Splits the PATH value into ';Dir;' form so a directory can be found without matching part of another one.
function Wrap(P: string): string;
begin
  Result := ';' + P + ';';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  P: string;
begin
  if CurStep <> ssPostInstall then Exit;
  if not RegQueryStringValue(HKCU, EnvKey, 'Path', P) then P := '';
  if Pos(Wrap(Uppercase(ExpandConstant('{app}'))), Uppercase(Wrap(P))) > 0 then Exit;
  if (P <> '') and (P[Length(P)] <> ';') then P := P + ';';
  RegWriteExpandStringValue(HKCU, EnvKey, 'Path', P + ExpandConstant('{app}'));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  P, S, Dir: string;
  I: Integer;
begin
  if CurUninstallStep <> usPostUninstall then Exit;
  if not RegQueryStringValue(HKCU, EnvKey, 'Path', P) then Exit;
  Dir := ExpandConstant('{app}');
  S := Wrap(P);
  I := Pos(Wrap(Uppercase(Dir)), Uppercase(S));
  if I = 0 then Exit;
  Delete(S, I, Length(Dir) + 1); // drops ';Dir', keeping the ';' that follows it
  if Length(S) > 2 then P := Copy(S, 2, Length(S) - 2) else P := '';
  if P = '' then
    RegDeleteValue(HKCU, EnvKey, 'Path') // the install created the value, so remove it rather than leave it empty
  else
    RegWriteExpandStringValue(HKCU, EnvKey, 'Path', P);
end;
