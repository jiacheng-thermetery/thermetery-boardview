#!/bin/bash
# Xcode build phase: copy the shared JS renderer (owned by the renderer agent
# — never fork it) and the generated third-party license text from the
# Android assets folder into the app bundle at $APP/web/. The iOS shell
# serves viewer.html from there via WKWebView.loadFileURL.
set -euo pipefail

REPO_ROOT="$(cd "$PROJECT_DIR/.." && pwd)"
SRC="$REPO_ROOT/android/app/src/main/assets"
DEST="$CODESIGNING_FOLDER_PATH/web"

ASSETS=(viewer.html viewer.js viewer.css third_party_licenses.txt)

for a in "${ASSETS[@]}"; do
    if [ ! -f "$SRC/$a" ]; then
        echo "error: stage_assets.sh: $SRC/$a is missing — the shared renderer" \
             "assets live in the Android tree (contract: docs/android_contract.md §1)." >&2
        exit 1
    fi
done

rm -rf "$DEST"
mkdir -p "$DEST"
for a in "${ASSETS[@]}"; do
    cp "$SRC/$a" "$DEST/$a"
done
echo "Staged ${#ASSETS[@]} web assets into web/"
