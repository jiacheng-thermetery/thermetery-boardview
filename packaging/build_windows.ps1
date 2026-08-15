[CmdletBinding()]
param(
    [string]$Version = "",
    [string]$OutputDir = "",
    [switch]$Clean,
    [switch]$SkipTests,
    [switch]$SkipInstaller,
    [switch]$NoBootstrap
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$repoHashBytes = [Security.Cryptography.SHA256]::Create().ComputeHash(
    [Text.Encoding]::UTF8.GetBytes($RepoRoot.ToLowerInvariant())
)
$repoHash = ([BitConverter]::ToString($repoHashBytes) -replace '-', '').Substring(0, 10)
$workBase = $env:TEMP
if ([string]::IsNullOrWhiteSpace($workBase)) {
    $workBase = Join-Path $RepoRoot ".release-work"
}
# Keep the frozen/staging tree short: license and Python package paths can
# otherwise exceed MAX_PATH when the repository itself has a deep checkout.
$WorkRoot = Join-Path $workBase "thermetery-boardviewer-release-$repoHash"
$VenvRoot = Join-Path $RepoRoot ".release-venv"
$ToolsRoot = Join-Path $RepoRoot ".release-tools"
if ([string]::IsNullOrWhiteSpace($OutputDir)) {
    $OutputDir = Join-Path $RepoRoot "release"
}
$OutputDir = [IO.Path]::GetFullPath($OutputDir)

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @()
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE`: $FilePath $($Arguments -join ' ')"
    }
}

function Remove-SafeDirectory([string]$Path, [string]$Label) {
    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    $driveRoot = [IO.Path]::GetPathRoot($full).TrimEnd('\', '/')
    $repo = $RepoRoot.TrimEnd('\', '/')
    $repoParent = (Split-Path -Parent $RepoRoot).TrimEnd('\', '/')
    if ($full.Length -lt 8 -or $full -eq $driveRoot -or
        $full -eq $repo -or $full -eq $repoParent) {
        throw "Refusing to recursively remove unsafe $Label path: $full"
    }
    Write-Host "Removing stale $Label`: $full"
    Remove-Item -LiteralPath $full -Recurse -Force
}

function Clear-ReleaseOutput([string]$Path, [string]$ReleaseVersion) {
    # OutputDir is user-selectable and may contain unrelated files. Never
    # recursively delete it; remove only the exact artifacts this invocation
    # owns.
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        return
    }
    $ownedNames = @(
        "ThermeteryBoardviewer-$ReleaseVersion-windows-x64-portable.zip",
        "ThermeteryBoardviewer-$ReleaseVersion-windows-x64-setup.exe",
        "SHA256SUMS.txt"
    )
    foreach ($name in $ownedNames) {
        Remove-Item -LiteralPath (Join-Path $Path $name) -Force -ErrorAction SilentlyContinue
    }
}

function Refresh-ProcessPath {
    $paths = @(
        $env:Path,
        [Environment]::GetEnvironmentVariable("Path", "Machine"),
        [Environment]::GetEnvironmentVariable("Path", "User")
    )
    $seen = @{}
    $merged = foreach ($entry in ($paths -split ';')) {
        $trimmed = $entry.Trim()
        if ($trimmed -and -not $seen.ContainsKey($trimmed.ToLowerInvariant())) {
            $seen[$trimmed.ToLowerInvariant()] = $true
            $trimmed
        }
    }
    $env:Path = $merged -join ';'
}

function Test-BuildPython([string]$Candidate) {
    if (-not $Candidate -or -not (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
        return $false
    }
    $probe = & $Candidate -c "import struct,sys; print('ok' if struct.calcsize('P') == 8 and sys.version_info >= (3, 12) else 'no')" 2>$null
    return $LASTEXITCODE -eq 0 -and ($probe | Select-Object -Last 1) -eq "ok"
}

function Resolve-BuildPython {
    if ($env:BOARDVIEWER_BUILD_PYTHON -and
        (Test-BuildPython $env:BOARDVIEWER_BUILD_PYTHON)) {
        return [IO.Path]::GetFullPath($env:BOARDVIEWER_BUILD_PYTHON)
    }

    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        foreach ($selector in @("-3.14", "-3.13", "-3.12")) {
            $candidate = & $launcher.Source $selector -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0) {
                $candidate = $candidate | Select-Object -Last 1
                if (Test-BuildPython $candidate) {
                    return [IO.Path]::GetFullPath($candidate)
                }
            }
        }
    }

    foreach ($name in @("python.exe", "python3.exe")) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command -and (Test-BuildPython $command.Source)) {
            return [IO.Path]::GetFullPath($command.Source)
        }
    }
    throw "A 64-bit Python 3.12 or newer installation is required to build the release."
}

