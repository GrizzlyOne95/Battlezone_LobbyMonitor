#!/bin/bash
# Build script for macOS and Linux

set -e

VERSION="${VERSION:-0.0.0}"
VERSION="${VERSION#v}"
APP_NAME="BZLobbyMonitor"
ARCHIVE_PREFIX="Battlezone_LobbyMonitor-v${VERSION}"

# Install dependencies
pip install -r requirements.txt
pip install pyinstaller

# Build for current platform
pyinstaller --name "${APP_NAME}" \
    --onefile \
    --windowed \
    --add-data "bzr_monitor_config.json:." \
    --add-data "LICENSE:." \
    --add-data "README.md:." \
    --hidden-import=websocket \
    --hidden-import=pystray \
    --hidden-import=pypresence \
    --hidden-import=pysocks \
    --hidden-import=PIL \
    bzr_monitor.py

mkdir -p releases

# Create platform-specific release archive. The executable name stays versionless;
# only the archive carries the release version.
if [[ "$OSTYPE" == "linux-gnu"* ]]; then
    cd dist
    tar -czf "../releases/${ARCHIVE_PREFIX}-linux-x64.tar.gz" "${APP_NAME}"
    cd ..
elif [[ "$OSTYPE" == "darwin"* ]]; then
    cd dist
    if [ -d "${APP_NAME}.app" ]; then
        zip -qr "../releases/${ARCHIVE_PREFIX}-macos-x64.zip" "${APP_NAME}.app"
    else
        tar -czf "../releases/${ARCHIVE_PREFIX}-macos-x64.tar.gz" "${APP_NAME}"
    fi
    cd ..
fi

echo "Build complete! Archive created in releases/ directory"
