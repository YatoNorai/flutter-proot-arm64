#!/usr/bin/env python3
"""
sysroot.py — Fallback Debian ARM64 sysroot builder.

Used when the Flutter engine's bundled install-sysroot.py is not available.
Downloads .deb packages from a Debian/Ubuntu mirror and extracts them into
a local sysroot directory for cross-compilation.

The primary approach (via engine's own script) downloads an official
Debian Bullseye ARM64 sysroot from Google Cloud Storage, which gives
better compatibility guarantees. This module is the fallback.
"""

import os
import gzip
import asyncio
import pathlib
import subprocess
import tempfile
import urllib.parse
from loguru import logger

try:
    import aiohttp
    _HAS_AIOHTTP = True
except ImportError:
    _HAS_AIOHTTP = False

# Map Flutter arch names → Debian arch names
ARCH_MAP = {
    "arm64": "arm64",
    "arm":   "armhf",
    "x64":   "amd64",
    "x86":   "i386",
}


# ── Async HTTP helpers ─────────────────────────────────────────────────────────

async def _fetch_bytes(sess, url: str) -> bytes:
    async with sess.get(url) as resp:
        resp.raise_for_status()
        return await resp.read()


async def _download_file(sess, url: str, dst: pathlib.Path) -> pathlib.Path:
    logger.debug(f"  ↓ {url}")
    data = await _fetch_bytes(sess, url)
    dst.write_bytes(data)
    return dst


async def _fetch_package_index(sess, repo: str, dist: str, arch: str) -> dict[str, dict]:
    """
    Download and parse the Packages list from a Debian-style APT repository.
    Returns {package_name: {Package, Filename, Version, ...}}.
    """
    base = repo.rstrip("/")
    candidates = [
        f"{base}/dists/{dist}/main/binary-{arch}/Packages.gz",
        f"{base}/dists/{dist}/main/binary-{arch}/Packages",
    ]
    text = None
    for url in candidates:
        try:
            data = await _fetch_bytes(sess, url)
            text = gzip.decompress(data).decode() if url.endswith(".gz") else data.decode()
            logger.debug(f"Package index fetched from {url}")
            break
        except Exception:
            continue

    if text is None:
        raise RuntimeError(
            f"Cannot fetch package index from {repo!r} "
            f"dist={dist!r} arch={arch!r}"
        )

    # Parse stanza-format Packages file
    packages: dict[str, dict] = {}
    current: dict[str, str] = {}
    for line in text.splitlines():
        if line == "":
            if "Package" in current and "Filename" in current:
                packages[current["Package"]] = current
            current = {}
        elif ": " in line:
            key, _, val = line.partition(": ")
            current[key.strip()] = val.strip()
    # Last stanza (file may not end with blank line)
    if "Package" in current and "Filename" in current:
        packages[current["Package"]] = current

    return packages


async def _download_packages(
    sess,
    tmp_dir: pathlib.Path,
    arch: str,
    sources: list[dict],
) -> list[pathlib.Path]:
    """Resolve and download all requested packages from all sources."""
    downloaded: list[pathlib.Path] = []

    for src in sources:
        repo = src["repo"]
        dist = src["dist"]
        pkgs = src["pkgs"]

        logger.info(f"Fetching package index: {repo} [{dist}] [{arch}]")
        index = await _fetch_package_index(sess, repo, dist, arch)

        tasks = []
        for pkg in pkgs:
            if pkg not in index:
                logger.warning(f"  ✗ Package not found: {pkg}")
                continue
            rel_path = index[pkg]["Filename"]
            url = urllib.parse.urljoin(repo.rstrip("/") + "/", rel_path)
            dst = tmp_dir / f"{pkg}.deb"
            tasks.append(_download_file(sess, url, dst))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                logger.warning(f"  Download error: {r}")
            else:
                downloaded.append(r)
                logger.info(f"  ✓ {r.name}")

    return downloaded


# ── Sysroot extraction ─────────────────────────────────────────────────────────

