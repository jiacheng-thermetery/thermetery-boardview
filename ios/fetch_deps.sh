#!/bin/bash
# Download the third-party binary dependencies the iOS app embeds:
#
#   vendor/Python.xcframework            CPython 3.13 for iOS (BeeWare
#                                        Python-Apple-support, incl. stdlib
#                                        and the install_python build helper)
#   vendor/app_packages.iphoneos/        numpy 1.26.2 (device wheel — same
#                                        version Chaquopy pins on Android)
#   vendor/app_packages.iphonesimulator/ numpy 1.26.2 (simulator wheel)
#
# Everything lands under ios/vendor/, which is gitignored. Idempotent: each
# component is skipped when already present. Run once per fresh checkout.
set -euo pipefail
cd "$(dirname "$0")"

PY_SUPPORT_TAG="3.13-b14"
PY_SUPPORT_URL="https://github.com/beeware/Python-Apple-support/releases/download/${PY_SUPPORT_TAG}/Python-3.13-iOS-support.b14.tar.gz"
NUMPY_VERSION="1.26.2"
NUMPY_BASE="https://pypi.anaconda.org/beeware/simple/numpy/${NUMPY_VERSION}"

mkdir -p vendor

if [ ! -d vendor/Python.xcframework ]; then
    echo "Downloading Python-Apple-support ${PY_SUPPORT_TAG}..."
    curl -sL -o vendor/python-ios-support.tar.gz "$PY_SUPPORT_URL"
    tar xzf vendor/python-ios-support.tar.gz -C vendor
    rm vendor/python-ios-support.tar.gz
else
    echo "vendor/Python.xcframework already present"
fi

fetch_numpy() {
    local platform=$1 wheel=$2 dest="vendor/app_packages.$1"
    if [ -d "$dest/numpy" ]; then
        echo "$dest already present"
        return
    fi
    echo "Downloading numpy ${NUMPY_VERSION} for ${platform}..."
    curl -sL -o "vendor/$wheel" "$NUMPY_BASE/$wheel"
    mkdir -p "$dest"
    unzip -qo "vendor/$wheel" -d "$dest"
    rm "vendor/$wheel"
}

fetch_numpy iphoneos "numpy-${NUMPY_VERSION}-cp313-cp313-ios_13_0_arm64_iphoneos.whl"
fetch_numpy iphonesimulator "numpy-${NUMPY_VERSION}-cp313-cp313-ios_13_0_arm64_iphonesimulator.whl"

echo "All iOS dependencies ready under ios/vendor/"
