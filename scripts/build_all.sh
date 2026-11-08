#!/usr/bin/env bash
# Build the HamClock WebAssembly client at every size, or at the sizes given.
#
#   scripts/build_all.sh [size ...]          sizes: 800x480 1600x960 2400x1440 3200x1920 (default: all)
#
# Environment (passed through to build.sh):
#   STRIP=1        strip debug info with wasm-opt (about 9.5 MB -> 1.7-1.9 MB per size)
#   JOBS, UPSTREAM_URL, UPSTREAM_REF   as for build.sh
# and for this script:
#   PACKAGE=1      also zip each size as dist/hamclock-wasm-<VERSION>-<size>.zip, for releases.
#                  (Not ZIP=1: zip itself reads $ZIP as default options.)
#   VERSION        version in the zip names (default: this repo's tag, from git describe)
#
# Each size is a full rebuild: the Makefile cleans whenever the size changes, because object files
# don't record which size they were compiled for. Expect a few minutes per size.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SIZES=("$@")
[ ${#SIZES[@]} -gt 0 ] || SIZES=(800x480 1600x960 2400x1440 3200x1920)

# catch a bad size before spending minutes on the good ones
for size in "${SIZES[@]}"; do
    case "$size" in
        800x480|1600x960|2400x1440|3200x1920) ;;
        *) echo "unknown size $size: use 800x480, 1600x960, 2400x1440 or 3200x1920" >&2; exit 1 ;;
    esac
done

if [ "${PACKAGE:-0}" = 1 ]; then
    command -v zip >/dev/null || { echo "PACKAGE=1: zip not found" >&2; exit 1; }
    VERSION="${VERSION:-$(git -C "$ROOT" describe --tags --always --dirty 2>/dev/null || echo dev)}"
fi

start=$SECONDS
for size in "${SIZES[@]}"; do
    echo
    echo "==================== $size ===================="
    "$ROOT/scripts/build.sh" "$size"
    if [ "${PACKAGE:-0}" = 1 ]; then
        zipname="hamclock-wasm-$VERSION-$size.zip"
        rm -f "$ROOT/dist/$zipname"
        (cd "$ROOT/dist" && env -u ZIP -u ZIPOPT zip -qr "$zipname" "$size")
    fi
done

echo
echo "built ${#SIZES[@]} size(s) in $(( (SECONDS - start) / 60 )) min $(( (SECONDS - start) % 60 )) s:"
for size in "${SIZES[@]}"; do
    printf '  %-10s  hamclock.wasm %s bytes\n' "$size" "$(wc -c < "$ROOT/dist/$size/hamclock.wasm" | tr -d ' ')"
done
if [ "${PACKAGE:-0}" = 1 ]; then
    echo "release zips:"
    for size in "${SIZES[@]}"; do
        echo "  dist/hamclock-wasm-$VERSION-$size.zip"
    done
fi
