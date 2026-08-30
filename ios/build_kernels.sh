#!/bin/bash
# Xcode build phase: cross-compile the three native parser kernels
# (src/parsers/native/{tvw,xzz,rc6}_native.c — no external deps) for the
# platform/arch being built and package each as a framework bundle at
# Frameworks/<name>.framework/<name> — App Store validation requires every
# dynamic library to live inside its own framework (same reason
# Python-Apple-support converts the Python .so modules).
#
# The Python wrappers find them because PythonRuntime.swift exports
# BOARDVIEW_NATIVE_DIR=<bundle>/Frameworks before the interpreter starts,
# and runtime_paths.native_lib_candidates() tries the iOS framework shape
# (<dir>/<base>.framework/<base>) first on sys.platform == "ios".
#
# Compile flags mirror build_android_native.bat (-O3, stripped); -dynamiclib
# is the Darwin spelling of -shared. Each framework is code-signed here
# (mirroring Python-Apple-support's utils.sh) because Xcode only auto-signs
# frameworks it embeds itself, not files added by scripts.
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

PLIST_TEMPLATE="$PROJECT_DIR/vendor/Python.xcframework/build/iOS-dylib-Info-template.plist"

for kernel in tvw_native xzz_native rc6_native; do
    fw_dir="$OUT_DIR/${kernel}.framework"
    slices=()
    for arch in $ARCHS; do
        slice="$TMP_DIR/${kernel}.${arch}.dylib"
        xcrun --sdk $SDK clang \
            -isysroot "$SDK_PATH" \
            -target "${arch}-apple-ios${MIN_IOS}${TARGET_SUFFIX}" \
            -O3 -fPIC -dynamiclib \
            -Wl,-install_name,"@rpath/${kernel}.framework/${kernel}" \
            -o "$slice" "$NATIVE_SRC/${kernel}.c"
        slices+=("$slice")
    done
    # Drop the loose-dylib layout from earlier builds of this bundle.
    rm -f "$OUT_DIR/${kernel}.dylib"
    mkdir -p "$fw_dir"
    dest="$fw_dir/${kernel}"
    if [ ${#slices[@]} -gt 1 ]; then
        lipo -create "${slices[@]}" -output "$dest"
    else
        cp "${slices[0]}" "$dest"
    fi
    strip -x "$dest"
    cp "$PLIST_TEMPLATE" "$fw_dir/Info.plist"
    plutil -replace CFBundleExecutable -string "$kernel" "$fw_dir/Info.plist"
    plutil -replace CFBundleIdentifier \
        -string "$(echo "$PRODUCT_BUNDLE_IDENTIFIER.$kernel" | tr '_' '-')" \
        "$fw_dir/Info.plist"
    /usr/bin/codesign --force --sign "$IDENTITY" ${OTHER_CODE_SIGN_FLAGS:-} \
        -o runtime --timestamp=none \
        --preserve-metadata=identifier,entitlements,flags \
        --generate-entitlement-der "$fw_dir"
    echo "Built + signed Frameworks/${kernel}.framework ($ARCHS)"
done
