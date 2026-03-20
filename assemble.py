#!/usr/bin/env python3
"""
assemble.py — Flutter SDK assembler for proot.

Copies build artifacts from the ninja output directories into the correct
Flutter SDK cache structure, then creates stamp files so that the `flutter`
tool will NOT attempt to re-download any components when run inside proot.

Flutter's artifact-download logic (packages/flutter_tools/lib/src/cache.dart)
checks stamp files in `bin/cache/` against `bin/internal/engine.version`.
If stamps are present and match, Flutter skips all downloads — which is exactly
what we want for a self-contained proot SDK.
"""

import os
import shutil
import pathlib
from loguru import logger


# ── Helpers ────────────────────────────────────────────────────────────────────

def _engine_version(flutter_root: pathlib.Path) -> str:
    """Read the expected engine commit hash from the Flutter SDK."""
    version_file = flutter_root / "bin" / "internal" / "engine.version"
    if not version_file.exists():
        raise FileNotFoundError(f"engine.version not found: {version_file}")
    return version_file.read_text().strip()


def _out_dir(flutter_root: pathlib.Path, arch: str, mode: str) -> pathlib.Path:
    """Return the ninja output directory for a given arch + mode."""
    return flutter_root / "engine" / "src" / "out" / f"linux_{mode}_{arch}"


def _copy(src: pathlib.Path, dst: pathlib.Path, name: str = "") -> bool:
    """Copy a file, logging success or warning. Returns True if copied."""
    label = name or src.name
    if not src.exists():
        logger.warning(f"    ✗ {label} not found")
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(src), str(dst))
    logger.info(f"    ✓ {label}")
    return True


def _copy_dir(src: pathlib.Path, dst: pathlib.Path, name: str = "") -> bool:
    """Copy a directory tree. Returns True if copied."""
    label = name or src.name
    if not src.exists():
        logger.warning(f"    ✗ {label}/ not found")
        return False
    if dst.exists():
        shutil.rmtree(str(dst))
    shutil.copytree(str(src), str(dst))
    logger.info(f"    ✓ {label}/")
    return True


# ── Per-mode artifact lists ────────────────────────────────────────────────────

# Files that exist in ALL build modes
_COMMON_ARTIFACTS = [
    "libflutter_linux_gtk.so",
    "icudtl.dat",
    "flutter_linux",
]

# Extra files only in debug build
_DEBUG_ONLY_ARTIFACTS = [
    "flutter_tester",
    "font-subset",
    "impellerc",
    "libpath_ops.so",
    "libtessellator.so",
]

# Snapshot files (under gen/ subdirectory) — debug only
_GEN_ARTIFACTS = [
    ("gen/frontend_server_aot.dart.snapshot", "frontend_server_aot.dart.snapshot"),
    ("gen/const_finder.dart.snapshot",        "const_finder.dart.snapshot"),
    ("gen/flutter/lib/snapshot/isolate_snapshot.bin",    "isolate_snapshot.bin"),
    ("gen/flutter/lib/snapshot/vm_isolate_snapshot.bin", "vm_isolate_snapshot.bin"),
]

# Flutter cache stamp files to create
_STAMP_FILES = [
    "engine-dart-sdk.stamp",
    "flutter_sdk.stamp",
    "font-subset.stamp",
    "linux-sdk.stamp",
]


# ── Main assembler ─────────────────────────────────────────────────────────────

