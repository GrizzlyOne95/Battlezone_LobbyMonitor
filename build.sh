#!/bin/bash
# Build script for macOS and Linux

# Install dependencies
pip install -r requirements.txt
pip install pyinstaller

# Build for current platform
pyinstaller --name "bzr_monitor" \
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

# Create platform-specific release archive
if [[ "$OSTYPE" == "linux-gnu"* ]]; then
    PLATFORM="linux-x64"
    BINARY="bzr_monitor"
    cd dist
    tar -czf ../releases/bzr_monitor-v1.0.0-${PLATFORM}.tar.gz ${BINARY}
    cd ..
elif [[ "$OSTYPE" == "darwin"* ]]; then
    PLATFORM="macos-x64"
    BINARY="bzr_monitor"
    cd dist
    zip -r ../releases/bzr_monitor-v1.0.0-${PLATFORM}.zip ${BINARY}.app/
    cd ..
fi

echo "Build complete! Archive created in releases/ directory"
