#!/bin/bash
# Proof that the checks can actually fail: run them against the PRE-FIX code
# kept in /opt/rollback on LXC 117, on scratch port 8099 (not proxied, not live).
# Expect FAIL here and PASS against the live service. A pass here means the
# check is not testing what it claims to.
cd "$(dirname "$0")" || exit 1
BACKUP="${1:-/opt/rollback/server.py-20260909}"   # -20260909b = after fix 1, before fix 2
echo "negative control against $BACKUP"
pct exec 117 -- bash -c "cp $BACKUP /tmp/oldsrv.py; PATH=/opt/ffglitch:/usr/local/bin:/usr/bin:/bin \
  GLITCHLOCK_PORT=8099 GLITCHLOCK_RETENTION=3600 setsid /opt/glitchlock-venv/bin/python /tmp/oldsrv.py \
  >/tmp/oldsrv.log 2>&1 < /dev/null & sleep 4; curl -s -o /dev/null -w 'scratch :8099 -> %{http_code}\n' http://127.0.0.1:8099/health"
pct exec 117 -- mkdir -p /tmp/nc/fixtures
for f in test_noop.py test_badmanifest.py; do pct push 117 "$PWD/$f" "/tmp/nc/$f"; done
for f in noop.mpg noise_ok.mpg; do pct push 117 "$PWD/fixtures/$f" "/tmp/nc/fixtures/$f"; done
for t in test_noop.py test_badmanifest.py; do
  echo "--- $t against pre-fix code (expect FAIL)"
  pct exec 117 -- bash -c "cd /tmp/nc && GLITCHLOCK_HOST=127.0.0.1:8099 /opt/glitchlock-venv/bin/python $t"
  echo "    exit=$?"
done
# bracketed pattern so pkill cannot match its own command line
pct exec 117 -- bash -c 'pkill -f "[o]ldsrv.py"; sleep 1; rm -rf /tmp/nc /tmp/oldsrv.py; echo cleaned up'
