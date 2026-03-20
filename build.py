#!/usr/bin/env python3
"""
flutter-proot-arm64 — Build Flutter + Dart SDK for Linux ARM64 (proot)
Supports: Debian/Ubuntu proot environments with glibc

Usage:
  python3 build.py                          # full pipeline (uses config.toml)
  python3 build.py resolve_version          # print resolved version and exit
  python3 build.py clone                    # clone flutter only
  python3 build.py sync                     # gclient sync only
  python3 build.py install_sysroot          # install ARM64 sysroot only
  python3 build.py configure                # GN configure only
  python3 build.py build                    # ninja build only
  python3 build.py assemble                 # assemble SDK + create stamps
  python3 build.py package                  # package as tarball
"""

import os
import sys
import shutil
import subprocess
import tomllib
import requests
import fire
import git
from pathlib import Path
from loguru import logger

# ── Config loading ─────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent.resolve()

def load_config(conf: str = "config.toml") -> dict:
    with open(ROOT / conf, "rb") as f:
        return tomllib.load(f)

# ── Architecture mapping ───────────────────────────────────────────────────────

# Flutter arch name → Debian/GNU triplet arch
ARCH_DEBIAN = {"arm64": "arm64", "arm": "armhf", "x64": "amd64", "x86": "i386"}
# Flutter arch name → GNU triplet prefix
ARCH_TRIPLET = {"arm64": "aarch64-linux-gnu", "arm": "arm-linux-gnueabihf"}
# Flutter arch name → GN cpu name
ARCH_GN = {"arm64": "arm64", "arm": "arm", "x64": "x64", "x86": "x86"}

# ── Version resolution ─────────────────────────────────────────────────────────

FLUTTER_RELEASES_URL = (
    "https://storage.googleapis.com/flutter_infra_release/releases/releases_linux.json"
)

