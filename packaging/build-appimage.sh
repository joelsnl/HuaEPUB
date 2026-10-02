#!/bin/sh
# Wrap the PyInstaller binary from build.py in an AppImage.
# Requires python3 and appimagetool on PATH.
# https://github.com/AppImage/appimagetool
set -eu
cd "$(dirname "$0")/.."

APP=HuaEPUB
ROOT=$(pwd)
DIST="$ROOT/dist/$APP"
APPDIR="$ROOT/appimage-build/$APP.appdir"
OUT="$ROOT/$APP-linux.AppImage"
DESKTOP=org.joelsnl.HuaEPUB.desktop

command -v python3 >/dev/null
command -v appimagetool >/dev/null

python3 build.py
test -f "$DIST"

rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" \
  "$APPDIR/usr/share/applications" \
  "$APPDIR/usr/share/icons/hicolor/scalable/apps" \
  "$APPDIR/usr/share/metainfo"

cp "$DIST" "$APPDIR/usr/bin/$APP"
chmod +x "$APPDIR/usr/bin/$APP"
cp packaging/org.joelsnl.HuaEPUB.svg "$APPDIR/org.joelsnl.HuaEPUB.svg"
cp packaging/org.joelsnl.HuaEPUB.svg \
  "$APPDIR/usr/share/icons/hicolor/scalable/apps/org.joelsnl.HuaEPUB.svg"
ver=$(tr -d '[:space:]' < VERSION)
sed "s/@VERSION@/${ver}/" packaging/org.joelsnl.HuaEPUB.metainfo.xml \
  > "$APPDIR/usr/share/metainfo/org.joelsnl.HuaEPUB.metainfo.xml"
sed 's/^Exec=.*/Exec=HuaEPUB %U/' packaging/org.joelsnl.HuaEPUB.desktop \
  > "$APPDIR/$DESKTOP"
cp "$APPDIR/$DESKTOP" "$APPDIR/usr/share/applications/$DESKTOP"

cat > "$APPDIR/AppRun" << 'EOF'
#!/bin/sh
HERE=$(dirname "$(readlink -f "$0")")
exec "$HERE/usr/bin/HuaEPUB" "$@"
EOF
chmod +x "$APPDIR/AppRun"

ARCH=$(uname -m) appimagetool "$APPDIR" "$OUT"
chmod +x "$OUT"
echo "$OUT"
