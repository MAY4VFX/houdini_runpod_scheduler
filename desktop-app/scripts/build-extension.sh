#!/bin/bash
# Build, embed, and sign the File Provider Extension (.appex)
# Usage: ./scripts/build-extension.sh [debug|release]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
FP_DIR="$PROJECT_DIR/FileProvider"
BUILD_CONFIG="${1:-debug}"

echo "==> Building File Provider Extension ($BUILD_CONFIG)..."

# Check for Xcode
if ! command -v xcodebuild &>/dev/null; then
    echo "ERROR: Xcode command line tools not found. Install with: xcode-select --install"
    exit 1
fi

# Check for xcodegen (to regenerate project if needed)
if command -v xcodegen &>/dev/null; then
    echo "==> Regenerating Xcode project..."
    cd "$FP_DIR"
    xcodegen generate 2>/dev/null || true
fi

# Build the extension
echo "==> Running xcodebuild..."
cd "$FP_DIR"

XCODE_CONFIG="Debug"
if [ "$BUILD_CONFIG" = "release" ]; then
    XCODE_CONFIG="Release"
fi

xcodebuild \
    -project RunPodFarmFileProvider.xcodeproj \
    -scheme RunPodFarmFileProvider \
    -configuration "$XCODE_CONFIG" \
    -derivedDataPath build \
    CODE_SIGN_IDENTITY="-" \
    CODE_SIGNING_REQUIRED=NO \
    CODE_SIGNING_ALLOWED=NO \
    2>&1 | tail -20

# Find the built .appex
APPEX_PATH=$(find "$FP_DIR/build" -name "*.appex" -type d | head -1)
if [ -z "$APPEX_PATH" ]; then
    echo "ERROR: Built .appex not found"
    exit 1
fi
echo "==> Built extension at: $APPEX_PATH"

# Find the Tauri .app bundle
APP_BUNDLE=""
for dir in \
    "$PROJECT_DIR/src-tauri/target/debug/bundle/macos/RunPodFarm.app" \
    "$PROJECT_DIR/src-tauri/target/release/bundle/macos/RunPodFarm.app"; do
    if [ -d "$dir" ]; then
        APP_BUNDLE="$dir"
        break
    fi
done

if [ -n "$APP_BUNDLE" ]; then
    echo "==> Embedding extension in $APP_BUNDLE..."
    PLUGINS_DIR="$APP_BUNDLE/Contents/PlugIns"
    mkdir -p "$PLUGINS_DIR"

    # Remove old extension if exists
    rm -rf "$PLUGINS_DIR/RunPodFarmFileProvider.appex"

    # Copy new extension
    cp -R "$APPEX_PATH" "$PLUGINS_DIR/"

    echo "==> Extension embedded successfully"

    # Sign inside-out (extension first, then app)
    if [ "$BUILD_CONFIG" = "release" ]; then
        echo "==> Signing..."
        codesign --force --deep --sign - "$PLUGINS_DIR/RunPodFarmFileProvider.appex"
        codesign --force --deep --sign - "$APP_BUNDLE"
        echo "==> Signed (ad-hoc)"
    fi
else
    echo "WARNING: Tauri app bundle not found. Build the app first with 'cargo tauri build'"
    echo "         Then re-run this script to embed the extension."
fi

echo "==> Done!"
