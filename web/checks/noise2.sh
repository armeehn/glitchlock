set -u
cd /home/user/glitchlock; GL=./.venv/bin/glitchlock; D=/tmp/glnoise2; rm -rf $D; mkdir -p $D
S=${1:-$RANDOM}; echo "seed $S"
Q="-v error -y"
# Pure random noise video, luma-only and full-chroma, as a rawvideo source
ffgac $Q -f lavfi -i "nullsrc=s=320x240:r=25:d=3,geq=random($S)*255:128:128" -pix_fmt yuv420p -c:v rawvideo $D/luma.nut 2>/dev/null
ffgac $Q -f lavfi -i "nullsrc=s=320x240:r=25:d=3,geq=random($S+1)*255:random($S+2)*255:random($S+3)*255" -pix_fmt yuv420p -c:v rawvideo $D/rgb.nut 2>/dev/null
# Noise moving over a real structure: motion estimation has something to find
ffgac $Q -f lavfi -i "testsrc2=s=320x240:r=25:d=3" -f lavfi -i "nullsrc=s=320x240:r=25:d=3,geq=random($S+4)*255:128:128" -filter_complex "[0][1]blend=all_mode=average" -pix_fmt yuv420p -c:v rawvideo $D/mixed.nut 2>/dev/null
enc () { # name src codec extra fmt
  ffgac $Q -i $D/$2.nut -c:v $3 $4 -f $5 $D/$1.mpg 2>/dev/null || echo "ENCODE FAILED $1"
}
for s in luma rgb mixed; do
  enc ${s}_m2_g12      $s mpeg2video "-g 12 -qscale:v 6"           mpeg2video
  enc ${s}_m2_bf2      $s mpeg2video "-g 12 -bf 2 -qscale:v 6"     mpeg2video
  enc ${s}_m2_q31      $s mpeg2video "-g 12 -qscale:v 31"          mpeg2video
  enc ${s}_m2_q2       $s mpeg2video "-g 12 -qscale:v 2"           mpeg2video
  enc ${s}_m1_g12      $s mpeg1video "-g 12 -qscale:v 6"           mpeg1video
  enc ${s}_m4_g12      $s mpeg4      "-g 12 -qscale:v 6"           m4v
  enc ${s}_m4_bf2      $s mpeg4      "-g 12 -bf 2 -qscale:v 6"     m4v
done
for f in $D/*.mpg; do
  out=$($GL verify --repeat 12 "$f" 2>&1 | grep -vE "^\[")
  feat=$(echo "$out" | grep -E "^features:" | tr -s ' '); res=$(echo "$out" | grep -E "^result:" | tr -s ' ')
  [ -z "$res" ] && res="ERROR: $(echo "$out" | tail -1 | cut -c1-90)"
  printf "%-16s %8d B  %-22s %s\n" "$(basename $f .mpg)" "$(stat -c %s $f)" "$feat" "$res"
done
