#!/bin/bash
# Xcode build phase: cross-compile the three native parser kernels
# (src/parsers/native/{tvw,xzz,rc6}_native.c — no external deps) for the
# platform/arch being built and place them in the app bundle's Frameworks/
# directory as {name}_native.dylib.
#
# The Python wrappers find them because PythonRuntime.swift exports
# BOARDVIEW_NATIVE_DIR=<bundle>/Frameworks before the interpreter starts —
# the first (env-override) branch of runtime_paths.native_lib_candidates().
# The filenames match native_lib_names()'s first candidate on iOS
# ("<base>.dylib" — sys.platform == "ios" maps to the darwin suffix).
#
# Flags mirror build_android_native.bat (-O3, stripped); -dynamiclib is the
# Darwin spelling of -shared. Each dylib is code-signed here because Xcode
# only auto-signs frameworks it embeds itself, not files added by scripts.
set -euo pipefail

REPO_ROOT="$(cd "$PROJECT_DIR/.." && pwd)"
NATIVE_SRC="$REPO_ROOT/src/parsers/native"
OUT_DIR="$CODESIGNING_FOLDER_PATH/Frameworks"
TMP_DIR="$DERIVED_FILE_DIR/boardview-kernels"
mkdir -p "$OUT_DIR" "$TMP_DIR"

if [ "$EFFECTIVE_PLATFORM_NAME" = "-iphonesimulator" ]; then
    SDK=iphonesimulator
    TARGET_SUFFIX="-simulator"
else
    SDK=iphoneos
    TARGET_SUFFIX=""
fi
SDK_PATH="$(xcrun --sdk $SDK --show-sdk-path)"
MIN_IOS="${IPHONEOS_DEPLOYMENT_TARGET:-15.0}"
IDENTITY="${EXPANDED_CODE_SIGN_IDENTITY:--}"

for kernel in tvw_native xzz_native rc6_native; do
    slices=()
    for arch in $ARCHS; do
        slice="$TMP_DIR/${kernel}.${arch}.dylib"
        xcrun --sdk $SDK clang \
            -isysroot "$SDK_PATH" \
            -target "${arch}-apple-ios${MIN_IOS}${TARGET_SUFFIX}" \
            -O3 -fPIC -dynamiclib \
            -Wl,-install_name,"@rpath/${kernel}.dylib" \
            -o "$slice" "$NATIVE_SRC/${kernel}.c"
        slices+=("$slice")
    done
    dest="$OUT_DIR/${kernel}.dylib"
    if [ ${#slices[@]} -gt 1 ]; then
        lipo -create "${slices[@]}" -output "$dest"
    else
        cp "${slices[0]}" "$dest"
    fi
    strip -x "$dest"
    /usr/bin/codesign --force --sign "$IDENTITY" ${OTHER_CODE_SIGN_FLAGS:-} \
        --timestamp=none --generate-entitlement-der "$dest"
    echo "Built + signed Frameworks/${kernel}.dylib ($ARCHS)"
done
