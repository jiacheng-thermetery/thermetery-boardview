# Windows release packaging

The Windows release build produces two standalone x64 distributions from the
same application payload:

- `ThermeteryBoardviewer-<version>-windows-x64-setup.exe` is the normal
  installer, with shortcuts and uninstall support.
- `ThermeteryBoardviewer-<version>-windows-x64-portable.zip` is the portable
  edition. It does not install or modify the system-wide `PATH`.
- `SHA256SUMS.txt` contains checksums for the distributable files.

Both Windows 10/11 x64 editions include Python, Tcl/Tk, application dependencies, and the native
parser DLLs. People running the application do not need Python, Meson, Ninja,
GCC, or Inno Setup.

## For people downloading the application

Choose one distribution; you do not need both.

For the installed edition, download and run the `-setup.exe` file. For the
portable edition, extract the entire ZIP to a writable directory and launch
`ThermeteryBoardviewer.exe` from the extracted
`ThermeteryBoardviewer` directory. Do not run it from inside the ZIP or move
only the EXE: its `_internal` directory and `portable.flag` are part of the
application.

Remembered keys in the portable edition are plaintext files under
`data\private`; remove that directory before sharing or re-zipping a used copy.

To verify a download in PowerShell, compare its hash with the matching entry in
`SHA256SUMS.txt`:

```powershell
Get-FileHash .\ThermeteryBoardviewer-1.0.0-windows-x64-setup.exe -Algorithm SHA256
Get-Content .\SHA256SUMS.txt
```

The source archive and `packaging/build_windows.ps1` are for developers and
release maintainers. End users should not need to compile anything.

## Building locally

Run the release build on 64-bit Windows from the repository root. Local builds
need built-in Windows PowerShell, 64-bit Python 3.12 or newer, and a
Meson-compatible x64 C toolchain. The script finds the MinGW GCC path used by
this project automatically. For MSVC, run it from an x64 Visual Studio
Developer shell.

The default build creates an isolated Python environment and, when needed,
downloads the signed Inno Setup 6.7.3 compiler into the repository tool cache.
Pass `-NoBootstrap` in an already provisioned or network-restricted environment.

```powershell
.\packaging\build_windows.cmd -Version 1.0.0 -Clean
```

Artifacts are written to the repository's `release` directory by default. A
different output directory can be selected explicitly:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\packaging\build_windows.ps1 `
  -Version 1.0.0 `
  -OutputDir C:\release\boardviewer `
  -Clean
```

Supported switches:

| Switch | Purpose |
| --- | --- |
| `-Version <semver>` | Set the version embedded in artifact names; defaults to `meson.build`. |
| `-OutputDir <path>` | Override the default `release` output directory. |
| `-Clean` | Recreate the isolated build environment and remove owned stale artifacts. |
| `-SkipTests` | Skip source unit tests; frozen diagnostics still run. |
| `-SkipInstaller` | Build the portable ZIP without the Inno Setup installer. |
| `-NoBootstrap` | Require build dependencies to already be installed. |

The script compiles the native parser libraries, runs the test suite unless
disabled, freezes the Tkinter application, creates the portable ZIP, builds the
installer when enabled, and writes SHA-256 checksums.

Local artifacts are unsigned unless a separate code-signing step is added.
Unsigned downloads can trigger a Windows SmartScreen warning; public releases
should sign the app executable and installer with the project's certificate.
Organizations distributing the Inno Setup-based installer commercially should
also confirm that their Inno Setup license covers that use.

## Android licenses

`collect_android_licenses.py` is the APK counterpart of `collect_licenses.py`:
it consolidates the project notices, the Chaquopy-installed Python
requirements (read from `android/app/build/python/pip/release/common`, so a
build must exist), and the static texts under `LICENSES/android/` into
`android/app/src/main/assets/third_party_licenses.txt`. The asset is
committed so APK builds stay offline-deterministic; re-run the script and
commit the result whenever the Chaquopy `pip` block, the Chaquopy/Python
version, or the bundled runtime components change. The app shows it under
Keys → Third-party licenses.

## GitHub Actions

`.github/workflows/windows-release.yml` runs the same build on a hosted Windows
x64 runner. It is manual-only: start it from the Actions tab with a semantic
version (tag pushes deliberately do not trigger it, so cutting a release never
burns a runner unasked). The workflow uploads the installer, portable ZIP, and
checksum file as one Actions artifact. It does not create or publish a GitHub
Release automatically.
