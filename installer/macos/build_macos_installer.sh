#!/bin/sh
set -eu

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

VERSION="0.2.2"
APP_NAME="AI Photo Sorter"
IDENTIFIER="com.local.aiphotosorter"
BUILD_VENV="$ROOT/.build-venv"
PKG_ROOT="$ROOT/build/pkgroot"
PKG_BUILD="$ROOT/build/macos-pkg"
PKG_RESOURCES="$ROOT/installer/macos/resources"
PKG_DISTRIBUTION="$ROOT/build/macos-distribution.xml"
RELEASE="$ROOT/release"

if [ ! -d "$BUILD_VENV" ]; then
  python3 -m venv "$BUILD_VENV"
fi

"$BUILD_VENV/bin/python" -m pip install --upgrade pip
"$BUILD_VENV/bin/python" -m pip install -r "$ROOT/requirements.txt" pyinstaller
"$BUILD_VENV/bin/python" -m PyInstaller --clean --noconfirm "$ROOT/installer/pyinstaller/AIPhotoSorter.spec"

rm -rf "$PKG_ROOT" "$PKG_BUILD"
mkdir -p "$PKG_ROOT/Applications" "$PKG_BUILD" "$RELEASE"
rm -f "$RELEASE/AIPhotoSorter.pkg" "$RELEASE/AIPhotoSorterInstaller.dmg"
chmod +x "$ROOT/installer/macos/scripts/postinstall"
cp -R "$ROOT/dist/$APP_NAME.app" "$PKG_ROOT/Applications/"

pkgbuild \
  --root "$PKG_ROOT" \
  --scripts "$ROOT/installer/macos/scripts" \
  --identifier "$IDENTIFIER" \
  --version "$VERSION" \
  --install-location "/" \
  "$PKG_BUILD/AIPhotoSorterComponent.pkg"

cat > "$PKG_DISTRIBUTION" <<EOF
<?xml version="1.0" encoding="utf-8"?>
<installer-gui-script minSpecVersion="1">
  <title>AI Photo Sorter</title>
  <readme file="ReadMe.html"/>
  <options customize="never" require-scripts="true"/>
  <domains enable_localSystem="true"/>
  <choices-outline>
    <line choice="default"/>
  </choices-outline>
  <choice id="default" visible="false" title="AI Photo Sorter">
    <pkg-ref id="$IDENTIFIER"/>
  </choice>
  <pkg-ref id="$IDENTIFIER" version="$VERSION" onConclusion="none">AIPhotoSorterComponent.pkg</pkg-ref>
</installer-gui-script>
EOF

productbuild \
  --distribution "$PKG_DISTRIBUTION" \
  --resources "$PKG_RESOURCES" \
  --package-path "$PKG_BUILD" \
  "$RELEASE/AIPhotoSorter.pkg"

hdiutil create \
  -volname "$APP_NAME" \
  -srcfolder "$RELEASE/AIPhotoSorter.pkg" \
  -ov \
  -format UDZO \
  "$RELEASE/AIPhotoSorterInstaller.dmg"

echo "Created $RELEASE/AIPhotoSorter.pkg"
echo "Created $RELEASE/AIPhotoSorterInstaller.dmg"
