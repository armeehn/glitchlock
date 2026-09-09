set -u
cd /home/user/glitchlock; GL=./.venv/bin/glitchlock; D=/tmp/glsweep; rm -rf $D; mkdir -p $D
PASS=0; FAIL=0; NOOP=0
echo "seed  size      dur codec       q  gop mode        result"
for i in $(seq 1 10); do
  S=$RANDOM
  W=$(( (RANDOM % 12 + 4) * 16 ))          # 64..240, multiple of 16
  H=$(( (RANDOM % 12 + 4) * 16 ))
  DUR=$(( RANDOM % 3 + 2 ))                 # 2..4 s
  Q=$(( RANDOM % 30 + 2 ))                  # 2..31
  G=$(( RANDOM % 30 + 2 ))                  # 2..31
  case $((RANDOM % 3)) in 0) C=mpeg2video; F=mpeg2video;; 1) C=mpeg1video; F=mpeg1video;; 2) C=mpeg4; F=m4v;; esac
  case $((RANDOM % 3)) in 0) M=full;; 1) M=substitute;; 2) M=permute;; esac
  src=$D/n$i.nut; car=$D/n$i.mpg
  # random noise in all three planes, random static seed per plane
  ffgac -v error -y -f lavfi -i "nullsrc=s=${W}x${H}:r=25:d=$DUR,geq=random($S)*255:random($((S+1)))*255:random($((S+2)))*255" -pix_fmt yuv420p -c:v rawvideo $src 2>/dev/null
  ffgac -v error -y -i $src -c:v $C -g $G -qscale:v $Q -f $F $car 2>/dev/null
  out=$($GL verify --repeat 12 --mode $M "$car" 2>&1 | grep -vE "^\[")
  res=$(echo "$out" | grep -E "^result:" | tr -s ' ' | cut -d' ' -f2)
  [ -z "$res" ] && res="ERR:$(echo "$out" | tail -1 | cut -c1-60)"
  case "$res" in EXACT=12) PASS=$((PASS+1));; NO-OP=*) NOOP=$((NOOP+1));; *) FAIL=$((FAIL+1));; esac
  printf "%5d %5dx%-4d %ds %-11s %2d %3d %-11s %s\n" "$S" "$W" "$H" "$DUR" "$C" "$Q" "$G" "$M" "$res"
done
echo "---- EXACT=$PASS  NO-OP=$NOOP  FAIL=$FAIL"
exit $((FAIL > 0))
