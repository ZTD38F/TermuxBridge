#!/usr/bin/env bash
set -euo pipefail
if (( $# != 1 )); then echo 'Usage: build-site.sh EMPTY_OUTPUT_DIRECTORY' >&2; exit 2; fi
src="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dst="$1"
if [[ -e "$dst" ]]; then echo 'Refusing to overwrite existing output' >&2; exit 3; fi
mkdir -p "$dst"
for f in _worker.js _headers _redirects index.html style.css app.js; do
  test -s "$src/$f"; cp -- "$src/$f" "$dst/$f"
done
node --check "$dst/_worker.js"
node --check "$dst/app.js"
printf 'Built 6 allowlisted Pages assets in %s
' "$dst"
