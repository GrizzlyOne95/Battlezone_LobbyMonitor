# Battlezone Redux Lobby Monitor - Release Instructions

## Release Build Process

This document describes how to build releases for all platforms.

### Prerequisites

- Python 3.6+
- PyInstaller (`pip install pyinstaller`)
- All dependencies from requirements.txt

### Building Releases

#### Windows
```powershell
# Install dependencies
python -m pip install -r requirements.txt
python -m pip install pyinstaller

# Build executable
python -m PyInstaller --name "bzr_monitor" --onefile --windowed ^
    --add-data "bzr_monitor_config.json:." bzr_monitor.py -y

# The executable will be in dist/bzr_monitor.exe
# Create a zip archive:
# Compress-Archive -Path dist\bzr_monitor.exe -DestinationPath releases\bzr_monitor-v1.1.0-windows-x64.zip
```

#### macOS
```bash
# Install dependencies
python3 -m pip install -r requirements.txt
python3 -m pip install pyinstaller

# Build executable
python3 -m PyInstaller --name "bzr_monitor" --onefile --windowed \
    --add-data "bzr_monitor_config.json:." bzr_monitor.py -y

# Create a zip archive
cd dist
zip -r ../releases/bzr_monitor-v1.1.0-macos-x64.zip bzr_monitor.app/
```

#### Linux
```bash
# Install dependencies
python3 -m pip install -r requirements.txt
python3 -m pip install pyinstaller

# Build executable
python3 -m PyInstaller --name "bzr_monitor" --onefile --windowed \
    --add-data "bzr_monitor_config.json:." bzr_monitor.py -y

# Create a tar archive
cd dist
tar -czf ../releases/bzr_monitor-v1.1.0-linux-x64.tar.gz bzr_monitor
```

### Cross-Platform Distributions

For developers or users who prefer to run from source:

```bash
# Wheel distribution
python setup.py bdist_wheel

# Source distribution
python setup.py sdist
```

These are located in `dist/` and can be installed with:
```bash
# From wheel
pip install dist/bzr_monitor-1.1.0-py3-none-any.whl

# From source
pip install dist/bzr_monitor-1.1.0.tar.gz
```

### Versioning and Tagging

When creating a new release:

1. Update version numbers in `setup.py` and `bzr_monitor.py` (if applicable)
2. Commit changes: `git commit -am "Release v1.1.0"`
3. Create a tag: `git tag -a v1.1.0 -m "Release version 1.1.0"`
4. Push to repository:
   ```bash
   git push origin main
   git push origin --tags
   ```

### Release Packages

All release artifacts should be committed to the `releases/` directory:
- `bzr_monitor-v1.1.0-windows-x64.zip` - Windows standalone executable
- `bzr_monitor-v1.1.0-macos-x64.zip` - macOS standalone application
- `bzr_monitor-v1.1.0-linux-x64.tar.gz` - Linux standalone executable
- `bzr_monitor-1.1.0-py3-none-any.whl` - Python wheel (cross-platform)
- `bzr_monitor-1.1.0.tar.gz` - Python source distribution
