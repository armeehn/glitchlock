#!/bin/bash
# Rebuild every carrier the checks need. Runs inside LXC 111, the only host with
# FFglitch. Fixed seeds, so the files are the same every time.
#
# FFedit needs an ELEMENTARY stream (-f mpeg2video / m4v; ffprobe says
# "mpegvideo"). Hand it an MPEG *program* stream (-f mpeg) and every feature
# disappears with a message that reads like a codec problem.
set -eu
OUT="$(cd "$(dirname "$0")" && pwd)/fixtures"
mkdir -p "$OUT"
W=/tmp/glitchlock-fixtures; mkdir -p $W
Q="-v error -y"

noise () { ffgac $Q -f lavfi -i "nullsrc=s=$2:r=25:d=3,geq=random($3)*255:128:128" -pix_fmt yuv420p -c:v rawvideo "$1" 2>/dev/null; }

noise $W/n320.nut 320x240 4242
noise $W/n160.nut 160x128 7

# A lockable noise carrier: MPEG-2 keeps qscale as well as motion vectors.
ffgac $Q -i $W/n320.nut -c:v mpeg2video -g 12 -qscale:v 6 -f mpeg2video "$OUT/noise_ok.mpg" 2>/dev/null

# Pure noise as MPEG-4: motion estimation finds nothing, so the stream carries
# no motion vectors at all and a lock would be a no-op. Must be refused.
ffgac $Q -i $W/n160.nut -c:v mpeg4 -g 12 -qscale:v 6 -f m4v "$OUT/noise_mpeg4.mpg" 2>/dev/null

# All-intra MPEG-4: same no-op, reached a different way (GOP of 1).
ffgac $Q -f lavfi -i "testsrc2=s=320x240:r=25:d=3" -c:v mpeg4 -g 1 -qscale:v 6 -f m4v "$OUT/noop.mpg" 2>/dev/null

# Trips an FFglitch 0.10.2 crash (heap corruption) on some MPEG-2 B-frame
# streams. Upstream, not glitchlock, and NOT noise-specific. The service must
# turn it into a clean 400.
ffgac $Q -f lavfi -i "testsrc2=s=320x240:r=25:d=3" -c:v mpeg2video -g 12 -bf 2 -qscale:v 6 -f mpeg2video "$OUT/crash_bf2.mpg" 2>/dev/null

# A lockable carrier WITH B-frames, for the recipient-mode round trip. Backward
# vectors are coded against bcode, not fcode, so this is not the same path as a
# B-frame-free carrier.
#
# It is MPEG-4 on purpose. Every MPEG-2 B-frame encode ffgac produces here
# crashes ffedit (bf 1/2/3, g 6..25, qscale 4..10, closed GOP, 176x144 all
# abort or segfault), while MPEG-4 with the same B-frame count is stable. An
# older hand-made MPEG-2 B-frame carrier is also stable, so the trigger is
# something about the file, not "MPEG-2 plus B-frames" as such. Do not
# "simplify" this back to mpeg2video.
ffgac $Q -f lavfi -i "testsrc2=s=320x240:r=25:d=3" -c:v mpeg4 -g 12 -bf 2 -qscale:v 6 -f m4v "$OUT/bf.mpg" 2>/dev/null

GL=/home/user/glitchlock/.venv/bin/glitchlock
for f in "$OUT"/*.mpg; do
  printf '%-22s %8d B  ' "$(basename "$f")" "$(stat -c %s "$f")"
  "$GL" inspect "$f" > $W/i.txt 2>&1 && sed -n 's/^glitchlock would use: *//p' $W/i.txt | tr -d '\n' || echo -n "inspect failed"
  echo
done
echo
echo "bf.mpg and noise_ok.mpg must list features, or the round-trip checks have"
echo "nothing to lock. crash_bf2.mpg is expected to CRASH ffedit: that is what"
echo "it is for. The service must turn that crash into a clean 400."