def assemble_sdk(
    flutter_root: pathlib.Path,
    arch: str,
    modes: list[str],
) -> None:
    """
    Assemble built artifacts into Flutter SDK cache structure.

    Directory layout produced:
      flutter/
        bin/
          cache/
            dart-sdk/                          ← ARM64 Dart SDK (built from source)
            engine-dart-sdk.stamp              ← prevents dart-sdk re-download
            flutter_sdk.stamp                  ← prevents flutter tool re-download
            font-subset.stamp                  ← prevents font-subset re-download
            linux-sdk.stamp                    ← prevents linux engine re-download
            artifacts/
              engine/
                common/
                  flutter_patched_sdk/         ← debug patched SDK
                  flutter_patched_sdk_product/ ← release patched SDK
                linux-{arch}/                  ← debug artifacts
                linux-{arch}-release/          ← release artifacts
                linux-{arch}-profile/          ← profile artifacts
    """
    flutter_root = pathlib.Path(flutter_root).resolve()
    cache_dir    = flutter_root / "bin" / "cache"
    engine_dir   = cache_dir / "artifacts" / "engine"
    common_dir   = engine_dir / "common"

    engine_ver = _engine_version(flutter_root)
    logger.info(f"Engine version hash: {engine_ver[:12]}...")

    # ── Per-mode artifacts ────────────────────────────────────────────────────
    for mode in modes:
        out = _out_dir(flutter_root, arch, mode)
        if not out.exists():
            logger.warning(f"  Build output not found for {arch}/{mode}: {out}")
            continue

        logger.info(f"  Assembling {arch}/{mode} ...")

        # Target artifact directory in cache
        if mode == "debug":
            artifact_dir = engine_dir / f"linux-{arch}"
        else:
            artifact_dir = engine_dir / f"linux-{arch}-{mode}"
        artifact_dir.mkdir(parents=True, exist_ok=True)

        # Common artifacts (all modes)
        for name in _COMMON_ARTIFACTS:
            _copy(out / name, artifact_dir / name)

        # gen_snapshot — lives in clang_x64/ (host binary used by build system)
        _copy(out / "clang_x64" / "gen_snapshot", artifact_dir / "gen_snapshot", "gen_snapshot (host)")

        # Debug-only artifacts
        if mode == "debug":
            for name in _DEBUG_ONLY_ARTIFACTS:
                _copy(out / name, artifact_dir / name)
            for src_rel, dst_name in _GEN_ARTIFACTS:
                _copy(out / src_rel, artifact_dir / dst_name)
            # Shader library (directory)
            shader_src = (
                flutter_root / "engine" / "src" / "flutter"
                / "impeller" / "compiler" / "shader_lib"
            )
            _copy_dir(shader_src, artifact_dir / "shader_lib", "shader_lib")

    # ── flutter_patched_sdk (from debug build) ───────────────────────────────
    debug_out = _out_dir(flutter_root, arch, "debug")
    if debug_out.exists():
        sdk_src = debug_out / "flutter_patched_sdk"
        sdk_dst = common_dir / "flutter_patched_sdk"
        if sdk_src.exists():
            sdk_dst.mkdir(parents=True, exist_ok=True)
            for f in ["platform_strong.dill", "vm_outline_strong.dill"]:
                _copy(sdk_src / f, sdk_dst / f)
            logger.info("    ✓ flutter_patched_sdk")

    # ── flutter_patched_sdk_product (from release build) ─────────────────────
    release_out = _out_dir(flutter_root, arch, "release")
    if release_out.exists():
        sdk_prod_src = release_out / "flutter_patched_sdk"
        sdk_prod_dst = common_dir / "flutter_patched_sdk_product"
        if sdk_prod_src.exists():
            sdk_prod_dst.mkdir(parents=True, exist_ok=True)
            for f in ["platform_strong.dill", "vm_outline_strong.dill"]:
                _copy(sdk_prod_src / f, sdk_prod_dst / f)
            logger.info("    ✓ flutter_patched_sdk_product")

    # ── Dart SDK (from debug build) ───────────────────────────────────────────
    dart_sdk_src = debug_out / "dart-sdk"
    dart_sdk_dst = cache_dir / "dart-sdk"
    if dart_sdk_src.exists():
        _copy_dir(dart_sdk_src, dart_sdk_dst, "dart-sdk")
    else:
        logger.warning(
            "    ✗ dart-sdk not found in build output. "
            "Ensure --no-prebuilt-dart-sdk was used in configure."
        )

    # ── Sky engine Dart package ───────────────────────────────────────────────
    sky_engine_src = debug_out / "gen" / "dart-pkg" / "sky_engine"
    sky_engine_dst = cache_dir / "pkg" / "sky_engine"
    if sky_engine_src.exists():
        _copy_dir(sky_engine_src, sky_engine_dst, "sky_engine pkg")

    # ── Create stamp files (prevent re-download) ──────────────────────────────
    _create_stamps(cache_dir, engine_ver, arch, modes)

    logger.info("✓ SDK assembly complete.")


def _create_stamps(
    cache_dir: pathlib.Path,
    engine_version: str,
    arch: str,
    modes: list[str],
) -> None:
    """
    Write stamp files so Flutter won't try to download artifacts it already has.

    Flutter reads bin/internal/engine.version and compares it against stamp
    file contents.  When they match, it considers that component up-to-date.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Top-level stamps
    for stamp_name in _STAMP_FILES:
        p = cache_dir / stamp_name
        p.write_text(engine_version)
        logger.info(f"    ✓ stamp: {stamp_name}")

    # Per-platform stamps (Flutter checks these per artifact type)
    artifact_engine_dir = cache_dir / "artifacts" / "engine"
    for mode in modes:
        if mode == "debug":
            platform_dir = artifact_engine_dir / f"linux-{arch}"
        else:
            platform_dir = artifact_engine_dir / f"linux-{arch}-{mode}"
        platform_dir.mkdir(parents=True, exist_ok=True)
        stamp = platform_dir / f"linux-{arch}{'' if mode == 'debug' else '-' + mode}.stamp"
        stamp.write_text(engine_version)
        logger.info(f"    ✓ stamp: artifacts/engine/{platform_dir.name}/{stamp.name}")

    # Common dir stamps
    common_dir = artifact_engine_dir / "common"
    common_dir.mkdir(parents=True, exist_ok=True)
    for stamp_name in ["flutter_patched_sdk.stamp", "flutter_patched_sdk_product.stamp"]:
        (common_dir / stamp_name).write_text(engine_version)
        logger.info(f"    ✓ stamp: artifacts/engine/common/{stamp_name}")

    # Dart SDK stamp
    (cache_dir / "dart-sdk.stamp").write_text(engine_version)
    logger.info("    ✓ stamp: dart-sdk.stamp")

    logger.info(f"✓ All stamps written with engine hash {engine_version[:12]}...")
