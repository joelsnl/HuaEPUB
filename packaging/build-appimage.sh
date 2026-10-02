#!/bin/bash
#
# Build HuaEPUB as an AppImage for Linux
# This script assumes you have Python 3.10+ and PyInstaller installed
#
# Usage:
#   ./build-appimage.sh [version]
#
# Prerequisites:
#   - Install dependencies: pip install -r requirements.txt pyinstaller
#   - Install AppImage tools: wget https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage
#     chmod +x appimagetool-x86_64.AppImage && mv appimagetool-x86_64.AppImage /usr/local/bin/
#

set -e

# Configuration
APP_NAME="HuaEPUB"
VERSION="${1:-2.15.0}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
BUILD_DIR="$PROJECT_ROOT/appimage-build"
DIST_DIR="$PROJECT_ROOT/dist"
APPIMAGE_OUTPUT="$PROJECT_ROOT/${APP_NAME}-linux.AppImage"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}=== HuaEPUB AppImage Builder ===${NC}"
echo "Version: $VERSION"
echo "Project Root: $PROJECT_ROOT"

# Check if PyInstaller is installed
if ! python -c "import PyInstaller" 2>/dev/null; then
    echo -e "${YELLOW}PyInstaller not found. Installing...${NC}"
    pip install pyinstaller
fi

# Clean previous builds
echo -e "${GREEN}Cleaning previous builds...${NC}"
rm -rf "$BUILD_DIR" "$DIST_DIR"
mkdir -p "$BUILD_DIR"

# Build the executable
echo -e "${GREEN}Building standalone executable with PyInstaller...${NC}"
cd "$PROJECT_ROOT"
python build.py

if [ ! -f "$DIST_DIR/HuaEPUB" ]; then
    echo -e "${RED}Error: Executable not found in $DIST_DIR/HuaEPUB${NC}"
    exit 1
fi

echo -e "${GREEN}Executable built successfully!${NC}"

# Create AppDir structure
echo -e "${GREEN}Creating AppDir structure...${NC}"
cd "$BUILD_DIR"

mkdir -p HuaEPUB.appdir/usr/bin
mkdir -p HuaEPUB.appdir/usr/share/applications
mkdir -p HuaEPUB.appdir/usr/share/icons/hicolor/scalable/apps
mkdir -p HuaEPUB.appdir/usr/share/metainfo

# Copy executable
cp "$DIST_DIR/HuaEPUB" HuaEPUB.appdir/usr/bin/
chmod +x HuaEPUB.appdir/usr/bin/HuaEPUB

# Copy desktop file (if exists)
if [ -f "$SCRIPT_DIR/org.joelsnl.HuaEPUB.desktop" ]; then
    cp "$SCRIPT_DIR/org.joelsnl.HuaEPUB.desktop" HuaEPUB.appdir/usr/share/applications/
    sed -i 's|^Exec=.*|Exec=huaepub %U|' HuaEPUB.appdir/usr/share/applications/org.joelsnl.HuaEPUB.desktop
fi

# Copy icon (if exists)
if [ -f "$SCRIPT_DIR/org.joelsnl.HuaEPUB.svg" ]; then
    # Convert SVG to PNG for better compatibility, or copy as-is
    cp "$SCRIPT_DIR/org.joelsnl.HuaEPUB.svg" HuaEPUB.appdir/usr/share/icons/hicolor/scalable/apps/org.joelsnl.HuaEPUB.svg
fi

# Create AppRun script
cat > HuaEPUB.appdir/AppRun << 'EOF'
#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export QT_QPA_PLATFORM=xcb
exec "$SCRIPT_DIR/usr/bin/HuaEPUB" "$@"
EOF
chmod +x HuaEPUB.appdir/AppRun

