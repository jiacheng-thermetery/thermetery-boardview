# Thermetery Boardview — Android shell

Single-activity Android app: a fullscreen WebView hosts the JS renderer
(`app/src/main/assets/viewer.html`), Chaquopy embeds CPython 3.13 with the
board parsers, and prebuilt native kernels live in
`app/src/main/jniLibs/<abi>/`. The binding contract between the three
components is `../docs/android_contract.md` — read it before changing
anything here.

## Layout / ownership

| Path | What | Owner |
|---|---|---|
| `settings.gradle.kts`, `build.gradle.kts`, `gradle.properties`, `app/build.gradle.kts` | Gradle config (AGP 8.7.3, Kotlin 2.0.21, Chaquopy 17.0.0) | shell |
| `app/src/main/java/com/thermetery/boardview/` | Kotlin sources | shell |
| `app/src/main/res/` | theme + launcher icon | shell |
| `app/src/main/assets/` | viewer.html / viewer.js / viewer.css (architecture notes: `../docs/WEB_CORE.md`) | renderer agent |
| `app/src/main/jniLibs/` | prebuilt `lib{tvw,xzz,rc6}_native.so` (arm64-v8a, x86_64) | already built — do not touch |

Python sources are **not** checked in under `android/` — a Gradle task
(`:app:stagePythonSources`) copies the curated module list from the repo
root into `app/build/staged-python/` before Chaquopy packages them,
preserving the `src/` package paths (contract §6). The authoritative
list is `pythonRootModules` + `pythonPackageModules` in
`app/build.gradle.kts` (`board_export.py` plus the `src/` parser core,
19 files today). If the build fails with `stagePythonSources: missing
module(s)`, the named file does not exist at the repo root — supply the
module or update the staging list *together with* the code change that
moved it.

## Build (local machine paths)

No Gradle wrapper — invoke the local Gradle install directly.

```powershell
$env:JAVA_HOME    = "C:/Android/jdk-17.0.19+10"
$env:ANDROID_HOME = "C:/Android/sdk"     # optional; local.properties pins sdk.dir

C:/Android/gradle-8.10.2/bin/gradle.bat -p C:/thermetery-boardview/android :app:assembleDebug
```

- First build downloads AGP/Chaquopy from Google/Maven Central and
  `numpy==1.26.2` wheels from the Chaquopy pip repository — allow several
  minutes.
- Chaquopy 17 requires `buildPython` to be the **same minor version** as the
  target (`version = "3.13"`), so the anaconda 3.11 python named in the
  contract cannot be used. A CPython 3.13 was installed via the Python
  install manager (`py install 3.13`) and `app/build.gradle.kts` pins
  `buildPython` to
  `C:/Users/Administrator/AppData/Local/Python/pythoncore-3.13-64/python.exe`.
  On another machine: install any CPython 3.13 and update that one line.
- `local.properties` (not committed) must contain `sdk.dir=C\:\\Android\\sdk`.

The debug APK lands at:

```
android/app/build/outputs/apk/debug/app-debug.apk
```

## Run on the emulator

The AVD `spraak` already exists on this machine:

```powershell
C:/Android/sdk/emulator/emulator.exe -avd spraak          # leave running
C:/Android/sdk/platform-tools/adb.exe install -r C:/thermetery-boardview/android/app/build/outputs/apk/debug/app-debug.apk
C:/Android/sdk/platform-tools/adb.exe shell am start -n com.thermetery.boardview/.MainActivity
```

Useful logcat tags:

```powershell
C:/Android/sdk/platform-tools/adb.exe logcat -s Boardview BoardviewPy BoardviewJS python.stdout python.stderr
```

- `BoardviewPy` — Python startup + the `board_export.ping()` native-kernel
  availability JSON (logged once at app start).
- `BoardviewJS` — `Android.log(...)` passthrough and WebView console output.
- `Boardview` — shell-side events (errors forwarded to the renderer, key saves).

## Behaviour notes

- Opening a board: in-app picker (SAF `ACTION_OPEN_DOCUMENT`) or "open with"
  from a file manager (`ACTION_VIEW` for `.tvw .fz .pcb .cad .brd .brd2 .bv`).
  The stream is copied to `cacheDir/boards/<displayName>` and kept for the
  session — the lazy trace build re-reads it.
- Encrypted boards (`.fz`, XZZ `.pcb`): a native dialog prompts for the key,
  up to 3 attempts, mirroring `viewer.py:_load_with_key_prompt`. Checking
  "Remember on this device" stores the working key at
  `filesDir/keys/<format>.txt`; it is supplied automatically next time.
  `android:allowBackup="false"` keeps keys out of cloud backups. To forget
  keys: Settings → Apps → Boardview → Clear storage (or delete the file via
  `adb shell run-as com.thermetery.boardview rm files/keys/<format>.txt`).
- Renderer-without-Android development (desktop browser) is covered by the
  contract's dev-harness rule and needs nothing from this shell.
