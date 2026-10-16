#!/usr/bin/env bash
set -euo pipefail

KRITA_VERSION="6.0.4"
KRITA_URL="https://download.kde.org/stable/krita/${KRITA_VERSION}/krita-${KRITA_VERSION}-x86_64.AppImage"
KRITA_SHA256="4ec9a45892308134b0ff5410f30aa9e68ae58d0dae5fb122666cfd27dfd6de9f"

CACHE_DIR="${1:-$HOME/.cache/krita-appimage}"
INSTALL_DIR="${2:-/usr/local/bin}"

mkdir -p "$CACHE_DIR"
APPIMAGE="$CACHE_DIR/krita-${KRITA_VERSION}-x86_64.AppImage"

if [ ! -f "$APPIMAGE" ]; then
    echo "Downloading Krita ${KRITA_VERSION} AppImage..."
    curl -fSL --retry 5 --retry-connrefused -o "$APPIMAGE" "$KRITA_URL"
fi

echo "Verifying SHA256 checksum..."
echo "${KRITA_SHA256}  ${APPIMAGE}" | sha256sum -c -

chmod +x "$APPIMAGE"

EXTRACT_DIR="$CACHE_DIR/squashfs-root"
if [ ! -d "$EXTRACT_DIR" ]; then
    echo "Extracting AppImage..."
    (cd "$CACHE_DIR" && "$APPIMAGE" --appimage-extract > /dev/null)
fi

mkdir -p "$INSTALL_DIR"
WRAPPER="$INSTALL_DIR/krita"
echo "Creating krita wrapper at ${WRAPPER}..."
cat << 'EOF' > "$WRAPPER"
#!/usr/bin/env bash
APPDIR="__EXTRACT_DIR__"
export PYTHONHOME="$APPDIR/usr"
export PYTHONPATH="$APPDIR/usr/lib/python3.13:$APPDIR/usr/lib/python3.13/site-packages:$APPDIR/usr/lib/krita-python-libs"
exec "$APPDIR/AppRun" "$@"
EOF
sed -i "s|__EXTRACT_DIR__|${EXTRACT_DIR}|g" "$WRAPPER"
chmod +x "$WRAPPER"

echo "Krita setup complete: $("$WRAPPER" --version 2>&1 | tail -n 1)"
