#!/bin/bash
# glitchlock proof battery. Run on x:  bash /z1-pool/share/glitchlock/checks/run-all.sh
# Web checks hit the live service at glitchlock.hq. Engine checks run in LXC 111,
# the only host that has the FFglitch binaries.
cd "$(dirname "$0")" || exit 1
PASS=0; FAIL=0

# Carriers are not committed; rebuild them on first run. They must be built on
# the host that has FFglitch, then copied back here.
if [ ! -f fixtures/noise_ok.mpg ]; then
  echo "fixtures missing, rebuilding in LXC 111..."
  pct exec 111 -- su - user -c 'mkdir -p /tmp/mkfix'
  pct push 111 "$PWD/make-fixtures.sh" /tmp/mkfix/make-fixtures.sh
  pct exec 111 -- chown -R user /tmp/mkfix   # pct push writes as root
  pct exec 111 -- su - user -c 'bash /tmp/mkfix/make-fixtures.sh'
  mkdir -p fixtures
  for f in bf.mpg noop.mpg noise_ok.mpg noise_mpeg4.mpg crash_bf2.mpg; do
    pct pull 111 "/tmp/mkfix/fixtures/$f" "fixtures/$f"
  done
fi

mkdir -p fixtures
head -c 400000 /dev/urandom > fixtures/random.bin

LOG=$(mktemp -d)

run () { # label filter-regex command...
  echo; echo "=============================================================="
  echo "  $1"; echo "=============================================================="
  local filter="$2"; shift 2
  "$@" > "$LOG/out" 2>&1; local rc=$?          # status captured BEFORE any pipe
  grep -E "$filter" "$LOG/out" || tail -5 "$LOG/out"
  if [ $rc -eq 0 ]; then PASS=$((PASS+1)); else FAIL=$((FAIL+1)); echo ">>> THIS CHECK FAILED (exit $rc)"; fi
}

in111 () { pct exec 111 -- su - user -c "$1"; }

for f in noise2.sh noise_sweep.sh mixed_sweep.sh fuzz_manifest.py ui.mjs; do
  pct push 111 "$PWD/$f" "/tmp/$f" >/dev/null 2>&1
done
pct push 111 "$PWD/fixtures/noise_ok.mpg" /tmp/fixtures_noise_ok.mpg >/dev/null 2>&1

run "1. Unit suite in LXC 111 (real FFglitch, no skips allowed)" "passed|failed|error" \
    in111 'cd /home/user/glitchlock && ./.venv/bin/python -m pytest -q'
run "2. Web round trip, passphrase (sample -> prepare -> lock -> unlock)" "." python3 e2e.py
run "3. Web round trip, uploaded B-frame carrier + public-key recipient" "." python3 upload.py
run "4. Pure noise MPEG-4 is refused, not silently copied" "." python3 test_noise_refusal.py
run "5. No-op lock returns 400, not 500" "." python3 test_noop.py
run "6. Malformed manifests return 400, not 500 (7 shapes)" "." python3 test_badmanifest.py
run "7. Random bytes / crashing carrier / good noise carrier" "." python3 noise_web.py
run "8. Randomised noise sweep, 10 configs x 12 nonces each" "seed|EXACT|NO-OP|ERR|----" \
    in111 'bash /tmp/noise_sweep.sh'
run "9. Manifest fuzzing, 60 random mutations, none may be accepted" "mutations=|RESULT|ACCEPTED" \
    in111 'cd /home/user/glitchlock && python3 /tmp/fuzz_manifest.py /tmp/fixtures_noise_ok.mpg 60'
run "10. Real browser, headless Chromium in LXC 111" "title|verdict|wrong key|RESULT" \
    in111 'node /tmp/ui.mjs'

echo; echo "=============================================================="
echo "  CHECKS PASSED: $PASS    FAILED: $FAIL"
echo "=============================================================="
exit $((FAIL > 0))
