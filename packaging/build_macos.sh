#!/usr/bin/env bash
# Build AutoReload.app and AutoReload-macos-<arch>.dmg. Run this ON A MAC, from the project root:
#     bash packaging/build_macos.sh
#
# Needs: Python 3.12 from python.org (it ships Tk), and Google Chrome installed (used by the self-test).
# The result matches the Mac it is built on: arm64 on Apple Silicon, x86_64 on Intel.
set -euo pipefail

cd "$(dirname "$0")/.."
ARCH="$(uname -m)"
PY="${PYTHON:-python3}"

if [ "$(uname -s)" != "Darwin" ]; then
  echo "This script builds a macOS app, so it has to run on a Mac." >&2
  exit 1
fi
"$PY" -c "import tkinter" 2>/dev/null || {
  echo "This Python has no Tk. Install Python 3.12 from python.org and re-run (PYTHON=/path/to/python3)." >&2
  exit 1
}

echo "==> Creating build environment ($ARCH)"
rm -rf .venv-build build dist
"$PY" -m venv .venv-build
# shellcheck disable=SC1091
source .venv-build/bin/activate
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt pyinstaller

echo "==> Building AutoReload.app"
python -m PyInstaller --noconfirm --clean --windowed --name AutoReload \
  --osx-bundle-identifier com.autoreload.app \
  --collect-data customtkinter --collect-all playwright \
  --distpath dist --workpath build --specpath build main.py

APP="dist/AutoReload.app"
[ -d "$APP" ] || { echo "Build did not produce $APP" >&2; exit 1; }

echo "==> Self-test of the packaged app (opens Chrome headless for a moment)"
RESULT="$(mktemp)"
"$APP/Contents/MacOS/AutoReload" --selftest "$RESULT" || true
cat "$RESULT"
tail -n 1 "$RESULT" | grep -q "SELFTEST OK" || { echo "Self-test FAILED - not making a .dmg" >&2; exit 1; }

echo "==> Creating the .dmg"
STAGE="$(mktemp -d)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
mkdir -p release/macos
DMG="release/macos/AutoReload-macos-$ARCH.dmg"
hdiutil create -volname "Auto Reload" -srcfolder "$STAGE" -ov -format UDZO "$DMG"

echo
echo "Done: $DMG"
echo "The app is not signed or notarized. On first open, right-click AutoReload.app > Open (then Open again),"
echo "or run:  xattr -dr com.apple.quarantine /Applications/AutoReload.app"
