# Smoke-tests dist\localcode-*-windows-x64.zip and -setup.exe the way a user with no Python would run them.
# The installer test edits the current user's PATH (and restores it); that is the point of the test.
$ErrorActionPreference = 'Stop'
Set-Location "$PSScriptRoot\.."

$version = (Select-String -Path localcode.py -Pattern '^__version__ = "(.+)"').Matches[0].Groups[1].Value
$zip = "dist\localcode-$version-windows-x64.zip"
$setup = "dist\localcode-$version-windows-x64-setup.exe"
$work = Join-Path ([IO.Path]::GetTempPath()) ("localcode-smoke-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
$cwd = New-Item -ItemType Directory -Force (Join-Path $work 'cwd with space')
$failures = 0

function Check($ok, $what) {
    if ($ok) { "ok   $what" } else { "FAIL $what"; $script:failures++ }
}

# Run an exe with only the Windows folders on PATH and junk Python variables, from the spaced directory.
function Run($exe, $argList, $inputText = '') {
    $oldPath, $oldHome, $oldPp = $env:PATH, $env:PYTHONHOME, $env:PYTHONPATH
    $env:PATH = "$env:WINDIR\System32;$env:WINDIR"; $env:PYTHONHOME = 'C:\nonexistent'; $env:PYTHONPATH = 'C:\nonexistent'
    try {
        $psi = New-Object Diagnostics.ProcessStartInfo $exe, $argList
        $psi.UseShellExecute = $false; $psi.RedirectStandardInput = $true; $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true
        $psi.WorkingDirectory = $cwd.FullName  # Push-Location only moves PowerShell's location, not the process's
        $p = [Diagnostics.Process]::Start($psi)
        $p.StandardInput.Write($inputText); $p.StandardInput.Close()
        $out = $p.StandardOutput.ReadToEndAsync(); $err = $p.StandardError.ReadToEndAsync()
        # Windows takes a few seconds to report a refused connection and a loaded machine can take far longer
        if (-not $p.WaitForExit(90000)) { $p.Kill(); throw "$exe timed out" }
        [pscustomobject]@{ Code = $p.ExitCode; Text = $out.Result + $err.Result }
    } finally {
        $env:PATH, $env:PYTHONHOME, $env:PYTHONPATH = $oldPath, $oldHome, $oldPp
    }
}

function CheckLauncher($lc) {
    $r = Run $lc '--version'
    Check ($r.Code -eq 0 -and $r.Text.Trim() -eq "localcode $version") "--version prints 'localcode $version'"
    $r = Run $lc '--help'
    Check ($r.Code -eq 0 -and $r.Text -match '--max-prompt') '--help lists the flags'
    $r = Run $lc '--bogus'
    Check ($r.Code -eq 2 -and $r.Text -match 'unrecognized arguments') 'argparse exit code 2 passes through'
    $r = Run $lc '--ctx 100 --max-tokens 200 --model x'
    Check ($r.Code -eq 1 -and $r.Text -match 'max-tokens must be smaller than ctx') 'arguments reach localcode; exit code 1 passes through'
    $r = Run $lc '--model "my model \"quoted\"" --url http://127.0.0.1:9/v1' "hi`n/exit`n"
    Check ($r.Code -eq 0 -and $r.Text.Contains('my model "quoted"')) 'an argument with spaces and quotes arrives intact'
    Check ($r.Text -match 'request failed') 'with no server running, a prompt reports the failure instead of crashing'
    $r = Run $lc '--url http://127.0.0.1:9/v1'
    Check ($r.Code -eq 1 -and $r.Text -match 'cannot list models') 'with no server and no --model, it exits 1 with a message'
}

try {
    "== zip: $zip"
    tar.exe -xf $zip -C $work
    if ($LASTEXITCODE) { throw 'could not extract the zip' }
    CheckLauncher (Join-Path $work 'localcode\localcode.exe')

    if (Test-Path $setup) {
        "== installer: $setup"
        $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment')
        $before = $key.GetValue('Path', $null, 'DoNotExpandEnvironmentNames')
        $kind = if ($null -ne $before) { $key.GetValueKind('Path') } else { $null }
        $key.Close()
        $dir = Join-Path $work 'installed'
        try {
            $p = Start-Process $setup "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /DIR=`"$dir`"" -Wait -PassThru
            Check ($p.ExitCode -eq 0 -and (Test-Path "$dir\localcode.exe")) 'silent install copies the files'
            $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment')
            $during = $key.GetValue('Path', $null, 'DoNotExpandEnvironmentNames'); $key.Close()
            Check (($during -split ';') -contains $dir) 'the install folder was added to the user PATH'
            CheckLauncher "$dir\localcode.exe"
        } finally {
            if (Test-Path "$dir\unins000.exe") { Start-Process "$dir\unins000.exe" '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART' -Wait }
            # The uninstaller carries on in a copy of itself after the process we waited for exits: it deletes the files,
            # then edits PATH, and removes unins000.exe last. So wait for that file to go.
            for ($i = 0; $i -lt 120 -and (Test-Path "$dir\unins000.exe"); $i++) { Start-Sleep -Milliseconds 500 }
        }
        $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment')
        $after = $key.GetValue('Path', $null, 'DoNotExpandEnvironmentNames')
        $afterKind = if ($null -ne $after) { $key.GetValueKind('Path') } else { $null }; $key.Close()
        Check ($after -ceq $before) 'uninstall restores the user PATH exactly'
        if ($after -cne $before) { "     before: [$before] ($kind)`n     during: [$during]`n     after:  [$after] ($afterKind)" }
        if ($after -ceq $before -and $null -ne $before) { Check ($afterKind -eq $kind) "the PATH value type is unchanged ($kind)" }
        Check (-not (Test-Path "$dir\localcode.exe")) 'uninstall removes the files'
    }
} finally {
    Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
}
if ($failures) { throw "$failures smoke check(s) failed" }
'smoke tests passed'
