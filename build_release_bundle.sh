#!/usr/bin/env bash
# Author: @dvkrum
# Build a release tree that older transactional updaters can validate while
# keeping obsolete built-in modules out of the installed runtime.
set -euo pipefail

DEST="${1:?usage: build_release_bundle.sh DESTINATION}"
rm -rf "$DEST"
mkdir -p "$DEST"
cp -a runtime "$DEST/runtime"
cp -a update_termux.sh termuxbridgectl TUNNEL_CLIENT_VERSION README.md SECURITY.md "$DEST/"

# v1.2.31 and earlier validate five historical runtime paths before they can
# receive the new updater. These are release-archive-only symlink aliases.
# The transactional staging loop uses `find -type f`, so aliases are not copied
# into the immutable installed generation.
cat > "$DEST/runtime/compat_removed_builtin.py" <<'PY'
"""Compatibility target for obsolete updater aliases.

This module is not imported by TermuxBridge and contains no domain logic.
"""
REMOVED_BUILTIN_COMPAT = True
PY
cat > "$DEST/runtime/compat_runtime_marker.py" <<'PY'
"""Generic release compatibility marker for older updater manifests."""
RELEASE_COMPAT_SCHEMA = 1
PY

for legacy in \
  gallery_bridge_tools.py \
  gallery_intelligence.py \
  gallery_ocr.py \
  gallery_shizuku.py
do
  ln -s compat_removed_builtin.py "$DEST/runtime/$legacy"
done

# Historical updater v1.2.31 also requires at least 21 regular Python helpers.
regular_count="$(find "$DEST/runtime" -maxdepth 1 -type f -name '*.py' | wc -l)"
[[ "$regular_count" -ge 21 ]] || {
  echo "release bundle has too few regular runtime helpers: $regular_count" >&2
  exit 1
}
