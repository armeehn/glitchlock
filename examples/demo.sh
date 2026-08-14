#!/usr/bin/env bash
# End-to-end demo: generate a clip, lock it, unlock it, prove it came back.
#
# Requires FFglitch (ffedit, ffgac) on PATH and glitchlock installed.
set -euo pipefail

WORK="${1:-./glitchlock-demo}"
mkdir -p "$WORK"
cd "$WORK"

echo "==> generating a source clip"
ffgac -v error -y -f lavfi \
  -i "testsrc2=size=640x480:rate=25:duration=10" \
  -vf "rotate=0.35*sin(2*PI*t/4):c=black" \
  -pix_fmt yuv420p source.mp4

echo "==> preparing a glitchable carrier"
glitchlock prepare source.mp4 -o carrier.mpg

echo "==> what can be locked here?"
glitchlock inspect carrier.mpg

echo "==> locking"
head -c 64 /dev/urandom > key.bin
glitchlock lock carrier.mpg -o locked.mpg -m manifest.json --key-file key.bin

echo "==> unlocking"
glitchlock unlock locked.mpg -o restored.mpg -m manifest.json --key-file key.bin

echo "==> digests"
sha256sum carrier.mpg locked.mpg restored.mpg

if cmp -s carrier.mpg restored.mpg; then
  echo "PASS: restored file is byte-identical to the carrier"
else
  echo "FAIL: restored file differs from the carrier" >&2
  exit 1
fi

echo "==> pulling a frame from each for a side-by-side look"
for f in carrier locked restored; do
  ffgac -v error -y -i "$f.mpg" -vf "select=eq(n\,140),scale=320:240" \
    -frames:v 1 "frame_$f.png"
done
echo "wrote frame_carrier.png frame_locked.png frame_restored.png in $PWD"