def _resolve_channel(channel: str) -> str:
    """Resolve 'stable' / 'beta' to the actual latest version tag."""
    logger.info(f"Fetching latest version for channel '{channel}' ...")
    resp = requests.get(FLUTTER_RELEASES_URL, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    current_hash = data["current_release"].get(channel)
    if not current_hash:
        raise ValueError(
            f"Unknown channel: '{channel}'. Valid: stable, beta.\n"
            "For 'main'/'master' use the branch name directly."
        )
    for release in data["releases"]:
        if release["hash"] == current_hash:
            version = release["version"].lstrip("v")
            logger.info(f"Resolved channel '{channel}' → {version}")
            return version

    raise RuntimeError(f"Cannot resolve version for channel: {channel}")

def resolve_version(version: str = "") -> str:
    """
    Resolve a version string to a concrete Flutter tag.

    Accepts:
      - "" or None            → use config.toml default (channel or explicit version)
      - "stable" / "beta"     → fetch latest release from that channel
      - "main" / "master"     → use the branch name as-is (for gclient)
      - "3.29.2" / "v3.29.2"  → strip leading 'v', use as-is
    """
    cfg = load_config()
    flutter_cfg = cfg.get("flutter", {})

    # Prefer CLI input over config file
    v = version or flutter_cfg.get("version") or flutter_cfg.get("channel") or "stable"
    v = v.strip()

    if v in ("stable", "beta"):
        return _resolve_channel(v)
    if v in ("main", "master"):
        return v  # use branch directly
    # specific tag
    return v.lstrip("v")

# ── Git helpers ────────────────────────────────────────────────────────────────

class _Progress(git.RemoteProgress):
    def update(self, op_code, cur_count, max_count=None, message=""):
        if message:
            logger.trace(f"  {cur_count}/{max_count} {message}")

def _current_tag(path: Path) -> str | None:
    """Return the current flutter tag of a checkout, or None."""
    if not path.is_dir():
        return None
    try:
        return git.Repo(path).git.describe("--tag", "--abbrev=0")
    except git.exc.GitCommandError:
        return None

# ── Build directory helpers ────────────────────────────────────────────────────

def _flutter_root(cfg: dict) -> Path:
    return ROOT / cfg["flutter"]["path"].lstrip("./")

def _engine_root(cfg: dict) -> Path:
    return _flutter_root(cfg) / "engine" / "src"

def _out_dir(cfg: dict, arch: str, mode: str) -> Path:
    """Returns the ninja output directory for a given arch + mode."""
    return _engine_root(cfg) / "out" / f"linux_{mode}_{arch}"

# ── Pipeline steps ─────────────────────────────────────────────────────────────

def clone(version: str = "", cfg_path: str = "config.toml") -> None:
    """Clone the Flutter framework repository at the given version."""
    cfg = load_config(cfg_path)
    flutter_cfg = cfg["flutter"]
    out = _flutter_root(cfg)
    repo_url = flutter_cfg.get("repo", "https://github.com/flutter/flutter")
    ver = version or resolve_version()

    current = _current_tag(out)
    if current == ver:
        logger.info(f"Flutter {ver} already cloned at {out}, skipping.")
        return
    if out.is_dir():
        backup = Path(str(out) + ".old")
        logger.warning(f"Moving existing checkout → {backup}")
        shutil.move(str(out), str(backup))

    logger.info(f"Cloning flutter/flutter @ {ver} → {out} ...")
    try:
        git.Repo.clone_from(
            url=repo_url,
            to_path=str(out),
            branch=ver,
            progress=_Progress(),
        )
    except git.exc.GitCommandError as exc:
        raise RuntimeError(f"Failed to clone Flutter: {exc}") from exc
    logger.info("✓ Flutter cloned.")

def sync(cfg_path: str = "config.toml") -> None:
    """Run gclient sync to fetch engine source, Dart, and all dependencies."""
    cfg = load_config(cfg_path)
    flutter_root = _flutter_root(cfg)
    gclient_src = ROOT / cfg["build"].get("gclient", ".gclient")

    # Copy our .gclient into the flutter checkout directory
    dst = flutter_root / ".gclient"
    shutil.copy(str(gclient_src), str(dst))
    logger.info(f"Copied .gclient to {dst}")

    cmd = ["gclient", "sync", "-DR", "--no-history"]
    logger.info(f"Running: {' '.join(cmd)}")
    subprocess.run(cmd, cwd=str(flutter_root), check=True)
    logger.info("✓ gclient sync complete.")

def install_sysroot(arch: str = "arm64", cfg_path: str = "config.toml") -> Path:
    """
    Install the ARM64 Debian sysroot for cross-compilation.

    Tries the Flutter engine's own install-sysroot.py first (downloads an
    official Debian Bullseye arm64 sysroot from Google Cloud Storage).
    Falls back to downloading Debian packages directly.

    Returns the sysroot path.
    """
    cfg = load_config(cfg_path)
    flutter_root = _flutter_root(cfg)
    engine_src = _engine_root(cfg)

    # ── Strategy 1: use Flutter engine's bundled sysroot installer ──────────
    sysroot_script = engine_src / "build" / "linux" / "sysroot_scripts" / "install-sysroot.py"
    # Flutter ≥3.19 may place the sysroot under a different name (bullseye/bookworm)
    for sysroot_name in [
        f"debian_bullseye_{arch}-sysroot",
        f"debian_bookworm_{arch}-sysroot",
    ]:
        candidate = engine_src / "build" / "linux" / sysroot_name
        if candidate.is_dir() and any(candidate.iterdir()):
            logger.info(f"Sysroot already present: {candidate}")
            return candidate

    if sysroot_script.exists():
        gn_arch = ARCH_GN.get(arch, arch)
        logger.info(f"Installing sysroot via engine script (arch={arch}) ...")
        subprocess.run(
            ["python3", str(sysroot_script), "--arch", gn_arch],
            cwd=str(flutter_root),
            check=True,
        )
        # Locate the installed sysroot
        for sysroot_name in [
            f"debian_bullseye_{arch}-sysroot",
            f"debian_bookworm_{arch}-sysroot",
        ]:
            candidate = engine_src / "build" / "linux" / sysroot_name
            if candidate.is_dir():
                logger.info(f"✓ Sysroot installed at {candidate}")
                return candidate

    # ── Strategy 2: download Debian packages and extract ────────────────────
    logger.warning("Engine sysroot script not found; using fallback sysroot builder.")
    from sysroot import DebianSysroot

    sysroot_cfg = cfg.get("sysroot", {})
    sysroot_path = ROOT / sysroot_cfg.get("path", "./sysroot") / arch
    sources = {k: v for k, v in sysroot_cfg.items() if isinstance(v, dict)}

    builder = DebianSysroot(sysroot_path, **sources)
    builder(arch=arch)
    return sysroot_path

def _find_sysroot(arch: str, cfg: dict) -> Path | None:
    """Locate a previously installed sysroot."""
    engine_src = _engine_root(cfg)
    for name in [
        f"debian_bullseye_{arch}-sysroot",
        f"debian_bookworm_{arch}-sysroot",
    ]:
        p = engine_src / "build" / "linux" / name
        if p.is_dir() and any(p.iterdir()):
            return p
    # Fallback sysroot
    sysroot_cfg = cfg.get("sysroot", {})
    fallback = ROOT / sysroot_cfg.get("path", "./sysroot") / arch
    if fallback.is_dir() and any(fallback.iterdir()):
        return fallback
    return None

def configure(
    arch: str = "arm64",
    runtimes: str = "debug,release,profile",
    lto: bool = False,
    cfg_path: str = "config.toml",
) -> None:
    """Run GN (generate ninja build files) for each requested runtime."""
    cfg = load_config(cfg_path)
    flutter_root = _flutter_root(cfg)
    build_cfg = cfg.get("build", {})

    arch = arch or build_cfg.get("arch", ["arm64"])[0]
    modes = [m.strip() for m in runtimes.split(",")]
    gn_arch = ARCH_GN.get(arch, arch)

    sysroot = _find_sysroot(arch, cfg)
    if not sysroot:
        raise RuntimeError(
            f"Sysroot for {arch} not found. Run 'python3 build.py install_sysroot' first."
        )
    logger.info(f"Using sysroot: {sysroot}")

    # Detect number of jobs
    jobs = build_cfg.get("jobs", 0) or os.cpu_count() or 4

    for mode in modes:
        logger.info(f"Configuring {arch}/{mode} ...")
        cmd = [
            "vpython3",
            "engine/src/flutter/tools/gn",
            "--linux",
            "--linux-cpu", gn_arch,
            "--no-goma",
            "--clang",
            "--no-prebuilt-dart-sdk",      # build Dart from source (embedded in engine)
            "--runtime-mode", mode,
            "--enable-fontconfig",
            "--no-backtrace",
            "--no-enable-unittests",
            "--no-build-embedder-examples",
            "--no-build-glfw-shell",        # GTK shell only
            # GN extra args
            "--gn-args", "symbol_level=0",
            "--gn-args", "dart_platform_sdk=true",
            "--gn-args", "dart_include_wasm_opt=false",
            "--gn-args", "dart_support_perfetto=false",
            "--gn-args", "skia_use_perfetto=false",
            "--gn-args", f'custom_sysroot="{sysroot}"',
            "--gn-args", "use_default_linux_sysroot=false",
        ]
        if lto:
            cmd.append("--lto")

        subprocess.run(cmd, cwd=str(flutter_root), check=True)
        logger.info(f"✓ Configured {arch}/{mode}")

def build(
    arch: str = "arm64",
    runtimes: str = "debug,release,profile",
    jobs: int = 0,
    cfg_path: str = "config.toml",
) -> None:
    """Run ninja to build the Flutter engine and Dart SDK."""
    cfg = load_config(cfg_path)
    flutter_root = _flutter_root(cfg)
    build_cfg = cfg.get("build", {})

    arch = arch or build_cfg.get("arch", ["arm64"])[0]
    modes = [m.strip() for m in runtimes.split(",")]
    _jobs = jobs or build_cfg.get("jobs", 0) or os.cpu_count() or 4

    for mode in modes:
        out = _out_dir(cfg, arch, mode)
        logger.info(f"Building {arch}/{mode} → {out} (jobs={_jobs}) ...")
        cmd = ["ninja", "-C", str(out), "flutter", f"-j{_jobs}"]
        subprocess.run(cmd, check=True)
        logger.info(f"✓ Built {arch}/{mode}")

def assemble(
    arch: str = "arm64",
    runtimes: str = "debug,release,profile",
    cfg_path: str = "config.toml",
) -> None:
    """
    Copy build artifacts into Flutter SDK cache structure and create stamp files.

    After this step, running `flutter doctor` inside proot will NOT try to
    re-download any SDK components.
    """
    from assemble import assemble_sdk

    cfg = load_config(cfg_path)
    flutter_root = _flutter_root(cfg)
    modes = [m.strip() for m in runtimes.split(",")]

    assemble_sdk(flutter_root=flutter_root, arch=arch, modes=modes)

def package(
    version: str = "",
    arch: str = "arm64",
    cfg_path: str = "config.toml",
) -> Path:
    """Create a redistributable tarball of the assembled Flutter SDK."""
    cfg = load_config(cfg_path)
    flutter_root = _flutter_root(cfg)
    pkg_cfg = cfg.get("package", {})

    version = version or resolve_version()
    pkg_name = pkg_cfg.get("name", "flutter-{version}-linux-{arch}").format(
        version=version, arch=arch
    )
    fmt = pkg_cfg.get("format", "tar.gz")
    out_path = ROOT / f"{pkg_name}.{fmt}"

    # Remove engine source to reduce size before packaging
    # (only keep the Flutter SDK directory)
    engine_src = _engine_root(cfg)
    if engine_src.is_dir():
        logger.info(f"Removing engine source ({engine_src}) to reduce package size ...")
        shutil.rmtree(str(engine_src))

    # Copy install script into the SDK
    install_src = ROOT / "install.sh"
    install_dst = flutter_root / "flutter-proot-install.sh"
    if install_src.exists():
        shutil.copy(str(install_src), str(install_dst))
        os.chmod(str(install_dst), 0o755)

    logger.info(f"Packaging {flutter_root} → {out_path} ...")
    if fmt == "tar.gz":
        subprocess.run(
            ["tar", "-czf", str(out_path), "-C", str(flutter_root.parent), flutter_root.name],
            check=True,
        )
    elif fmt == "zip":
        shutil.make_archive(str(out_path.with_suffix("")), "zip", str(flutter_root.parent), flutter_root.name)
    else:
        raise ValueError(f"Unknown format: {fmt}")

    size_mb = out_path.stat().st_size / 1024 / 1024
    logger.info(f"✓ Package created: {out_path} ({size_mb:.1f} MB)")
    return out_path

# ── Full pipeline ──────────────────────────────────────────────────────────────

def run(
    flutter_version: str = "",
    arch: str = "",
    runtimes: str = "",
    lto: bool = False,
    cfg_path: str = "config.toml",
) -> None:
    """
    Run the full build pipeline:
      resolve → clone → sync → install_sysroot → configure → build → assemble → package
    """
    cfg = load_config(cfg_path)
    build_cfg = cfg.get("build", {})

    # Resolve inputs with config fallback
    _arch = arch or build_cfg.get("arch", ["arm64"])[0]
    _runtimes = runtimes or ",".join(build_cfg.get("runtime", ["debug", "release", "profile"]))
    _version = resolve_version(flutter_version)

    logger.info("=" * 60)
    logger.info(f"  Flutter version : {_version}")
    logger.info(f"  Target arch     : {_arch}")
    logger.info(f"  Runtimes        : {_runtimes}")
    logger.info(f"  LTO             : {lto}")
    logger.info("=" * 60)

    clone(version=_version, cfg_path=cfg_path)
    sync(cfg_path=cfg_path)
    install_sysroot(arch=_arch, cfg_path=cfg_path)
    configure(arch=_arch, runtimes=_runtimes, lto=lto, cfg_path=cfg_path)
    build(arch=_arch, runtimes=_runtimes, cfg_path=cfg_path)
    assemble(arch=_arch, runtimes=_runtimes, cfg_path=cfg_path)
    package(version=_version, arch=_arch, cfg_path=cfg_path)

    logger.success("🎉 Flutter SDK for proot built successfully!")

# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.remove()
    logger.add(
        sys.stdout,
        diagnose=False,
        colorize=True,
        format=(
            "<green>{time:HH:mm:ss}</green> | "
            "<level>{level:<9}</level> | "
            "<level>{message}</level>"
        ),
    )

    # Expose all public functions via fire CLI
    fire.Fire({
        "run": run,
        "resolve_version": resolve_version,
        "clone": clone,
        "sync": sync,
        "install_sysroot": install_sysroot,
        "configure": configure,
        "build": build,
        "assemble": assemble,
        "package": package,
    })
