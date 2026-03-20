#!/usr/bin/env bash
# flutter-proot-install.sh
# ─────────────────────────────────────────────────────────────────────────────
# Run this script ONCE after extracting the Flutter SDK tarball into proot.
# It ensures the SDK is wired up correctly so `flutter doctor` passes
# without downloading anything.
#
# Usage (from inside proot):
#   bash /opt/flutter/flutter-proot-install.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Colors ────────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC}  $*"; }
fail() { echo -e "${RED}✗${NC} $*"; exit 1; }

# ── Locate Flutter SDK root ───────────────────────────────────────────────────
FLUTTER_SDK="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FLUTTER_BIN="${FLUTTER_SDK}/bin"
FLUTTER_CACHE="${FLUTTER_BIN}/cache"
DART_SDK="${FLUTTER_CACHE}/dart-sdk"
ENGINE_VERSION_FILE="${FLUTTER_BIN}/internal/engine.version"

echo ""
echo "Flutter proot SDK installer"
echo "SDK path: ${FLUTTER_SDK}"
echo ""

# ── Sanity checks ─────────────────────────────────────────────────────────────
[[ -f "${FLUTTER_BIN}/flutter" ]]          || fail "Not a Flutter SDK directory: ${FLUTTER_SDK}"
[[ -f "${ENGINE_VERSION_FILE}" ]]          || fail "engine.version not found"
[[ -d "${DART_SDK}" ]]                     || fail "Dart SDK not found in cache (bin/cache/dart-sdk/)"
[[ -d "${FLUTTER_CACHE}/artifacts" ]]      || fail "Engine artifacts not found in cache"

ENGINE_VERSION="$(cat "${ENGINE_VERSION_FILE}")"
ok "Engine version: ${ENGINE_VERSION:0:12}..."

# ── Fix file permissions ───────────────────────────────────────────────────────
echo ""
echo "Fixing permissions ..."

chmod +x "${FLUTTER_BIN}/flutter" 2>/dev/null || true
chmod +x "${FLUTTER_BIN}/dart"    2>/dev/null || true

# Make all binaries in dart-sdk executable
find "${DART_SDK}/bin" -type f -exec chmod +x {} \; 2>/dev/null || true

# Make all engine binaries executable
find "${FLUTTER_CACHE}/artifacts" -type f \
    \( -name "*.so" -o -name "flutter_*" -o -name "dart" -o -name "gen_snapshot" \
       -o -name "font-subset" -o -name "impellerc" -o -name "flutter_tester" \) \
    -exec chmod +x {} \; 2>/dev/null || true

ok "Permissions fixed"

# ── Verify/recreate stamp files ───────────────────────────────────────────────
echo ""
echo "Verifying stamp files ..."

STAMPS=(
    "${FLUTTER_CACHE}/engine-dart-sdk.stamp"
    "${FLUTTER_CACHE}/flutter_sdk.stamp"
    "${FLUTTER_CACHE}/font-subset.stamp"
    "${FLUTTER_CACHE}/linux-sdk.stamp"
    "${FLUTTER_CACHE}/dart-sdk.stamp"
)

STAMPS_OK=true
for stamp in "${STAMPS[@]}"; do
    if [[ ! -f "${stamp}" ]]; then
        warn "Missing stamp: $(basename "${stamp}") — recreating"
        echo "${ENGINE_VERSION}" > "${stamp}"
        STAMPS_OK=false
    fi
done

"${STAMPS_OK}" && ok "All stamps present" || ok "Stamps recreated"

# ── Add to PATH ───────────────────────────────────────────────────────────────
echo ""
SHELL_RC=""
if [[ -n "${BASH_VERSION:-}" ]]; then
    SHELL_RC="${HOME}/.bashrc"
elif [[ -n "${ZSH_VERSION:-}" ]]; then
    SHELL_RC="${HOME}/.zshrc"
fi

FLUTTER_PATH_LINE="export PATH=\"\$PATH:${FLUTTER_BIN}\""

add_to_path() {
    local rc="$1"
    if [[ -f "${rc}" ]]; then
        if grep -q "${FLUTTER_BIN}" "${rc}" 2>/dev/null; then
            ok "PATH already set in ${rc}"
        else
            echo "" >> "${rc}"
            echo "# Flutter SDK (proot)" >> "${rc}"
            echo "${FLUTTER_PATH_LINE}" >> "${rc}"
            ok "Added Flutter to PATH in ${rc}"
        fi
    fi
}

[[ -n "${SHELL_RC}" ]] && add_to_path "${SHELL_RC}"
add_to_path "${HOME}/.profile"

# ── Check required system libraries ───────────────────────────────────────────
echo ""
echo "Checking system library dependencies ..."

MISSING_LIBS=()
for lib in libgtk-3.so.0 libglib-2.0.so.0 libGL.so.1; do
    if ! ldconfig -p 2>/dev/null | grep -q "${lib}" && \
       ! find /usr/lib /lib -name "${lib}" 2>/dev/null | grep -q .; then
        MISSING_LIBS+=("${lib}")
    fi
done

if [[ ${#MISSING_LIBS[@]} -gt 0 ]]; then
    warn "Some libraries may be missing: ${MISSING_LIBS[*]}"
    echo ""
    echo "  Install them with:"
    echo "    apt-get install -y libgtk-3-0 libglib2.0-0 libgl1-mesa-glx"
else
    ok "System libraries look good"
fi

# ── Summary ────────────────────────────────────────────────────────────────────
echo ""
echo "─────────────────────────────────────────────────────────"
ok "Flutter SDK installed for proot!"
echo ""
echo "  Next steps:"
echo "    1. Reload your shell:  source ~/.bashrc  (or open a new terminal)"
echo "    2. Verify:             flutter doctor"
echo "    3. Create a project:   flutter create my_app"
echo ""
echo "  Run Linux apps with Termux:X11:"
echo "    DISPLAY=:0 flutter run -d linux"
echo "─────────────────────────────────────────────────────────"
