#!/usr/bin/env bash
# Rebuild FFglitch 0.10.2 + the H.264 CAVLC mv patches from the pristine tarball.
#
#   ./build.sh [SRC_DIR] [PREFIX]
#
# SRC_DIR  where the tree is unpacked (default ./ffglitch-h264-build)
# PREFIX   if set, ffedit/ffgac/qjs are copied there (e.g. /opt/ffglitch-h264)
# STATIC=1 link ffedit and ffgac statically (no qjs, no rtmidi/zmq): the
#          build CI downloads as a release asset, since a binary built on
#          Arch wants a newer glibc than the Ubuntu runner image has.
#          Needs a static zlib (libz.a) reachable through
#          --extra-cflags/--extra-ldflags, e.g. ZLIB=/path/to/zlib-install;
#          Arch ships none, so build zlib 1.3.1 with ./configure --static.
#
# Needs: gcc, make, pkg-config, git, curl, xz. No nasm required (asm is
# disabled, which is also why the bundled Xvid encoder is left out).
set -euo pipefail

TARBALL_URL=https://ffglitch.org/pub/src/ffglitch-0.10.2.tar.xz
PATCH_DIR=$(cd "$(dirname "$0")" && pwd)
SRC_DIR=${1:-$PWD/ffglitch-h264-build}
PREFIX=${2:-}
JOBS=${JOBS:-6}

CONFIGURE_FLAGS=(
    --disable-doc --enable-gpl --enable-static --disable-shared
    --disable-autodetect --disable-iconv --enable-zlib
    --disable-libxvid --enable-rtmidi --enable-libzmq
    --disable-x86asm --disable-ffplay
)

mkdir -p "$SRC_DIR"
cd "$SRC_DIR"

if [ ! -f ffglitch-0.10.2.tar.xz ]; then
    curl -sSL -o ffglitch-0.10.2.tar.xz "$TARBALL_URL"
fi
if [ ! -f configure ]; then
    tar -xJf ffglitch-0.10.2.tar.xz --strip-components=1
fi

# pristine commit, then the series on top
if [ ! -d .git ]; then
    git init -q
    git add -A
    git -c user.name=build -c user.email=build@localhost commit -q -m "Import FFglitch 0.10.2 pristine tree"
    git -c user.name=build -c user.email=build@localhost am "$PATCH_DIR"/0*.patch
fi

if [ "${STATIC:-0}" = 1 ]; then
    CONFIGURE_FLAGS=(
        --disable-doc --enable-gpl --enable-static --disable-shared
        --disable-autodetect --disable-iconv --enable-zlib
        --disable-libxvid --disable-libzmq --disable-rtmidi
        --disable-x86asm --disable-ffplay --disable-ffprobe
        --extra-cflags=-I${ZLIB:?set ZLIB to a static zlib prefix}/include
        --extra-ldflags="-static -L$ZLIB/lib" --pkg-config-flags=--static
    )
fi

./configure "${CONFIGURE_FLAGS[@]}"
if [ "${STATIC:-0}" = 1 ]; then
    # --disable-rtmidi still leaves -lasound in the link line, and there is
    # no static ALSA to satisfy it; nothing references it once rtmidi is off.
    sed -i 's/-lasound//g' ffbuild/config.mak
    make -j"$JOBS" ffedit ffgac
    BINS=(ffedit ffgac)
else
    make -j"$JOBS"
    make -j"$JOBS" qjs
    BINS=(ffedit ffgac qjs)
fi

if [ -n "$PREFIX" ]; then
    mkdir -p "$PREFIX"
    cp "${BINS[@]}" "$PREFIX"/
    echo "installed to $PREFIX"
fi
