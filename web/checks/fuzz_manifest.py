"""Randomly mutate a signed manifest; every mutation must be refused, none may unlock."""
import json, os, random, subprocess, sys, tempfile

GL = "/home/user/glitchlock/.venv/bin/glitchlock"
CARRIER = sys.argv[1]
TRIALS = int(sys.argv[2]) if len(sys.argv) > 2 else 30
d = tempfile.mkdtemp()
locked, man = os.path.join(d, "l.mpg"), os.path.join(d, "m.json")
subprocess.run([GL, "lock", CARRIER, "-o", locked, "-m", man, "--password", "pw"],
               check=True, capture_output=True)
base = json.load(open(man))

def mutate(o, rng):
    """Walk to a random leaf and replace it with random content of the same type."""
    path = []
    while isinstance(o, (dict, list)) and o:
        k = rng.choice(list(o.keys()) if isinstance(o, dict) else range(len(o)))
        path.append(k); parent, o = o, o[k]
        if rng.random() < 0.35: break
    if isinstance(o, bool): new = not o
    elif isinstance(o, int): new = o + rng.choice([-1, 1, 1000])
    elif isinstance(o, float): new = o * rng.uniform(0.1, 2.0) or 0.5
    elif isinstance(o, str): new = "".join(rng.choice("0123456789abcdef") for _ in range(len(o) or 4))
    else: new = None
    if new == o and type(new) is type(o):
        return None, None                      # identity mutation, not a test
    parent[path[-1]] = new
    return ".".join(str(p) for p in path), new

rng = random.Random()
caught = accepted = errored = skipped = 0
expected_sha = base['carrier_sha256']
import hashlib
for i in range(TRIALS):
    m = json.loads(json.dumps(base))
    where, new = None, None
    for _ in range(20):                        # retry until the mutation really changes something
        m = json.loads(json.dumps(base))
        where, new = mutate(m, rng)
        if where is not None and m != base:
            break
    if where is None:
        skipped += 1
        continue
    p = os.path.join(d, f"m{i}.json"); json.dump(m, open(p, "w"))
    out = os.path.join(d, f"r{i}.mpg")
    r = subprocess.run([GL, "unlock", locked, "-o", out, "-m", p, "--password", "pw"],
                       capture_output=True, text=True)
    txt = (r.stdout + r.stderr).lower()
    if r.returncode != 0 and ("mac check failed" in txt or "authentication tag" in txt or "altered" in txt or "wrong key" in txt):
        caught += 1
    elif r.returncode != 0:
        errored += 1; print(f"  other-refusal @{where}: {txt.strip().splitlines()[-1][:80]}")
    else:
        got = hashlib.sha256(open(out, "rb").read()).hexdigest()
        verdict = "output still correct" if got.startswith(expected_sha[:40]) or got == expected_sha else "OUTPUT DIFFERS"
        accepted += 1; print(f"  !! ACCEPTED @{where} -> {new!r}  [{verdict}]")
print(f"mutations={TRIALS - skipped}  caught_by_MAC={caught}  refused_other={errored}  ACCEPTED={accepted}  skipped_identity={skipped}")
print("RESULT", "PASS" if accepted == 0 else "FAIL")
sys.exit(0 if accepted == 0 else 1)