function Add-DiscoveredCompilerPath {
    if ((Get-Command gcc.exe -ErrorAction SilentlyContinue) -or
        (Get-Command cl.exe -ErrorAction SilentlyContinue)) {
        return
    }

    $candidateBins = @(
        "C:\msys64\ucrt64\bin",
        "C:\mingw64\bin"
    )
    if (Test-Path -LiteralPath "C:\Program Files") {
        $candidateBins += Get-ChildItem -LiteralPath "C:\Program Files" -Directory -Filter "gcc-*mingw*" -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending |
            ForEach-Object { Join-Path $_.FullName "bin" }
    }
    foreach ($bin in $candidateBins) {
        if (Test-Path -LiteralPath (Join-Path $bin "gcc.exe") -PathType Leaf) {
            $env:Path = "$bin;$env:Path"
            return
        }
    }
}

function Assert-NativeCompiler {
    $gcc = Get-Command gcc.exe -ErrorAction SilentlyContinue
    $cl = Get-Command cl.exe -ErrorAction SilentlyContinue
    if (-not $gcc -and -not $cl) {
        throw "No x64 C compiler was found. Install MinGW GCC or run from an x64 Visual Studio developer shell."
    }
    if ($gcc) {
        $target = & $gcc.Source -dumpmachine
        if ($LASTEXITCODE -ne 0 -or $target -notmatch '^(x86_64|amd64)') {
            throw "GCC does not target x64 Windows: $target"
        }
        Write-Host "Native compiler: $($gcc.Source)"
    } else {
        if ($env:VSCMD_ARG_TGT_ARCH -and $env:VSCMD_ARG_TGT_ARCH -ne "x64") {
            throw "The Visual Studio developer shell targets $env:VSCMD_ARG_TGT_ARCH, not x64."
        }
        Write-Host "Native compiler: $($cl.Source)"
    }
}

function New-PortableArchive {
    <#
        Write the portable ZIP deterministically, whichever PowerShell runs
        this script.

        Two reasons this is not Compress-Archive:

        1. Entry separators. Windows PowerShell 5.1's Compress-Archive AND
           .NET Framework's ZipFile.CreateFromDirectory both write BACKSLASH
           separators, which the ZIP spec (APPNOTE 4.4.17) forbids. Explorer
           tolerates them; strict readers (Python's zipfile, most non-Windows
           tools) do not - they treat the backslash as part of the filename
           and produce one flat directory of files literally called
           "_internal\python314.dll". CI runs pwsh, whose .NET 5+ writes
           forward slashes, so only local builds were affected - and
           build_windows.cmd invokes powershell.exe. Building the entries by
           hand makes both editions agree.

        2. Layout. The archive holds the payload CONTENTS, not the staging
           folder. Explorer's "Extract All" already creates a folder named
           after the ZIP, so an inner ThermeteryBoardviewer\ folder produced
           "...-portable\ThermeteryBoardviewer\" - the redundant nesting that
           invites users to flatten the tree by hand and strand the .exe
           without _internal beside it.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$SourceDir,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    Add-Type -AssemblyName System.IO.Compression | Out-Null
    Add-Type -AssemblyName System.IO.Compression.FileSystem | Out-Null

    $root = [IO.Path]::GetFullPath($SourceDir).TrimEnd([char]92)
    $archive = [IO.Compression.ZipFile]::Open($Destination, "Create")
    try {
        $files = Get-ChildItem -LiteralPath $root -Recurse -File -Force |
            Sort-Object FullName
        foreach ($file in $files) {
            $relative = $file.FullName.Substring($root.Length + 1).Replace(
                [char]92, [char]47)
            [void][IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                $archive, $file.FullName, $relative,
                [IO.Compression.CompressionLevel]::Optimal)
        }
    } finally {
        $archive.Dispose()
    }
}

