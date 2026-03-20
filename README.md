# flutter-proot-arm64

Build **Flutter + Dart SDK** from source for **Linux ARM64** — designed to run inside **proot** (proot-distro, chroot, any Debian/Ubuntu ARM64 environment).

> **Key differences from other approaches:**
> - **Dart is compiled from source** (embedded in the Flutter engine build) — no pre-built Dart download needed
> - **Stamp files are created automatically** — `flutter` will never try to re-download artifacts
> - **No hardcoded versions** — resolves `stable`/`beta` to actual tags at build time
> - **Targets standard glibc** — works in any Debian/Ubuntu proot, not Termux-specific

---

## Quick Start (GitHub Actions)

1. **Fork or clone** this repository
2. Go to **Actions** → **Build Flutter SDK for proot (ARM64)**
3. Click **Run workflow** and fill in the inputs:

| Input | Default | Description |
|-------|---------|-------------|
| `flutter_version` | *(empty)* | `stable`, `beta`, `main`, or `3.29.2` — empty uses config.toml |
| `arch` | `arm64` | `arm64` or `arm` |
| `runtimes` | `debug,release,profile` | Comma-separated build modes |
| `lto` | `false` | Enable Link-Time Optimization |

4. Wait for the build to complete (~3–5 hours)
5. Download the `.tar.gz` from the **Releases** page

---

## Using the SDK in proot

```bash
# 1. Extract the SDK (adjust path as needed)
tar -xzf flutter-3.x.y-linux-arm64-proot.tar.gz -C /opt

# 2. Run the install script (fixes permissions, stamps, PATH)
bash /opt/flutter/flutter-proot-install.sh

# 3. Reload shell
source ~/.bashrc

# 4. Verify — this should NOT download anything
flutter doctor

# 5. Create and run a Flutter app
flutter create my_app
cd my_app

# Run as Linux desktop app (requires Termux:X11 or VNC)
DISPLAY=:0 flutter run -d linux

# Run as web server (accessible from phone browser)
flutter run -d web-server --web-port 8080
```

### Required system packages (inside proot)

```bash
# Debian/Ubuntu proot
apt-get install -y \
  libgtk-3-0 \
  libglib2.0-0 \
  libpango1.0-0 \
  libfontconfig1 \
  libfreetype6 \
  libharfbuzz0b \
  libepoxy0 \
  ninja-build \
  cmake \
  clang \
  pkg-config \
  git \
  curl
```

---

## Why stamps matter

When you run `flutter doctor` or `flutter build`, the Flutter tool checks
`bin/cache/*.stamp` files against `bin/internal/engine.version`.  
If stamps are missing or mismatched, Flutter downloads the pre-built artifacts
for your platform from Google's servers — which are **x86_64 only**.

This project creates all required stamps as part of the build, so Flutter sees
everything as already up-to-date and skips all downloads.

Stamps created:
```
bin/cache/
  dart-sdk.stamp
  engine-dart-sdk.stamp
  flutter_sdk.stamp
  font-subset.stamp
  linux-sdk.stamp
  artifacts/engine/
    common/flutter_patched_sdk.stamp
    common/flutter_patched_sdk_product.stamp
    linux-arm64/linux-arm64.stamp
    linux-arm64-release/linux-arm64-release.stamp
    linux-arm64-profile/linux-arm64-profile.stamp
```

---

## Local build (advanced)

You can also build locally on an x86_64 Linux machine:

```bash
# Install dependencies
sudo apt-get install -y ninja-build libfreetype-dev python3 python3-pip
pip install -r requirements.txt

# Install depot_tools (gclient, vpython3, etc.)
git clone https://chromium.googlesource.com/chromium/tools/depot_tools.git
export PATH="$PATH:$(pwd)/depot_tools"

# Run full build pipeline
python3 build.py run --flutter_version=stable --arch=arm64

# Or step by step:
python3 build.py resolve_version          # see what version "stable" maps to
python3 build.py clone --version=3.29.2   # clone Flutter
python3 build.py sync                     # gclient sync (very slow, ~30 min)
python3 build.py install_sysroot          # install ARM64 Debian sysroot
python3 build.py configure                # GN configure
python3 build.py build                    # ninja build (~2–4 hours)
python3 build.py assemble                 # copy artifacts + create stamps
python3 build.py package                  # create tarball
```

### Configuration

All settings are in `config.toml`. CLI flags override config values:

```toml
[flutter]
channel = "stable"    # or "beta", "main", or set version = "3.29.2"
version = ""          # explicit version (overrides channel)

[build]
arch    = ["arm64"]
runtime = ["debug", "release", "profile"]
lto     = false       # set true for smaller release binaries
jobs    = 0           # 0 = auto-detect CPU count

[package]
format = "tar.gz"
name   = "flutter-{version}-linux-{arch}-proot"
```

---

## Architecture

```
flutter-proot-arm64/
├── .github/
│   └── workflows/
│       └── build.yml       # GitHub Actions workflow (workflow_dispatch)
├── build.py                # Main build orchestrator — CLI entry point
├── sysroot.py              # Fallback: downloads Debian arm64 packages
├── assemble.py             # Copies artifacts + creates stamp files
├── config.toml             # All configuration (no hardcoded values)
├── requirements.txt        # Python dependencies
├── .gclient                # gclient config (no version, reads from DEPS)
├── install.sh              # proot installation helper
└── README.md
```

### Build pipeline

```
resolve_version()      → e.g. "stable" → "3.29.2"
clone()                → git clone flutter/flutter @ v3.29.2
sync()                 → gclient sync (fetches engine + Dart source)
install_sysroot()      → Debian Bullseye arm64 sysroot (for cross-compile)
configure()            → vpython3 tools/gn (generate ninja files)
build()                → ninja -C out/linux_{mode}_arm64 flutter
assemble()             → copy artifacts to bin/cache/ + write stamps
package()              → tar.gz with install.sh bundled
```

### Sysroot

Cross-compilation from x86_64 → arm64 requires an ARM64 sysroot containing
glibc headers and libraries.

**Primary:** Uses Flutter engine's own `build/linux/sysroot_scripts/install-sysroot.py`,
which downloads an official Debian Bullseye ARM64 sysroot from Google Cloud Storage.
This is the most reliable approach and produces binaries compatible with most proot distributions.

**Fallback:** Downloads Debian Bookworm ARM64 packages listed in `config.toml`
and extracts them into a local sysroot directory.

---

## Comparison with similar projects

| Feature | This project | termux-flutter | Flutter-SDK-ARM64 |
|---------|-------------|----------------|-------------------|
| Builds Dart from source | ✅ | ✅ | ❌ (downloads pre-built) |
| Targets proot (glibc) | ✅ | ❌ (Termux bionic) | ⚠️ partial |
| No re-download on `flutter doctor` | ✅ | ✅ | ❌ (must re-copy manually) |
| Configurable version | ✅ | ✅ | ❌ (hardcoded) |
| GitHub Actions workflow | ✅ | ✅ | ✅ |
| No hardcoded paths | ✅ | ✅ | ❌ |

---

## License

MIT
