#!/usr/bin/env python3
"""Round-trip test for ffedit H.264 mv / mv_delta.

usage: mvtest.py FFEDIT INPUT.264 FEATURE WORKDIR [SEED]

(a) edited stream decodes with ffmpeg without errors, same frame count
(b) re-export from the edited stream returns exactly the applied values
(c) applying the original JSON on the edited stream restores the input
"""
import json, os, random, subprocess, sys

MVD_RANGE = 64

ffedit, src, feat, work = sys.argv[1:5]
seed = int(sys.argv[5]) if len(sys.argv) > 5 else 1
os.makedirs(work, exist_ok=True)
rng = random.Random(seed)

def run(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True)

def p(name):
    return os.path.join(work, name)

def slots(doc):
    """flatten every mv slot of every frame, in file order"""
    out = []
    for f in doc["streams"][0]["frames"]:
        d = f.get(feat, {})
        for dirn in ("forward", "backward"):
            for row in d.get(dirn, []):
                for mb in row:
                    if mb is None:
                        continue
                    if isinstance(mb[0], list):
                        out.extend(tuple(x) for x in mb)
                    else:
                        out.append(tuple(mb))
    return out

def mutate(doc):
    for f in doc["streams"][0]["frames"]:
        d = f.get(feat, {})
        for dirn in ("forward", "backward"):
            for row in d.get(dirn, []):
                for i, mb in enumerate(row):
                    if mb is None:
                        continue
                    if isinstance(mb[0], list):
                        row[i] = [[rng.randint(-MVD_RANGE, MVD_RANGE), rng.randint(-MVD_RANGE, MVD_RANGE)] for _ in mb]
                    else:
                        row[i] = [rng.randint(-MVD_RANGE, MVD_RANGE), rng.randint(-MVD_RANGE, MVD_RANGE)]

def frames(path):
    r = run("ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
            "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", path)
    return r.stdout.strip()

ok = True
def check(cond, msg):
    global ok
    print(("PASS " if cond else "FAIL ") + msg)
    ok = ok and cond

# export original
r = run(ffedit, "-i", src, "-f", feat, "-e", p("orig.json"))
check(r.returncode == 0, "export original (rc=%d)" % r.returncode)
orig = json.load(open(p("orig.json")))
orig_slots = slots(orig)
print("     frames in JSON: %d, %s slots: %d" % (len(orig["streams"][0]["frames"]), feat, len(orig_slots)))

# mutate and apply
edited = json.load(open(p("orig.json")))
mutate(edited)
json.dump(edited, open(p("edited.json"), "w"))
want = slots(edited)
r = run(ffedit, "-i", src, "-f", feat, "-a", p("edited.json"), "-o", p("edited.264"))
check(r.returncode == 0, "apply edited JSON (rc=%d)" % r.returncode)

# (a) decodes cleanly, same frame count
r = run("ffmpeg", "-v", "error", "-i", p("edited.264"), "-f", "null", "-")
n_src, n_ed = frames(src), frames(p("edited.264"))
check(r.returncode == 0 and r.stderr.strip() == "", "(a) ffmpeg decodes edited.264 with no errors (stderr=%r)" % r.stderr.strip()[:200])
check(n_src == n_ed, "(a) frame count %s -> %s" % (n_src, n_ed))

# (b) re-export equals applied values, same layout
r = run(ffedit, "-i", p("edited.264"), "-f", feat, "-e", p("reexport.json"))
check(r.returncode == 0, "export from edited.264 (rc=%d)" % r.returncode)
got = slots(json.load(open(p("reexport.json"))))
check(len(got) == len(want), "(b) slot count %d == %d" % (len(got), len(want)))
diff = sum(1 for a, b in zip(got, want) if a != b)
check(diff == 0 and len(got) == len(want), "(b) re-exported values identical (%d differ)" % diff)

# (c) original JSON onto edited.264 restores the input
r = run(ffedit, "-i", p("edited.264"), "-f", feat, "-a", p("orig.json"), "-o", p("restored.264"))
check(r.returncode == 0, "apply original JSON onto edited.264 (rc=%d)" % r.returncode)
same = open(src, "rb").read() == open(p("restored.264"), "rb").read()
check(same, "(c) restored.264 == %s byte for byte" % os.path.basename(src))
print("sizes: src %d edited %d restored %d" % (os.path.getsize(src), os.path.getsize(p("edited.264")), os.path.getsize(p("restored.264"))))
sys.exit(0 if ok else 1)