def _extract_deb(deb: pathlib.Path, sysroot: pathlib.Path) -> None:
    """Extract a .deb file into the sysroot directory using dpkg."""
    result = subprocess.run(
        ["dpkg", "-x", str(deb), str(sysroot)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        logger.warning(f"  ✗ Failed to extract {deb.name}: {result.stderr.strip()}")
    else:
        logger.debug(f"  ✓ {deb.name}")


def _fix_symlinks(sysroot: pathlib.Path) -> None:
    """
    Rewrite absolute symlinks to be relative to the sysroot root.

    When .deb files are extracted, symlinks like /usr/lib/... remain
    absolute, which breaks the sysroot when used from a different path.
    This function makes them relative so the sysroot is self-contained.
    """
    fixed = 0
    broken = 0
    for path in sysroot.rglob("*"):
        if not path.is_symlink():
            continue
        target = os.readlink(path)
        if not os.path.isabs(target):
            continue  # already relative
        # Convert: /usr/lib/foo → <sysroot>/usr/lib/foo
        new_target = sysroot / target.lstrip("/")
        if new_target.exists():
            rel = os.path.relpath(new_target, path.parent)
            path.unlink()
            path.symlink_to(rel)
            fixed += 1
        else:
            broken += 1

    logger.info(f"Symlink fix: {fixed} fixed, {broken} unresolvable (may be ok)")


def _ensure_libpthread(sysroot: pathlib.Path) -> None:
    """
    Create a stub libpthread.a that redirects to libc.
    Required for linking against glibc on some toolchains.
    """
    for lib_dir in sysroot.rglob("lib"):
        if lib_dir.is_dir():
            stub = lib_dir / "libpthread.a"
            if not stub.exists():
                stub.write_bytes(b"INPUT(-lc)\n")


# ── Public API ─────────────────────────────────────────────────────────────────

class DebianSysroot:
    """
    Build a Debian-based cross-compilation sysroot for the given arch.

    Configuration in config.toml:
      [sysroot]
      path = "./sysroot"

      [sysroot.debian-bookworm]       # arbitrary name
      repo = "http://deb.debian.org/debian/"
      dist = "bookworm"
      pkgs = ["libgtk-3-dev", ...]
    """

    def __init__(self, path: str | pathlib.Path, **sources):
        self.path = pathlib.Path(path).expanduser().resolve()
        self.sources: list[dict] = []

        self.path.mkdir(parents=True, exist_ok=True)

        for _name, src in sources.items():
            if isinstance(src, dict) and "repo" in src and "pkgs" in src:
                self.sources.append(src)

    def __call__(self, arch: str = "arm64") -> None:
        """Download and extract packages to build the sysroot for `arch`."""
        if not _HAS_AIOHTTP:
            raise ImportError(
                "aiohttp is required for sysroot building. "
                "Run: pip install aiohttp"
            )

        debian_arch = ARCH_MAP.get(arch, arch)

        # Skip if sysroot already has content
        if self.path.is_dir() and any(self.path.iterdir()):
            logger.info(f"Sysroot already exists at {self.path}, skipping.")
            return

        if not self.sources:
            logger.warning("No sysroot sources defined in config.toml — skipping.")
            return

        logger.info(f"Building Debian {debian_arch} sysroot at {self.path} ...")

        timeout = aiohttp.ClientTimeout(total=600)

        async def _run():
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = pathlib.Path(tmp)
                    debs = await _download_packages(
                        sess, tmp_path, debian_arch, self.sources
                    )
                    logger.info(f"Extracting {len(debs)} packages ...")
                    for deb in debs:
                        _extract_deb(deb, self.path)

        asyncio.run(_run())
        _fix_symlinks(self.path)
        _ensure_libpthread(self.path)
        logger.info(f"✓ Sysroot built at {self.path}")

    def __str__(self) -> str:
        return str(self.path)


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import tomllib
    import fire

    with open("config.toml", "rb") as f:
        cfg = tomllib.load(f)

    sysroot_cfg = cfg.get("sysroot", {})
    path = sysroot_cfg.pop("path", "./sysroot")
    sources = {k: v for k, v in sysroot_cfg.items() if isinstance(v, dict)}

    fire.Fire(DebianSysroot(path, **sources))
