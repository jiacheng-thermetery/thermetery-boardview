#!/bin/bash
# Xcode build phase: stage the Python parser core and site packages into the
# app bundle.
#
#   $APP/app/           board_export.py + the src/ parser core, repo-relative
#                       paths intact (the core uses relative imports and must
#                       not be flattened) — mirrors the Android Gradle task
#                       :app:stagePythonSources and shares its authoritative
#                       module list (android/app/build.gradle.kts).
#   $APP/app_packages/  numpy for the platform being built (device/simulator).
#
# Fails the build if a listed module is missing, exactly like the Gradle task:
# when a module moves or a new import is added to the core, update this list
# together with the code change. NEVER glob the repo root — it contains
# gitignored sample boards and Windows DLLs.
set -euo pipefail

REPO_ROOT="$(cd "$PROJECT_DIR/.." && pwd)"
APP_DIR="$CODESIGNING_FOLDER_PATH/app"
PKG_DIR="$CODESIGNING_FOLDER_PATH/app_packages"
STAMP="$DERIVED_FILE_DIR/boardview-python.stamp"

PYTHON_MODULES=(
    "board_export.py"
    "src/__init__.py"
    "src/ratsnest.py"
    "src/runtime_paths.py"
    "src/units.py"
    "src/key_store.py"
    "src/tvw_compal.py"
    "src/tvw_topology.py"
    "src/parsers/__init__.py"
    "src/parsers/boardview.py"
    "src/parsers/gencad_parser.py"
    "src/parsers/brd_parser.py"
    "src/parsers/asc_parser.py"
    "src/parsers/tvw_parser.py"
    "src/parsers/tvw_master_fp.py"
    "src/parsers/tvw_trace_scanners.py"
    "src/parsers/fz_parser.py"
    "src/parsers/xzzpcb_parser.py"
    "src/parsers/tvw_native.py"
    "src/parsers/xzz_native.py"
)

missing=()
for m in "${PYTHON_MODULES[@]}"; do
    [ -f "$REPO_ROOT/$m" ] || missing+=("$m")
done
if [ ${#missing[@]} -gt 0 ]; then
    echo "error: stage_python.sh: missing module(s) in $REPO_ROOT: ${missing[*]}" \
         "— the parser core lives under src/parsers/. Supply the missing" \
         "file(s); do NOT flatten the list." >&2
    exit 1
fi

# numpy: pick the wheel matching the platform being built. fetch_deps.sh
# populates these; EFFECTIVE_PLATFORM_NAME is "-iphoneos"/"-iphonesimulator".
WHEEL_DIR="$PROJECT_DIR/vendor/app_packages.${EFFECTIVE_PLATFORM_NAME#-}"
if [ ! -d "$WHEEL_DIR/numpy" ]; then
    echo "error: stage_python.sh: $WHEEL_DIR is missing — run ios/fetch_deps.sh first." >&2
    exit 1
fi

# Change detection: the embed step below re-converts and re-signs every
# Python binary module (~157 sequential codesigns, minutes per build) if it
# runs, so skip the whole stage+embed when nothing it consumes has changed.
fingerprint() {
    {
        for m in "${PYTHON_MODULES[@]}"; do stat -f "%N %z %m" "$REPO_ROOT/$m"; done
        find "$WHEEL_DIR" -type f -exec stat -f "%N %z %m" {} + | sort
        stat -f "%z %m" "$PROJECT_DIR/vendor/Python.xcframework/VERSIONS" 2>/dev/null || true
        echo "$EFFECTIVE_PLATFORM_NAME ${ARCHS:-} ${EXPANDED_CODE_SIGN_IDENTITY:--}"
    } | /sbin/md5 -q
}
FP="$(fingerprint)"
if [ -f "$STAMP" ] && [ "$(cat "$STAMP")" = "$FP" ] \
   && [ -d "$APP_DIR" ] && [ -d "$CODESIGNING_FOLDER_PATH/python/lib" ]; then
    echo "Python payload unchanged — skipping stage + embed"
    exit 0
fi
rm -f "$STAMP"

rm -rf "$APP_DIR"
for m in "${PYTHON_MODULES[@]}"; do
    mkdir -p "$APP_DIR/$(dirname "$m")"
    cp "$REPO_ROOT/$m" "$APP_DIR/$m"
done
echo "Staged ${#PYTHON_MODULES[@]} Python modules into app/"

rm -rf "$PKG_DIR"
mkdir -p "$PKG_DIR"
rsync -a "$WHEEL_DIR/" "$PKG_DIR/"
echo "Staged app_packages from $(basename "$WHEEL_DIR")"

# Embed: stdlib into python/lib, every .so (stdlib + numpy) converted into a
# signed framework with a .fwork marker (Python-Apple-support's helper).
source "$PROJECT_DIR/vendor/Python.xcframework/build/utils.sh"
install_python vendor/Python.xcframework app_packages

echo "$FP" > "$STAMP"
