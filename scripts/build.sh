#!/usr/bin/env bash
# Build the HamClock WebAssembly client from upstream source plus this repo's patches.
#
#   scripts/build.sh [800x480|1600x960|2400x1440|3200x1920]
#
# Environment:
#   UPSTREAM_URL   upstream repo     (default https://github.com/openhamclock/hamclock.git)
#   UPSTREAM_REF   commit to patch   (default: the commit the patches were made against)
#   STRIP=1        strip debug info with wasm-opt (about 9.5 MB -> 1.7 MB)
#   JOBS           parallel make jobs (default: number of CPUs)
#
# Needs Emscripten on PATH (source emsdk_env.sh first), git, make and python3.
# Output: dist/<size>/  - serve it with:  python3 dist/<size>/devserver.py

set -euo pipefail

SIZE="${1:-800x480}"
UPSTREAM_URL="${UPSTREAM_URL:-https://github.com/openhamclock/hamclock.git}"
UPSTREAM_REF="${UPSTREAM_REF:-b9db5fbe519faf84ee1350019eadb8747f1cd1e1}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/build/hamclock"
OUT="$ROOT/dist/$SIZE"
JOBS="${JOBS:-$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)}"

case "$SIZE" in
    800x480|1600x960|2400x1440|3200x1920) ;;
    *) echo "size must be 800x480, 1600x960, 2400x1440 or 3200x1920" >&2; exit 1 ;;
esac
command -v em++ >/dev/null || { echo "em++ not found: install emsdk and source emsdk_env.sh" >&2; exit 1; }

# fresh upstream tree at the pinned commit, with the patch series applied
if [ ! -d "$SRC/.git" ]; then
    mkdir -p "$ROOT/build"
    git clone --quiet "$UPSTREAM_URL" "$SRC"
fi
git -C "$SRC" fetch --quiet origin
git -C "$SRC" checkout --quiet --force "$UPSTREAM_REF"
git -C "$SRC" clean --quiet -fdx -e ESPHamClock/.wasm-size -e '*.o' -e '*.a'
for p in "$ROOT"/patches/*.patch; do
    echo "applying $(basename "$p")"
    git -C "$SRC" apply "$p"
done

# the sub-Makefiles call plain ar/ranlib, which (on macOS) can't index wasm objects.
# Wrapper scripts, not symlinks: emar finds its python half by its own name.
SHIM="$ROOT/build/emshim"
mkdir -p "$SHIM"
printf '#!/bin/sh\nexec "%s" "$@"\n' "$(command -v emar)"     > "$SHIM/ar"
printf '#!/bin/sh\nexec "%s" "$@"\n' "$(command -v emranlib)" > "$SHIM/ranlib"
chmod +x "$SHIM/ar" "$SHIM/ranlib"

PATH="$SHIM:$PATH" make -C "$SRC/ESPHamClock" -j"$JOBS" hamclock-wasm WASM_SIZE="$SIZE" \
    CXX=em++ CC=emcc AR=emar RANLIB=emranlib

# collect everything servable
rm -rf "$OUT"
mkdir -p "$OUT"
cp "$SRC"/ESPHamClock/wasm/hamclock.* "$OUT"/
cp "$ROOT"/web/index.html "$ROOT"/web/devserver.py "$OUT"/
if [ "${STRIP:-0}" = 1 ]; then
    wasm-opt --enable-threads --enable-bulk-memory --enable-mutable-globals --enable-sign-ext \
        --strip-debug -O2 "$OUT/hamclock.wasm" -o "$OUT/hamclock.wasm"
fi

echo
echo "built $OUT:"
ls -l "$OUT"
echo
echo "run:  python3 $OUT/devserver.py   then open http://localhost:8000/"
