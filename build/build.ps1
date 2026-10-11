# Builds dist\localcode-<version>-windows-x64.zip and the matching -setup.exe:
# launcher + the unmodified python.org embeddable Python + localcode.py. Nothing is downloaded when localcode runs.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Set-Location "$PSScriptRoot\.."

$version = (Select-String -Path localcode.py -Pattern '^__version__ = "(.+)"').Matches[0].Groups[1].Value
$pyVersion = '3.11.9'
$pySha256 = '009d6bf7e3b2ddca3d784fa09f90fe54336d5b60f0e0f305c37f400bf83cfd3b'
$name = "localcode-$version-windows-x64"

New-Item -ItemType Directory -Force build\cache, dist | Out-Null
$embed = "build\cache\python-$pyVersion-embed-amd64.zip"
if (-not (Test-Path $embed)) {
    Invoke-WebRequest -UseBasicParsing "https://www.python.org/ftp/python/$pyVersion/python-$pyVersion-embed-amd64.zip" -OutFile $embed
}
$actual = (Get-FileHash $embed -Algorithm SHA256).Hash.ToLower()
if ($actual -ne $pySha256) { throw "$embed has SHA256 $actual, expected $pySha256" }

$stage = "build\stage\localcode"
Remove-Item -Recurse -Force build\stage -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force "$stage\runtime" | Out-Null
Expand-Archive $embed "$stage\runtime"

$csc = "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
& $csc /nologo /target:exe /optimize+ /warnaserror+ "/out:$stage\localcode.exe" launcher\launcher.cs
if ($LASTEXITCODE) { throw "csc failed with exit code $LASTEXITCODE" }
Copy-Item localcode.py, LICENSE, README.md $stage

# bsdtar writes a zip with forward-slash entry names; Windows PowerShell 5.1's Compress-Archive does not
Remove-Item "dist\$name.zip" -ErrorAction SilentlyContinue
tar.exe -a -c -f "dist\$name.zip" -C build\stage localcode
if ($LASTEXITCODE) { throw "tar failed with exit code $LASTEXITCODE" }

$iscc = (Get-Command iscc -ErrorAction SilentlyContinue).Source
if (-not $iscc) {
    $iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe") |
        Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $iscc) { throw 'Inno Setup 6 (ISCC.exe) not found; install it from https://jrsoftware.org/isinfo.php' }
& $iscc /Qp "/DVersion=$version" build\localcode.iss
if ($LASTEXITCODE) { throw "ISCC failed with exit code $LASTEXITCODE" }

Get-ChildItem "dist\$name*" | ForEach-Object { '{0}  {1:N1} MB' -f $_.Name, ($_.Length / 1MB) }
