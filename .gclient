solutions = [
  {
    # Flutter framework repo — gclient reads its DEPS file to find the
    # correct engine commit hash automatically. No version is hardcoded here;
    # the version is determined by which Flutter tag was checked out first.
    "managed": False,
    "name": ".",
    "url": "https://github.com/flutter/flutter",
    "deps_file": "DEPS",
    "safesync_url": "",
    "custom_deps": {
      # Skip Fuchsia SDK — not needed for Linux/proot target
      "engine/src/fuchsia/sdk/linux": None,
      # Skip large test datasets
      "engine/src/third_party/google_fonts_for_unit_tests": None,
      # Skip Java/Android toolchain — not needed for Linux target
      "engine/src/flutter/third_party/java/openjdk": None,
    },
    "custom_vars": {
      "setup_githooks":       False,
      "use_cipd_goma":        False,
      "download_emsdk":       False,   # no WASM needed
      "download_dart_sdk":    False,   # we build Dart from source
      "download_linux_deps":  False,   # we manage our own sysroot
      "download_fuchsia_sdk": False,
      "download_android_deps": False,  # no Android target
      "download_windows_deps": False,
      "download_fuchsia_deps": False,
    },
    # No patches: unlike termux-flutter, we target standard Linux (glibc),
    # so Termux-specific patches to Dart/Skia/engine are not needed.
    "custom_hooks": [],
  }
]
