@echo off
rem Cross-compile the three native kernels to Android .so for every ABI the
rem app ships. The desktop build now uses meson (meson.build); this script is
rem the Android cross-compile, with the NDK clang target triple added and
rem -static-libgcc dropped (NDK clang links its own compiler-rt).
rem
rem After the src-layout restructure the kernel sources live under
rem src\parsers\native\ (not the repo root).
rem
rem Requires ANDROID_NDK to point at an NDK install (r28+ recommended: those
rem emit 16 KB-page-aligned .so by default, which Google Play requires for
rem Android 15+ targets). On older NDKs add -Wl,-z,max-page-size=16384.
rem
rem Output: android\app\src\main\jniLibs\<abi>\lib<name>.so

setlocal enabledelayedexpansion
if "%ANDROID_NDK%"=="" set ANDROID_NDK=D:\Android\android-ndk-r29
set CLANG=%ANDROID_NDK%\toolchains\llvm\prebuilt\windows-x86_64\bin\clang.exe
if not exist "%CLANG%" (
    echo ERROR: NDK clang not found at %CLANG%
    exit /b 1
)

set MINAPI=24
set OUTROOT=%~dp0android\app\src\main\jniLibs

for %%A in (aarch64-linux-android:arm64-v8a x86_64-linux-android:x86_64) do (
    for /f "tokens=1,2 delims=:" %%T in ("%%A") do (
        set TRIPLE=%%T%MINAPI%
        set ABIDIR=%OUTROOT%\%%U
        if not exist "!ABIDIR!" mkdir "!ABIDIR!"
        for %%L in (tvw_native xzz_native rc6_native) do (
            echo Building lib%%L.so for %%U ...
            "%CLANG%" --target=!TRIPLE! -O3 -fPIC -shared -Wl,--strip-all ^
                -o "!ABIDIR!\lib%%L.so" "%~dp0src\parsers\native\%%L.c"
            if errorlevel 1 exit /b 1
        )
    )
)
echo.
echo All Android kernels built under %OUTROOT%