function Assert-PortableRuntime([string]$PayloadDir) {
    # The PyInstaller bootloader dies before a single line of Python runs if
    # any of these is absent, and it reports only "Failed to load Python DLL
    # ... The specified module could not be found" - so the frozen self-test
    # can never catch it. A file manifest is the only automated guard.
    $runtime = @(
        "ThermeteryBoardviewer.exe",
        "_internal\python314.dll",
        "_internal\VCRUNTIME140.dll",
        "_internal\base_library.zip",
        "_internal\src\parsers\native\tvw_native.dll",
        "_internal\src\parsers\native\xzz_native.dll",
        "_internal\src\parsers\native\rc6_native.dll"
    )
    foreach ($relative in $runtime) {
        $path = Join-Path $PayloadDir $relative
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Frozen runtime is incomplete: $path"
        }
    }
}

function Test-LtoSupported {
    # `-Db_lto=true` is a hard failure, not a downgrade, on a GCC built
    # --disable-lto: cc1 reports "LTO support has not been enabled in this
    # configuration" and the whole release build stops. w64devkit's GCC 16.1
    # is exactly such a toolchain. The measured win on these three
    # single-file libraries is under 2%, so LTO is worth taking when it is
    # there and never worth failing the build over.
    #
    # Only GCC is probed: meson's b_lto is not among cl.exe's base options,
    # so on MSVC the flag is accepted and ignored.
    $gcc = Get-Command gcc.exe -ErrorAction SilentlyContinue
    if (-not $gcc) {
        return $true
    }
    $probeDir = Join-Path $WorkRoot "lto-probe"
    New-Item -ItemType Directory -Path $probeDir -Force | Out-Null
    try {
        $probeSource = Join-Path $probeDir "probe.c"
        Set-Content -LiteralPath $probeSource -Encoding ascii `
            -Value "int probe(void) { return 0; }"
        # Start-Process rather than the call operator, because this probe is
        # MEANT to fail sometimes. Windows PowerShell wraps a native
        # command's stderr in a NativeCommandError, and under this script's
        # $ErrorActionPreference = "Stop" that terminates the build -- the
        # very failure this function exists to avoid. Redirection operators
        # (2>&1, *>) do not help; they are what triggers the wrapping.
        # Start-Process routes the child's streams to files without
        # involving PowerShell's error stream at all.
        $probe = Start-Process -FilePath $gcc.Source -PassThru -Wait -NoNewWindow `
            -ArgumentList @("-flto", "-c",
                            "-o", (Join-Path $probeDir "probe.o"),
                            $probeSource) `
            -RedirectStandardOutput (Join-Path $probeDir "probe.out") `
            -RedirectStandardError (Join-Path $probeDir "probe.err")
        return ($probe.ExitCode -eq 0)
    } catch {
        # An unusable probe is not evidence that LTO works.
        return $false
    } finally {
        Remove-Item -LiteralPath $probeDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Assert-ReleasePayload([string]$PayloadDir) {
    Assert-PortableRuntime $PayloadDir
    $required = @(
        (Join-Path $PayloadDir "ThermeteryBoardviewer.exe"),
        (Join-Path $PayloadDir "_internal\src\parsers\native\tvw_native.dll"),
        (Join-Path $PayloadDir "_internal\src\parsers\native\xzz_native.dll"),
        (Join-Path $PayloadDir "_internal\src\parsers\native\rc6_native.dll"),
        (Join-Path $PayloadDir "licenses\LICENSE")
    )
    foreach ($path in $required) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Frozen payload is incomplete: $path"
        }
    }
    if (Test-Path -LiteralPath (Join-Path $PayloadDir "portable.flag")) {
        throw "The canonical installer payload unexpectedly contains portable.flag."
    }

    $forbiddenNames = @("fz_key.txt", "xzz_key.txt", "xzz_key")
    $forbiddenExtensions = @(".cad", ".brd", ".brd2", ".bv", ".tvw", ".fz", ".pcb")
    $bad = Get-ChildItem -LiteralPath $PayloadDir -Recurse -Force | Where-Object {
        ($_.PSIsContainer -and $_.Name -ieq "private") -or
        (-not $_.PSIsContainer -and $forbiddenNames -contains $_.Name.ToLowerInvariant()) -or
        (-not $_.PSIsContainer -and $forbiddenExtensions -contains $_.Extension.ToLowerInvariant())
    }
    if ($bad) {
        $listed = ($bad | ForEach-Object FullName) -join [Environment]::NewLine
        throw "Forbidden private keys or board files entered the release payload:`n$listed"
    }
}

