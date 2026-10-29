#!/usr/bin/env bash
set -euo pipefail
if (( $# != 1 )); then echo "Usage: build-site.sh EMPTY_OUTPUT_DIRECTORY" >&2; exit 2; fi
src="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dst="$1"
if [[ -e "$dst" ]]; then echo "Refusing to overwrite existing output" >&2; exit 3; fi
mkdir -p "$dst"
for f in _worker.js index.html; do
  test -s "$src/$f"; cp -- "$src/$f" "$dst/$f"
done
node --check "$dst/_worker.js"
printf 'Built 2 allowlisted Mode 2 Pages assets in %s\n' "$dst"