# Create .desktop file for AppImage
cat > HuaEPUB.appdir/org.joelsnl.HuaEPUB.desktop << EOF
[Desktop Entry]
Name=HuaEPUB
Comment=Download and translate Chinese web novels to EPUB format
Exec=huaepub %U
Icon=org.joelsnl.HuaEPUB
Terminal=false
Type=Application
Categories=Utility;Education;
Keywords=webnovel;ebook;translator;epub;chinese;
MimeType=application/epub+zip;
StartupNotify=true
EOF

# Create AppStream metadata
cat > HuaEPUB.appdir/usr/share/metainfo/org.joelsnl.HuaEPUB.metainfo.xml << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<component type="desktop-application">
  <id>org.joelsnl.HuaEPUB</id>
  <name>HuaEPUB</name>
  <summary>Download and translate Chinese web novels to EPUB format</summary>
  <description>
    <p>HuaEPUB is a desktop application that downloads Chinese web novels, translates them to English, and builds ready-to-read EPUB files.</p>
    <ul>
      <li>Downloads from 100+ novel websites</li>
      <li>Multiple translation options (Google, LibreTranslate, Ollama, Offline NMT)</li>
      <li>Local AI polishing for better English quality</li>
      <li>Automatic watermark and ad removal</li>
      <li>Library management with update tracking</li>
      <li>In-app EPUB reader</li>
      <li>Optional Google Drive sync across devices</li>
    </ul>
  </description>
  <launchable type="desktop-id">org.joelsnl.HuaEPUB.desktop</launchable>
  <url type="homepage">https://github.com/joelsnl/HuaEPUB</url>
  <provides>
    <binary>huaepub</binary>
  </provides>
  <releases>
    <release version="2.15.0" date="2026-10-02">
      <description>Latest stable release</description>
    </release>
  </releases>
  <content_rating type="oars-1.1"/>
</component>
EOF

# Create AppImage
echo -e "${GREEN}Building AppImage...${NC}"
cd "$BUILD_DIR"

# Check if appimagetool is available
if command -v appimagetool &> /dev/null; then
    echo "Using system appimagetool..."
    APPIMAGE_TOOL=$(command -v appimagetool)
elif [ -f "$PROJECT_ROOT/appimagetool-x86_64.AppImage" ]; then
    echo "Using local appimagetool..."
    APPIMAGE_TOOL="$PROJECT_ROOT/appimagetool-x86_64.AppImage"
else
    echo -e "${YELLOW}AppImageTool not found. Downloading...${NC}"
    cd "$PROJECT_ROOT"
    wget -q https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage
    chmod +x appimagetool-x86_64.AppImage
    APPIMAGE_TOOL="$PROJECT_ROOT/appimagetool-x86_64.AppImage"
fi

# Build the AppImage
if [ -f "$APPIMAGE_TOOL" ]; then
    "$APPIMAGE_TOOL" "$BUILD_DIR/HuaEPUB.appdir" "$APPIMAGE_OUTPUT" --skip-appstream-check 2>/dev/null || \
    "$APPIMAGE_TOOL" "$BUILD_DIR/HuaEPUB.appdir" "$APPIMAGE_OUTPUT"
    
    if [ -f "$APPIMAGE_OUTPUT" ]; then
        SIZE=$(du -h "$APPIMAGE_OUTPUT" | cut -f1)
        echo -e "${GREEN}=== AppImage built successfully! ===${NC}"
        echo "Output: $APPIMAGE_OUTPUT"
        echo "Size: $SIZE"
        echo -e "${YELLOW}Make it executable: chmod +x $APPIMAGE_OUTPUT${NC}"
    else
        echo -e "${RED}Error: AppImage not created${NC}"
        exit 1
    fi
else
    echo -e "${RED}Error: Could not find appimagetool${NC}"
    echo -e "${YELLOW}Please install AppImageKit or download appimagetool manually${NC}"
    echo "See: https://github.com/AppImage/AppImageKit/wiki#appimagetool"
    exit 1
fi

echo -e "${GREEN}=== Build Complete ===${NC}"