function Invoke-FrozenSelfTest {
    param(
        [Parameter(Mandatory = $true)][string]$PayloadDir,
        [Parameter(Mandatory = $true)][bool]$ExpectedPortable,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $unicodeSuffix = "$([char]0x6D4B)$([char]0x8BD5)"
    $testRoot = Join-Path $WorkRoot "smoke tests\$Label - spaces - $unicodeSuffix"
    Remove-SafeDirectory $testRoot "smoke-test"
    New-Item -ItemType Directory -Path $testRoot -Force | Out-Null
    $diagnostics = Join-Path $testRoot "diagnostics.json"
    $exe = Join-Path $PayloadDir "ThermeteryBoardviewer.exe"

    $oldPath = $env:Path
    $oldData = $env:BOARDVIEWER_DATA_DIR
    $oldOutput = $env:BOARDVIEWER_SELF_TEST_OUTPUT
    $oldLocalAppData = $env:LOCALAPPDATA
    $oldNativeDir = $env:BOARDVIEW_NATIVE_DIR
    $oldPythonHome = $env:PYTHONHOME
    $oldPythonPath = $env:PYTHONPATH
    try {
        # Prove the bundle does not inherit Python, Meson, Ninja, or GCC from
        # the build machine. Windows system DLL discovery is unaffected.
        $env:Path = "$env:SystemRoot\System32;$env:SystemRoot"
        Remove-Item Env:BOARDVIEWER_DATA_DIR -ErrorAction SilentlyContinue
        Remove-Item Env:BOARDVIEW_NATIVE_DIR -ErrorAction SilentlyContinue
        Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
        $env:LOCALAPPDATA = Join-Path $testRoot "local app data"
        $env:BOARDVIEWER_SELF_TEST_OUTPUT = $diagnostics
        $process = Start-Process -FilePath $exe `
            -ArgumentList "--packaging-self-test" `
            -WorkingDirectory $testRoot `
            -WindowStyle Hidden `
            -PassThru
        if (-not $process.WaitForExit(60000)) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            throw "$Label frozen self-test timed out after 60 seconds."
        }
        $process.Refresh()
    } finally {
        $env:Path = $oldPath
        if ($null -eq $oldData) {
            Remove-Item Env:BOARDVIEWER_DATA_DIR -ErrorAction SilentlyContinue
        } else {
            $env:BOARDVIEWER_DATA_DIR = $oldData
        }
        if ($null -eq $oldOutput) {
            Remove-Item Env:BOARDVIEWER_SELF_TEST_OUTPUT -ErrorAction SilentlyContinue
        } else {
            $env:BOARDVIEWER_SELF_TEST_OUTPUT = $oldOutput
        }
        if ($null -eq $oldLocalAppData) {
            Remove-Item Env:LOCALAPPDATA -ErrorAction SilentlyContinue
        } else {
            $env:LOCALAPPDATA = $oldLocalAppData
        }
        if ($null -eq $oldNativeDir) {
            Remove-Item Env:BOARDVIEW_NATIVE_DIR -ErrorAction SilentlyContinue
        } else {
            $env:BOARDVIEW_NATIVE_DIR = $oldNativeDir
        }
        if ($null -eq $oldPythonHome) {
            Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
        } else {
            $env:PYTHONHOME = $oldPythonHome
        }
        if ($null -eq $oldPythonPath) {
            Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
        } else {
            $env:PYTHONPATH = $oldPythonPath
        }
    }

    if (-not (Test-Path -LiteralPath $diagnostics -PathType Leaf)) {
        throw "$Label frozen self-test did not produce diagnostics (exit $($process.ExitCode))."
    }
    $result = Get-Content -LiteralPath $diagnostics -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($process.ExitCode -ne 0 -or -not $result.ok) {
        throw "$Label frozen self-test failed: $($result.error)"
    }
    if ([bool]$result.portable -ne $ExpectedPortable) {
        throw "$Label reported portable=$($result.portable), expected $ExpectedPortable."
    }
    if ($ExpectedPortable) {
        $expectedData = [IO.Path]::GetFullPath((Join-Path $PayloadDir "data")).TrimEnd('\')
    } else {
        $expectedData = [IO.Path]::GetFullPath(
            (Join-Path $testRoot "local app data\Thermetery Boardviewer")
        ).TrimEnd('\')
    }
    $reportedData = [IO.Path]::GetFullPath([string]$result.data_dir).TrimEnd('\')
    if (-not $reportedData.Equals($expectedData, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label used data directory '$reportedData', expected '$expectedData'."
    }
    if ($ExpectedPortable) {
        Remove-SafeDirectory (Join-Path $PayloadDir "data") "portable self-test data"
    }
    Write-Host "$Label frozen self-test passed."
}

function Test-InnoCompiler([string]$Candidate) {
    if (-not $Candidate -or -not (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
        return $false
    }
    $rawVersion = (Get-Item -LiteralPath $Candidate).VersionInfo.ProductVersion
    $versionMatch = [regex]::Match([string]$rawVersion, '\d+\.\d+(?:\.\d+)?(?:\.\d+)?')
    if ($versionMatch.Success) {
        $parsedVersion = [Version]"0.0"
        if ([Version]::TryParse($versionMatch.Value, [ref]$parsedVersion) -and
            $parsedVersion -ge [Version]"6.7.0") {
            return $true
        }
    }

    # Official reproducible Inno builds expose 0.0.0.0 in Windows version
    # resources. Their bundled revision history starts with the actual release.
    $history = Join-Path (Split-Path -Parent $Candidate) "whatsnew.htm"
    if (Test-Path -LiteralPath $history -PathType Leaf) {
        $historyText = Get-Content -LiteralPath $history -Raw
        $historyMatch = [regex]::Match($historyText, '<a name="(\d+\.\d+(?:\.\d+)?)"')
        if ($historyMatch.Success) {
            $historyVersion = [Version]"0.0"
            if ([Version]::TryParse($historyMatch.Groups[1].Value, [ref]$historyVersion)) {
                return $historyVersion -ge [Version]"6.7.0"
            }
        }
    }
    return $false
}

function Resolve-InnoCompiler {
    $candidates = @(
        (Join-Path $ToolsRoot "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
    )
    foreach ($candidate in $candidates) {
        if (Test-InnoCompiler $candidate) {
            return $candidate
        }
    }
    $command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($command -and (Test-InnoCompiler $command.Source)) {
        return $command.Source
    }
    if ($NoBootstrap) {
        throw "Inno Setup was not found and -NoBootstrap disables automatic installation."
    }

    Write-Step "Bootstrapping signed Inno Setup 6.7.3"
    New-Item -ItemType Directory -Path $ToolsRoot -Force | Out-Null
    $download = Join-Path $ToolsRoot "innosetup-6.7.3.exe"
    if (-not (Test-Path -LiteralPath $download -PathType Leaf)) {
        [Net.ServicePointManager]::SecurityProtocol = `
            [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest `
            -Uri "https://github.com/jrsoftware/issrc/releases/download/is-6_7_3/innosetup-6.7.3.exe" `
            -OutFile $download `
            -UseBasicParsing
    }
    $expectedHash = "9C73C3BAE7ED48D44112A0F48E66742C00090BDB5BEF71D9D3C056C66E97B732"
    $actualHash = (Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash
    if ($actualHash -ne $expectedHash) {
        throw "Refusing to run Inno Setup bootstrap: SHA-256 mismatch."
    }
    $signature = Get-AuthenticodeSignature -LiteralPath $download
    if ($signature.Status -ne "Valid" -or
        $signature.SignerCertificate.Subject -notmatch "Pyrsys B\.V\.") {
        throw "Refusing to run Inno Setup bootstrap: Authenticode validation failed ($($signature.Status))."
    }
    $installDir = Join-Path $ToolsRoot "Inno Setup 6"
    Invoke-Checked -FilePath $download -Arguments @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/SP-",
        "/PORTABLE=1",
        "/DIR=$installDir"
    ) | ForEach-Object { Write-Host $_ }
    $iscc = Join-Path $installDir "ISCC.exe"
    $bootstrapDeadline = [DateTime]::UtcNow.AddSeconds(30)
    while (-not (Test-Path -LiteralPath $iscc -PathType Leaf) -and
        [DateTime]::UtcNow -lt $bootstrapDeadline) {
        Start-Sleep -Milliseconds 250
    }
    if (-not (Test-Path -LiteralPath $iscc -PathType Leaf)) {
        if (Test-Path -LiteralPath $installDir -PathType Container) {
            $iscc = Get-ChildItem -LiteralPath $installDir -Recurse -Filter ISCC.exe -File |
                Select-Object -First 1 -ExpandProperty FullName
        }
    }
    if (-not $iscc -or -not (Test-Path -LiteralPath $iscc -PathType Leaf)) {
        throw "Inno Setup bootstrap completed without producing ISCC.exe."
    }
    if (-not (Test-InnoCompiler $iscc)) {
        throw "Inno Setup bootstrap produced an unsupported ISCC.exe version."
    }
    return $iscc
}

Refresh-ProcessPath
Add-DiscoveredCompilerPath

if ([string]::IsNullOrWhiteSpace($Version)) {
    $mesonText = Get-Content -LiteralPath (Join-Path $RepoRoot "meson.build") -Raw
    $match = [regex]::Match($mesonText, "version\s*:\s*'([^']+)'")
    if (-not $match.Success) {
        throw "Could not infer the release version from meson.build."
    }
    $Version = $match.Groups[1].Value
}
$versionMatch = [regex]::Match(
    $Version,
    '^(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?$'
)
if (-not $versionMatch.Success) {
    throw "Version '$Version' is not a supported semantic version."
}
$numericVersion = "$($versionMatch.Groups[1].Value).$($versionMatch.Groups[2].Value).$($versionMatch.Groups[3].Value).0"

if ($Clean) {
    Remove-SafeDirectory $WorkRoot "release work"
    Remove-SafeDirectory $VenvRoot "release virtual environment"
    Clear-ReleaseOutput $OutputDir $Version
}
New-Item -ItemType Directory -Path $WorkRoot, $OutputDir -Force | Out-Null

$basePython = Resolve-BuildPython
Write-Host "Build Python: $basePython"

if ($NoBootstrap) {
    $buildPython = $basePython
} else {
    Write-Step "Bootstrapping isolated Python build environment"
    $venvPython = Join-Path $VenvRoot "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        Invoke-Checked -FilePath $basePython -Arguments @("-m", "venv", $VenvRoot)
    }
    $buildPython = $venvPython
    Invoke-Checked -FilePath $buildPython -Arguments @(
        "-m", "pip", "install", "--upgrade", "pip"
    )
    Invoke-Checked -FilePath $buildPython -Arguments @(
        "-m", "pip", "install", "-r", (Join-Path $PSScriptRoot "requirements-build.txt")
    )
}
$env:Path = "$(Split-Path -Parent $buildPython);$env:Path"
Invoke-Checked -FilePath $buildPython -Arguments @(
    "-c",
    "import mesonbuild, numpy, PIL, PyInstaller, skia, tkinterdnd2; from OpenGL import GL; import pyopengltk"
)
Assert-NativeCompiler

Push-Location $RepoRoot
try {
    Write-Step "Compiling optimized native parser libraries"
    $nativeBuild = Join-Path $WorkRoot "native"
    $mesonArgs = @(
        "-m", "mesonbuild.mesonmain", "setup",
        $nativeBuild, $RepoRoot,
        "--buildtype=release",
        "-Db_ndebug=true"
    )
    if (Test-LtoSupported) {
        $mesonArgs += "-Db_lto=true"
    } else {
        Write-Host "Toolchain has no LTO support -- building the native libraries without it."
    }
    if (Test-Path -LiteralPath (Join-Path $nativeBuild "meson-private\coredata.dat")) {
        $mesonArgs = @("-m", "mesonbuild.mesonmain", "setup", "--reconfigure") + $mesonArgs[3..($mesonArgs.Count - 1)]
    }
    Invoke-Checked -FilePath $buildPython -Arguments $mesonArgs
    Invoke-Checked -FilePath $buildPython -Arguments @(
        "-m", "mesonbuild.mesonmain", "compile", "-C", $nativeBuild,
        "tvw_native", "xzz_native", "rc6_native"
    )

    $nativeNames = @("tvw_native.dll", "xzz_native.dll", "rc6_native.dll")
    foreach ($name in $nativeNames) {
        $nativePath = Join-Path $nativeBuild $name
        if (-not (Test-Path -LiteralPath $nativePath -PathType Leaf)) {
            throw "Native release library was not produced: $nativePath"
        }
    }

    Write-Step "Verifying source tests and native load paths"
    if (-not $SkipTests) {
        Invoke-Checked -FilePath $buildPython -Arguments @(
            "-m", "unittest", "discover", "-s", "tests", "-v"
        )
    }
    $oldNativeDir = $env:BOARDVIEW_NATIVE_DIR
    try {
        $env:BOARDVIEW_NATIVE_DIR = $nativeBuild
        Invoke-Checked -FilePath $buildPython -Arguments @(
            "-c",
            "from src.parsers.tvw_native import _load; from src.parsers.xzz_native import available; from src.parsers.fz_parser import _load_native_rc6; assert _load() is not None and available() and _load_native_rc6() is not None; print('native DLL checks passed')"
        )
    } finally {
        if ($null -eq $oldNativeDir) {
            Remove-Item Env:BOARDVIEW_NATIVE_DIR -ErrorAction SilentlyContinue
        } else {
            $env:BOARDVIEW_NATIVE_DIR = $oldNativeDir
        }
    }

    Write-Step "Generating Windows icon and version metadata"
    $assetDir = Join-Path $WorkRoot "assets"
    $iconPath = Join-Path $assetDir "ThermeteryBoardviewer.ico"
    $versionFile = Join-Path $assetDir "version_info.txt"
    $iconSource = Join-Path $RepoRoot "android\app\src\main\res\mipmap-xxxhdpi\ic_launcher.png"
    Invoke-Checked -FilePath $buildPython -Arguments @(
        (Join-Path $PSScriptRoot "prepare_windows_assets.py"),
        "--version", $Version,
        "--icon-source", $iconSource,
        "--icon-output", $iconPath,
        "--version-output", $versionFile
    )

    Write-Step "Freezing the self-contained Windows application"
    $pyinstallerDist = Join-Path $WorkRoot "pyinstaller-dist"
    $pyinstallerWork = Join-Path $WorkRoot "pyinstaller-build"
    Remove-SafeDirectory $pyinstallerDist "PyInstaller dist"
    Remove-SafeDirectory $pyinstallerWork "PyInstaller work"
    $env:BOARDVIEW_NATIVE_BUILD_DIR = $nativeBuild
    $env:BOARDVIEW_ICON_PATH = $iconPath
    $env:BOARDVIEW_VERSION_FILE = $versionFile
    Invoke-Checked -FilePath $buildPython -Arguments @(
        "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--distpath", $pyinstallerDist,
        "--workpath", $pyinstallerWork,
        (Join-Path $PSScriptRoot "boardviewer.spec")
    )
    $payload = Join-Path $pyinstallerDist "ThermeteryBoardviewer"
    Invoke-Checked -FilePath $buildPython -Arguments @(
        (Join-Path $PSScriptRoot "collect_licenses.py"),
        "--repo-root", $RepoRoot,
        "--destination", (Join-Path $payload "licenses")
    )
    Assert-ReleasePayload $payload
    Invoke-FrozenSelfTest -PayloadDir $payload -ExpectedPortable $false -Label "installed"

    Write-Step "Creating portable ZIP"
    $portableStage = Join-Path $WorkRoot "portable-stage"
    Remove-SafeDirectory $portableStage "portable staging"
    New-Item -ItemType Directory -Path $portableStage -Force | Out-Null
    $portablePayload = Join-Path $portableStage "ThermeteryBoardviewer"
    Copy-Item -LiteralPath $payload -Destination $portablePayload -Recurse
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "PORTABLE_README.txt") `
        -Destination (Join-Path $portablePayload "README.txt") -Force
    [IO.File]::WriteAllText(
        (Join-Path $portablePayload "portable.flag"),
        "Portable mode: settings and keys are stored under .\data.`r`n",
        [Text.UTF8Encoding]::new($false)
    )
    Invoke-FrozenSelfTest -PayloadDir $portablePayload -ExpectedPortable $true -Label "portable"

    $portableZip = Join-Path $OutputDir "ThermeteryBoardviewer-$Version-windows-x64-portable.zip"
    Remove-Item -LiteralPath $portableZip -Force -ErrorAction SilentlyContinue
    New-PortableArchive -SourceDir $portablePayload -Destination $portableZip
    if (-not (Test-Path -LiteralPath $portableZip -PathType Leaf)) {
        throw "Portable archive was not created: $portableZip"
    }

    # Everything above this point tested the staging tree, which no user ever
    # runs. Extract the archive that actually ships and launch THAT, so a
    # fault introduced by the archive step - a dropped file, a mangled entry
    # name, an unexpected layout - fails the build instead of the customer.
    Write-Step "Verifying the shipped portable archive"
    $verifyRoot = Join-Path $WorkRoot "portable-verify"
    Remove-SafeDirectory $verifyRoot "portable verification"
    New-Item -ItemType Directory -Path $verifyRoot -Force | Out-Null
    Expand-Archive -LiteralPath $portableZip -DestinationPath $verifyRoot -Force
    $extractedExe = Join-Path $verifyRoot "ThermeteryBoardviewer.exe"
    if (-not (Test-Path -LiteralPath $extractedExe -PathType Leaf)) {
        throw ("The portable archive does not extract ThermeteryBoardviewer.exe " +
               "at its root: $portableZip")
    }
    $stagedCount = (Get-ChildItem -LiteralPath $portablePayload -Recurse -File -Force).Count
    $extractedCount = (Get-ChildItem -LiteralPath $verifyRoot -Recurse -File -Force).Count
    if ($extractedCount -ne $stagedCount) {
        throw ("The portable archive round-trips $extractedCount files but the " +
               "payload has $stagedCount.")
    }
    Assert-PortableRuntime $verifyRoot
    Invoke-FrozenSelfTest -PayloadDir $verifyRoot -ExpectedPortable $true `
        -Label "extracted portable"
    Remove-SafeDirectory $verifyRoot "portable verification"

    $artifacts = @($portableZip)
    if (-not $SkipInstaller) {
        Write-Step "Building per-user Windows installer"
        $iscc = Resolve-InnoCompiler
        $installerBase = "ThermeteryBoardviewer-$Version-windows-x64-setup"
        $env:BOARDVIEWER_APP_VERSION = $Version
        $env:BOARDVIEWER_NUMERIC_VERSION = $numericVersion
        $env:BOARDVIEWER_PAYLOAD_DIR = $payload
        $env:BOARDVIEWER_INSTALLER_OUTPUT_DIR = $OutputDir
        $env:BOARDVIEWER_INSTALLER_BASENAME = $installerBase
        $env:BOARDVIEWER_ICON_PATH = $iconPath
        Invoke-Checked -FilePath $iscc -Arguments @(
            (Join-Path $PSScriptRoot "installer.iss")
        )
        $installer = Join-Path $OutputDir "$installerBase.exe"
        if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
            throw "Installer was not created: $installer"
        }
        $artifacts += $installer
    }

    Write-Step "Writing release checksums"
    $checksumRows = foreach ($artifact in ($artifacts | Sort-Object)) {
        $hash = (Get-FileHash -LiteralPath $artifact -Algorithm SHA256).Hash.ToLowerInvariant()
        "$hash  $(Split-Path -Leaf $artifact)"
    }
    $checksumPath = Join-Path $OutputDir "SHA256SUMS.txt"
    [IO.File]::WriteAllLines(
        $checksumPath,
        [string[]]$checksumRows,
        [Text.UTF8Encoding]::new($false)
    )

    Write-Host ""
    Write-Host "Release build complete:" -ForegroundColor Green
    foreach ($artifact in ($artifacts + $checksumPath)) {
        $item = Get-Item -LiteralPath $artifact
        Write-Host ("  {0} ({1:N1} MiB)" -f $item.FullName, ($item.Length / 1MB))
    }
} finally {
    Pop-Location
}
